"""Thin, explicit wrapper around ffmpeg / ffprobe.

Everything that shells out to the media toolchain lives here so the rest of the
codebase can stay declarative. Commands are built as argument lists (never shell
strings) which keeps untrusted filenames from reaching a shell.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ..config import get_settings
from ..logging_setup import get_logger

logger = get_logger(__name__)


class FFmpegError(RuntimeError):
    """Raised when an ffmpeg/ffprobe invocation fails."""


@dataclass(slots=True)
class MediaInfo:
    path: str
    duration_seconds: float
    width: int
    height: int
    fps: float
    has_audio: bool
    raw: dict = field(default_factory=dict)

    @property
    def aspect_ratio(self) -> float:
        return self.width / self.height if self.height else 0.0


def _run(cmd: list[str], timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    logger.debug("ffmpeg.run", cmd=cmd)
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=True,
        )
    except subprocess.CalledProcessError as exc:  # pragma: no cover - error path
        tail = (exc.stderr or "")[-2000:]
        raise FFmpegError(f"{cmd[0]} failed ({exc.returncode}): {tail}") from exc
    except subprocess.TimeoutExpired as exc:  # pragma: no cover - error path
        raise FFmpegError(f"{cmd[0]} timed out after {timeout}s") from exc


def _parse_fraction(value: str | None) -> float:
    if not value:
        return 0.0
    if "/" in value:
        num, _, den = value.partition("/")
        try:
            den_f = float(den)
            return float(num) / den_f if den_f else 0.0
        except ValueError:
            return 0.0
    try:
        return float(value)
    except ValueError:
        return 0.0


def require_binaries() -> None:
    settings = get_settings()
    for binary in (settings.ffmpeg_bin, settings.ffprobe_bin):
        if shutil.which(binary) is None:
            raise FFmpegError(f"required binary '{binary}' not found on PATH. Install ffmpeg.")


def probe(path: str | Path) -> MediaInfo:
    settings = get_settings()
    result = _run(
        [
            settings.ffprobe_bin,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ]
    )
    payload = json.loads(result.stdout or "{}")
    streams = payload.get("streams", [])
    fmt = payload.get("format", {})

    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    duration = 0.0
    for candidate in (fmt.get("duration"), (video or {}).get("duration")):
        if candidate:
            try:
                duration = float(candidate)
                break
            except ValueError:
                continue

    fps = _parse_fraction((video or {}).get("avg_frame_rate")) or _parse_fraction(
        (video or {}).get("r_frame_rate")
    )

    return MediaInfo(
        path=str(path),
        duration_seconds=duration,
        width=int((video or {}).get("width") or 0),
        height=int((video or {}).get("height") or 0),
        fps=fps,
        has_audio=audio is not None,
        raw=payload,
    )


def extract_audio(source: str | Path, target: str | Path, sample_rate: int = 16000) -> Path:
    """Extract mono PCM WAV audio, the input format speech models expect."""
    settings = get_settings()
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            settings.ffmpeg_bin,
            "-y",
            "-i",
            str(source),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            "-acodec",
            "pcm_s16le",
            str(target),
        ],
        timeout=settings.render_timeout_seconds,
    )
    return target


def slice_audio(
    source: str | Path,
    target: str | Path,
    *,
    start: float,
    duration: float,
    sample_rate: int = 16000,
) -> Path:
    """Cut a mono WAV window out of ``source``.

    Used to feed a hosted speech model in bounded pieces: a two-hour source is
    hundreds of megabytes of PCM, which is too much for a single upload.
    """
    settings = get_settings()
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            settings.ffmpeg_bin,
            "-y",
            "-ss",
            f"{start:.3f}",
            "-t",
            f"{duration:.3f}",
            "-i",
            str(source),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            "-acodec",
            "pcm_s16le",
            str(target),
        ],
        timeout=settings.render_timeout_seconds,
    )
    return target


def detect_silences(
    source: str | Path,
    noise_db: float = -32.0,
    min_duration: float = 0.35,
) -> list[tuple[float, float]]:
    """Return (start, end) pairs of silent stretches using ffmpeg silencedetect."""
    settings = get_settings()
    result = subprocess.run(
        [
            settings.ffmpeg_bin,
            "-i",
            str(source),
            "-af",
            f"silencedetect=noise={noise_db}dB:d={min_duration}",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        timeout=settings.render_timeout_seconds,
    )
    silences: list[tuple[float, float]] = []
    current_start: float | None = None
    for line in (result.stderr or "").splitlines():
        if "silence_start:" in line:
            try:
                current_start = float(line.split("silence_start:")[1].strip().split()[0])
            except (IndexError, ValueError):
                current_start = None
        elif "silence_end:" in line and current_start is not None:
            try:
                end = float(line.split("silence_end:")[1].strip().split()[0])
            except (IndexError, ValueError):
                continue
            silences.append((current_start, end))
            current_start = None
    return silences


# astats reports -inf for digital silence; clamp to a finite floor instead.
_SILENCE_DB = -120.0


@dataclass(slots=True)
class AudioFrame:
    """One analysis window of the audio envelope.

    Levels are dBFS. ``crest`` is peak-over-RMS, ``flatness`` and ``entropy``
    come from astats, and ``zcr`` is the zero-crossing rate (0..1).
    """

    time: float
    rms: float
    peak: float
    crest: float
    flatness: float
    entropy: float
    zcr: float


def audio_envelope(
    source: str | Path,
    window_seconds: float = 0.5,
    sample_rate: int = 16000,
) -> list[AudioFrame]:
    """Return per-window loudness and spectral-shape statistics.

    Uses ffmpeg's ``astats`` rather than librosa, so audio analysis needs no
    numpy, scipy or numba - about 1GB of dependencies for one function. Only
    loudness is measured here. astats also reports crest factor and a
    "flat factor", but on real material those do not separate laughter from
    speech (see ``SignalAudioAnalyzer``), so they are recorded and unused.
    """
    settings = get_settings()
    samples = max(int(sample_rate * window_seconds), 1)
    result = subprocess.run(
        [
            settings.ffmpeg_bin,
            "-hide_banner",
            "-nostats",
            "-i",
            str(source),
            "-vn",
            "-af",
            f"aresample={sample_rate},asetnsamples=n={samples}:p=0,"
            "astats=metadata=1:reset=1,ametadata=print:file=-",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        timeout=settings.render_timeout_seconds,
    )

    frames: list[AudioFrame] = []
    current_time: float | None = None
    values: dict[str, float] = {}

    def flush() -> None:
        if current_time is None or "RMS_level" not in values:
            return
        # Digital silence makes astats emit -inf. Callers take percentiles and
        # averages, where an infinity poisons the whole result, so floor it.
        def level(name: str) -> float:
            value = values.get(name, _SILENCE_DB)
            return _SILENCE_DB if value != value or value == float("-inf") else value

        frames.append(
            AudioFrame(
                time=current_time,
                rms=level("RMS_level"),
                peak=level("Peak_level"),
                crest=values.get("Crest_factor", 0.0),
                flatness=values.get("Flat_factor", 0.0),
                entropy=values.get("Entropy", 0.0),
                zcr=values.get("Zero_crossings_rate", 0.0),
            )
        )

    for line in (result.stdout or "").splitlines():
        line = line.strip()
        if line.startswith("frame:"):
            flush()
            values = {}
            marker = "pts_time:"
            current_time = None
            if marker in line:
                try:
                    current_time = float(line.split(marker, 1)[1].split()[0])
                except (IndexError, ValueError):
                    current_time = None
            continue
        if "=" not in line:
            continue
        key, _, raw = line.partition("=")
        # astats reports per-channel keys first (``astats.1.RMS_level``) and then
        # an Overall duplicate. Prefer the channel key and skip the duplicate so
        # a stereo file does not halve the envelope resolution.
        if ".Overall." in key:
            continue
        name = key.rsplit(".", 1)[-1]
        try:
            values[name] = float(raw)
        except ValueError:
            continue
    flush()
    return frames


def extract_frames(
    source: str | Path,
    target_dir: str | Path,
    *,
    interval_seconds: float = 30.0,
    max_frames: int = 24,
    width: int = 480,
) -> list[Path]:
    """Sample evenly spaced frames from ``source`` for a vision model.

    The whole video is never uploaded: a 2-hour podcast is gigabytes, so frames
    are downscaled and capped at ``max_frames``. Returns paths in time order.
    """
    settings = get_settings()
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    # Clear previous frames first: ffmpeg overwrites frame_0001..N but leaves any
    # extras from an earlier run with a higher cap, which would be sampled back.
    for stale in target_dir.glob("frame_*.jpg"):
        stale.unlink(missing_ok=True)
    pattern = str(target_dir / "frame_%04d.jpg")
    _run(
        [
            settings.ffmpeg_bin,
            "-y",
            "-i",
            str(source),
            "-vf",
            f"fps=1/{max(interval_seconds, 0.1)},scale={width}:-2",
            "-frames:v",
            str(max_frames),
            "-q:v",
            "4",
            pattern,
        ],
        timeout=settings.render_timeout_seconds,
    )
    return sorted(target_dir.glob("frame_*.jpg"))


def detect_scenes(source: str | Path, threshold: float = 0.3) -> list[float]:
    """Return cut timestamps using ffmpeg's scene-change score.

    This replaces an earlier librosa-based silence heuristic for visuals and
    needs no Python CV dependency.
    """
    settings = get_settings()
    result = subprocess.run(
        [
            settings.ffmpeg_bin,
            "-i",
            str(source),
            "-filter:v",
            f"select='gt(scene,{threshold})',showinfo",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        timeout=settings.render_timeout_seconds,
    )
    scenes: list[float] = []
    for line in (result.stderr or "").splitlines():
        marker = "pts_time:"
        if marker not in line:
            continue
        try:
            value = line.split(marker, 1)[1].split()[0]
            scenes.append(float(value))
        except (IndexError, ValueError):
            continue
    return scenes


def render_clip(
    source: str | Path,
    target: str | Path,
    start: float,
    end: float,
    *,
    width: int = 1080,
    height: int = 1920,
    burn_captions: bool = False,
    subtitle_path: str | Path | None = None,
    normalize_audio: bool = True,
    bottom_safe_pixels: int = 0,
    video_bitrate: str = "6M",
    audio_bitrate: str = "192k",
) -> Path:
    """Cut ``[start, end]`` and reframe to ``width`` x ``height``.

    Strategy: scale to cover the target canvas then centre-crop. A saliency-aware
    crop driven by face tracking plugs in here later (see docs/architecture.md)
    by replacing the ``crop`` filter with per-frame x offsets.

    ``bottom_safe_pixels`` lifts burned-in captions above the area the destination
    platform covers with its own interface. Captions drawn at the default
    baseline are hidden behind TikTok's description and Instagram's action rail.
    """
    settings = get_settings()
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)

    duration = max(0.1, end - start)

    filters = [
        f"scale={width}:{height}:force_original_aspect_ratio=increase",
        f"crop={width}:{height}",
        "setsar=1",
    ]
    if burn_captions and subtitle_path:
        filters.append(_subtitle_filter(subtitle_path, height, bottom_safe_pixels))

    cmd = [
        settings.ffmpeg_bin,
        "-y",
        "-ss",
        f"{start:.3f}",
        "-t",
        f"{duration:.3f}",
        "-i",
        str(source),
    ]
    if normalize_audio:
        cmd += ["-af", "loudnorm=I=-16:TP=-1.5:LRA=11"]
    cmd += [
        "-vf",
        ",".join(filters),
        "-threads",
        str(settings.render_threads),
        "-filter_threads",
        str(settings.render_threads),
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "20",
        "-maxrate",
        video_bitrate,
        "-bufsize",
        video_bitrate,
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        audio_bitrate,
        "-movflags",
        "+faststart",
        str(target),
    ]
    _run(cmd, timeout=settings.render_timeout_seconds)
    return target


# libass scales ASS style values by frame_height / this default script height
# when the subtitle file has no header of its own, which an SRT never does.
_LIBASS_SCRIPT_HEIGHT = 288


def _subtitle_filter(
    subtitle_path: str | Path, frame_height: int, bottom_safe_pixels: int
) -> str:
    """Build the ``subtitles`` filter, lifted above the platform's UI.

    ``MarginV`` is not in output pixels. An SRT file carries no ASS header, so
    libass falls back to its default 288-unit script height and scales styles by
    ``frame_height / 288``. Passing pixel values straight through therefore
    multiplies the lift by ~6.7 at 1920 tall, which pushes captions off the top
    of the frame and they disappear entirely. The margin is divided back into
    libass units so the requested pixel lift is what actually lands.
    """
    escaped = (
        str(subtitle_path)
        .replace("\\", "\\\\")
        .replace(":", r"\:")
        .replace("'", r"\'")
    )
    if bottom_safe_pixels <= 0:
        return f"subtitles='{escaped}'"
    scale = frame_height / _LIBASS_SCRIPT_HEIGHT
    margin = max(0, int(round(bottom_safe_pixels / scale)))
    # The caption is drawn ``MarginV`` units above the bottom edge. A margin at or
    # beyond the frame height would place it off-screen, so it is capped short of
    # that in the same units the margin is expressed in.
    margin = min(margin, int(_LIBASS_SCRIPT_HEIGHT * 0.9))
    return f"subtitles='{escaped}':force_style='MarginV={margin}'"


def render_vertical_clip(
    source: str | Path,
    target: str | Path,
    start: float,
    end: float,
    **kwargs,
) -> Path:
    """Cut ``[start, end]`` and reframe to a 9:16 vertical clip."""
    return render_clip(source, target, start, end, **kwargs)


def render_horizontal_clip(
    source: str | Path,
    target: str | Path,
    start: float,
    end: float,
    **kwargs,
) -> Path:
    """Cut ``[start, end]`` and reframe to 1920x1080."""
    kwargs.setdefault("width", 1920)
    kwargs.setdefault("height", 1080)
    return render_clip(source, target, start, end, **kwargs)
