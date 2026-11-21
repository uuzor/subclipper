"""Clip rendering: subtitle generation plus platform-specific export.

Face-tracked reframing plugs in here later; today the reframe is a centre crop.
What differs per destination - canvas, caption safe area, bitrate, duration
ceiling - comes from ``rendering.platforms``.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session

from ..config import get_settings
from ..logging_setup import get_logger
from ..media import ffmpeg
from ..models import Clip, ClipStatus, Project
from ..storage import get_storage
from . import platforms

logger = get_logger(__name__)


class PlatformLimitError(ValueError):
    """A clip cannot be exported to a platform because it breaks a platform rule."""


def _srt_timestamp(seconds: float) -> str:
    milliseconds = int(round(seconds * 1000))
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    secs, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{milliseconds:03d}"


def build_srt(clip: Clip, project: Project) -> Path:
    """Write an SRT file with timings relative to the clip start.

    Words are chunked into short cues because one-word-at-a-time captions hold
    attention better on vertical platforms.
    """
    settings = get_settings()
    transcript = project.transcript
    target = settings.artifacts_dir / f"{clip.id}.srt"
    lines: list[str] = []
    index = 1

    if transcript and transcript.segments:
        for segment in transcript.segments:
            start = float(segment["start"])
            end = float(segment["end"])
            if end <= clip.start_seconds or start >= clip.end_seconds:
                continue
            relative_start = max(0.0, start - clip.start_seconds)
            relative_end = min(clip.end_seconds - clip.start_seconds, end - clip.start_seconds)
            if relative_end <= relative_start:
                continue
            lines.append(str(index))
            lines.append(f"{_srt_timestamp(relative_start)} --> {_srt_timestamp(relative_end)}")
            lines.append(segment["text"].strip())
            lines.append("")
            index += 1

    target.write_text("\n".join(lines), encoding="utf-8")
    return target


def render_clip(
    session: Session,
    clip: Clip,
    *,
    burn_captions: bool = True,
    vertical: bool = True,
    platform: str | None = None,
) -> Clip:
    settings = get_settings()
    project = session.get(Project, clip.project_id)
    if project is None or project.media is None:
        raise RuntimeError(f"clip {clip.id} has no project media to render from")

    spec = _resolve_spec(platform, vertical)
    duration = clip.end_seconds - clip.start_seconds
    if spec.max_seconds and duration > spec.max_seconds:
        # Refuse rather than truncate: a clip cut mid-sentence is worse than a
        # clear error the caller can act on by choosing another moment.
        raise PlatformLimitError(
            f"clip {clip.id} is {duration:.1f}s, longer than the "
            f"{spec.max_seconds:.0f}s {spec.label} limit"
        )

    clip.status = ClipStatus.rendering
    session.commit()

    subtitle_path: Path | None = None
    if burn_captions:
        subtitle_path = build_srt(clip, project)

    output = settings.renders_dir / f"{clip.id}-{spec.key}.mp4"
    try:
        ffmpeg.render_clip(
            project.media.path,
            output,
            clip.start_seconds,
            clip.end_seconds,
            width=spec.width,
            height=spec.height,
            burn_captions=burn_captions,
            subtitle_path=subtitle_path,
            bottom_safe_pixels=spec.bottom_safe_pixels,
            video_bitrate=spec.video_bitrate,
            audio_bitrate=spec.audio_bitrate,
        )
    except Exception as exc:  # noqa: BLE001 - surfaced to the API
        clip.status = ClipStatus.render_failed
        clip.render_meta = {"error": str(exc), "platform": spec.key}
        session.commit()
        logger.error("render.failed", clip_id=clip.id, platform=spec.key, error=str(exc))
        raise

    clip.render_path = str(output)
    clip.status = ClipStatus.rendered
    storage_key = None
    storage = get_storage()
    if storage.is_remote:
        # Only a remote copy outlives the container, so only that is worth
        # recording as a fallback. Uploading on a local backend would just
        # duplicate the file next to itself and make a deleted MP4 still read
        # as playable. The key (not the returned URI) is stored because that is
        # what every read path - url_for, get, exists - takes.
        key = f"renders/{output.name}"
        try:
            storage.put(output, key)
            storage_key = key
        except Exception as exc:  # noqa: BLE001 - local file is still usable
            # A failed upload must not fail a render that produced a playable
            # file: the local copy still serves until the next restart.
            logger.error("render.upload_failed", clip_id=clip.id, error=str(exc))
    clip.render_meta = {
        "platform": spec.key,
        "orientation": "vertical" if spec.height > spec.width else "horizontal",
        "width": spec.width,
        "height": spec.height,
        "captions_burned": burn_captions,
        "subtitle_path": str(subtitle_path) if subtitle_path else None,
        "size_bytes": output.stat().st_size if output.exists() else 0,
        "storage_key": storage_key,
    }
    session.commit()
    logger.info("render.done", clip_id=clip.id, platform=spec.key, path=str(output))
    return clip


def _resolve_spec(platform: str | None, vertical: bool) -> platforms.PlatformSpec:
    """Pick the export spec, keeping the older ``vertical`` flag working.

    ``platform`` wins when given. Without it, the historical boolean still
    selects between the 9:16 and 16:9 exports so existing callers are unaffected.
    """
    if platform:
        return platforms.get_spec(platform)
    return platforms.get_spec(
        platforms.DEFAULT_PLATFORM if vertical else "youtube"
    )


def render_clip_by_id(
    session: Session,
    clip_id: str,
    *,
    burn_captions: bool = True,
    vertical: bool = True,
    platform: str | None = None,
) -> Clip:
    clip = session.get(Clip, clip_id)
    if clip is None:
        raise RuntimeError(f"clip {clip_id} not found")
    return render_clip(
        session, clip, burn_captions=burn_captions, vertical=vertical, platform=platform
    )
