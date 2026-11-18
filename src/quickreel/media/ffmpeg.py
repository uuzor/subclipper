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


def render_vertical_clip(
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
) -> Path:
    """Cut ``[start, end]`` and reframe to a 9:16 vertical clip.

    Strategy: scale to cover the target canvas then centre-crop. A saliency-aware
    crop driven by face tracking plugs in here later (see docs/architecture.md)
    by replacing the ``crop`` filter with per-frame x offsets.
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
        # Escape the path for the filter graph.
        escaped = str(subtitle_path).replace("\\", "\\\\").replace(":", r"\:").replace("'", r"\'")
        filters.append(f"subtitles='{escaped}'")

    audio_filters = "loudnorm=I=-16:TP=-1.5:LRA=11" if normalize_audio else None

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
    if audio_filters:
        cmd += ["-af", audio_filters]
    cmd += [
        "-vf",
        ",".join(filters),
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        str(target),
    ]
    _run(cmd, timeout=settings.render_timeout_seconds)
    return target


def render_horizontal_clip(
    source: str | Path,
    target: str | Path,
    start: float,
    end: float,
    *,
    burn_captions: bool = False,
    subtitle_path: str | Path | None = None,
    normalize_audio: bool = True,
) -> Path:
    """Cut ``[start, end]`` keeping the source aspect ratio, capped at 1080p.

    Used when a caller asks for a non-vertical export (``vertical=false``).
    """
    settings = get_settings()
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)

    duration = max(0.1, end - start)

    filters = [
        "scale=1920:1080:force_original_aspect_ratio=decrease",
        "pad=1920:1080:(ow-iw)/2:(oh-ih)/2",
        "setsar=1",
    ]
    if burn_captions and subtitle_path:
        escaped = str(subtitle_path).replace("\\", "\\\\").replace(":", r"\:").replace("'", r"\'")
        filters.append(f"subtitles='{escaped}'")

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
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        str(target),
    ]
    _run(cmd, timeout=settings.render_timeout_seconds)
    return target
