"""Clip rendering: subtitle generation plus vertical export.

Face-tracked reframing plugs in here later; today the reframe is a centre crop
that already produces platform-ready 1080x1920 output.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session

from ..config import get_settings
from ..logging_setup import get_logger
from ..media import ffmpeg
from ..models import Clip, ClipStatus, Project

logger = get_logger(__name__)


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
) -> Clip:
    settings = get_settings()
    project = session.get(Project, clip.project_id)
    if project is None or project.media is None:
        raise RuntimeError(f"clip {clip.id} has no project media to render from")

    clip.status = ClipStatus.rendering
    session.commit()

    subtitle_path: Path | None = None
    if burn_captions:
        subtitle_path = build_srt(clip, project)

    output = settings.renders_dir / f"{clip.id}.mp4"
    renderer = ffmpeg.render_vertical_clip if vertical else ffmpeg.render_horizontal_clip
    try:
        renderer(
            project.media.path,
            output,
            clip.start_seconds,
            clip.end_seconds,
            burn_captions=burn_captions,
            subtitle_path=subtitle_path,
        )
    except Exception as exc:  # noqa: BLE001 - surfaced to the API
        clip.status = ClipStatus.render_failed
        clip.render_meta = {"error": str(exc)}
        session.commit()
        logger.error("render.failed", clip_id=clip.id, error=str(exc))
        raise

    clip.render_path = str(output)
    clip.status = ClipStatus.rendered
    clip.render_meta = {
        "orientation": "vertical" if vertical else "horizontal",
        "width": 1080 if vertical else 1920,
        "height": 1920 if vertical else 1080,
        "captions_burned": burn_captions,
        "subtitle_path": str(subtitle_path) if subtitle_path else None,
        "size_bytes": output.stat().st_size if output.exists() else 0,
    }
    session.commit()
    logger.info("render.done", clip_id=clip.id, path=str(output))
    return clip


def render_clip_by_id(
    session: Session, clip_id: str, *, burn_captions: bool = True, vertical: bool = True
) -> Clip:
    clip = session.get(Clip, clip_id)
    if clip is None:
        raise RuntimeError(f"clip {clip_id} not found")
    return render_clip(session, clip, burn_captions=burn_captions, vertical=vertical)
