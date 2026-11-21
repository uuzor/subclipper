"""Clip rendering: subtitle generation plus platform-specific export.

Face-tracked reframing plugs in here later; today the reframe is a centre crop.
What differs per destination - canvas, caption safe area, bitrate, duration
ceiling - comes from ``rendering.platforms``.
"""

from __future__ import annotations

import threading
from pathlib import Path

from sqlalchemy import select
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


def _scaled_dimensions(width: int, height: int, max_dimension: int) -> tuple[int, int]:
    """Shrink ``width``x``height`` to fit ``max_dimension``, keeping the aspect.

    Both sides are rounded down to even numbers because h264 with yuv420p cannot
    encode an odd dimension, and a half-pixel crop is invisible next to a clip
    that fails to encode at all.
    """
    longest = max(width, height)
    if max_dimension <= 0 or longest <= max_dimension:
        return width, height
    scale = max_dimension / longest
    return (
        max(2, int(width * scale) // 2 * 2),
        max(2, int(height * scale) // 2 * 2),
    )


_render_locks: dict[tuple[str, str], threading.Lock] = {}
_locks_guard = threading.Lock()


def _render_lock(clip_id: str, spec_key: str) -> threading.Lock:
    """One lock per clip+platform, so two renders cannot share an output file.

    Two entry points render the same clip: the pipeline auto-render that runs at
    the end of processing, and the ``/render`` endpoint. Both write the same
    deterministic path (``{clip.id}-{spec.key}.mp4``), and the pipeline sets the
    project to ``ready`` *before* it renders, so a client that reacts to ``ready``
    by calling ``/render`` overlaps with it. Two ffmpeg processes writing one
    path interleave their ``moov``/``mdat`` boxes and leave a file that probes
    fine but has no decodable frames. A lock is enough because the process model
    is one uvicorn worker; the DB status is the cross-process guard.
    """
    key = (clip_id, spec_key)
    with _locks_guard:
        lock = _render_locks.get(key)
        if lock is None:
            lock = _render_locks[key] = threading.Lock()
        return lock


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

    lock = _render_lock(clip.id, spec.key)
    if not lock.acquire(blocking=False):
        # Another render of this clip is already writing the same file. Waiting
        # would just duplicate the work, and running would corrupt the output, so
        # leave the clip to the render already in flight. The status is left
        # alone: committing ``rendering`` here could land after the other render
        # has already committed ``rendered``, stranding the clip until restart.
        logger.info("render.already_running", clip_id=clip.id, platform=spec.key)
        session.refresh(clip)
        return clip
    try:
        clip.status = ClipStatus.rendering
        session.commit()

        subtitle_path: Path | None = None
        if burn_captions:
            subtitle_path = build_srt(clip, project)

        output = settings.renders_dir / f"{clip.id}-{spec.key}.mp4"
        # Write beside the target and rename once complete. ffmpeg writes the
        # moov atom last, so a partial file at the final path is a valid-looking
        # container with no decodable frames - exactly what a client would cache
        # and fail to play. A rename is atomic, so the path only ever holds a
        # finished render.
        partial = output.with_name(f"{output.stem}.partial{output.suffix}")
        width, height = _scaled_dimensions(
            spec.width, spec.height, settings.render_max_dimension
        )
        try:
            ffmpeg.render_clip(
                project.media.path,
                partial,
                clip.start_seconds,
                clip.end_seconds,
                width=width,
                height=height,
                burn_captions=burn_captions,
                subtitle_path=subtitle_path,
                bottom_safe_pixels=spec.bottom_safe_pixels,
                video_bitrate=spec.video_bitrate,
                audio_bitrate=spec.audio_bitrate,
            )
            partial.replace(output)
        except Exception as exc:  # noqa: BLE001 - surfaced to the API
            partial.unlink(missing_ok=True)
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
            # duplicate the file next to itself and make a deleted MP4 still
            # read as playable. The key (not the returned URI) is stored because
            # that is what every read path - url_for, get, exists - takes.
            key = f"renders/{output.name}"
            try:
                storage.put(output, key)
                storage_key = key
            except Exception as exc:  # noqa: BLE001 - local file is still usable
                # A failed upload must not fail a render that produced a
                # playable file: the local copy still serves until restart.
                logger.error("render.upload_failed", clip_id=clip.id, error=str(exc))
        clip.render_meta = {
            "platform": spec.key,
            "orientation": "vertical" if height > width else "horizontal",
            "width": width,
            "height": height,
            "captions_burned": burn_captions,
            "subtitle_path": str(subtitle_path) if subtitle_path else None,
            "size_bytes": output.stat().st_size if output.exists() else 0,
            "storage_key": storage_key,
        }
        session.commit()
    finally:
        lock.release()
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
    try:
        return render_clip(
            session, clip, burn_captions=burn_captions, vertical=vertical, platform=platform
        )
    except Exception as exc:
        # Every async caller - Celery worker and in-process thread alike - comes
        # through here, and each of them used to let the exception die in a log
        # line. Checks that run before ffmpeg starts (missing source, over-long
        # clip for the platform) left the clip on its old status, so the page had
        # nothing to show. Recording it here covers both paths at once.
        _mark_render_failed(session, clip, exc)
        raise


def _mark_render_failed(session: Session, clip: Clip, exc: Exception) -> None:
    """Store ``exc`` on the clip so the API can return it to the client."""
    clip.status = ClipStatus.render_failed
    clip.render_meta = {**(clip.render_meta or {}), "error": str(exc)}
    try:
        session.commit()
    except Exception:  # noqa: BLE001 - the original error matters more
        session.rollback()
        logger.error("render.failure_not_recorded", clip_id=clip.id, error=str(exc))


def recover_interrupted_renders() -> int:
    """Reset clips left in ``rendering`` by a process that died mid-render.

    A render runs in a thread inside the web process, so an OOM kill or a
    platform restart takes it down without any chance to record a failure. The
    row keeps ``rendering`` forever and the page polls a spinner that never
    resolves. Nothing can be rendering at boot, so every such row is stale by
    definition and becomes retryable again.
    """
    from ..db import session_scope

    with session_scope() as session:
        stale = list(session.scalars(select(Clip).where(Clip.status == ClipStatus.rendering)))
        for clip in stale:
            clip.status = ClipStatus.render_failed
            clip.render_meta = {
                **(clip.render_meta or {}),
                "error": "render was interrupted by a server restart; render again to retry",
            }
        if stale:
            logger.warning("render.recovered", clips=len(stale))
        return len(stale)
