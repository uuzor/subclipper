"""Clip listing, scoring explanation, and rendering endpoints."""

from __future__ import annotations

import threading
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...config import get_settings
from ...db import get_db, session_scope
from ...logging_setup import get_logger
from ...models import Clip, ClipStatus, Project, Workspace
from ...pipeline.tasks import enqueue_render
from ...rendering import platforms
from ...rendering.renderer import render_clip, render_clip_by_id
from ...schemas import ClipOut, RenderRequest
from ...storage import get_storage
from ..deps import current_workspace, get_scoped_project

logger = get_logger(__name__)
router = APIRouter(prefix="/api/v1", tags=["clips"])


def _clip_out(clip: Clip) -> ClipOut:
    payload = ClipOut.model_validate(clip)
    payload.duration_seconds = round(clip.end_seconds - clip.start_seconds, 3)
    return payload


@router.get("/projects/{project_id}/clips", response_model=list[ClipOut])
def list_clips(
    project_id: str,
    min_score: float = 0.0,
    moment_type: str | None = None,
    db: Session = Depends(get_db),
    workspace: Workspace = Depends(current_workspace),
) -> list[ClipOut]:
    # Resolving the project through the workspace scope is what stops a clip
    # list being read by an id guess from another tenant.
    get_scoped_project(db, project_id, workspace.id)

    query = select(Clip).where(Clip.project_id == project_id)
    if min_score:
        query = query.where(Clip.score >= min_score)
    if moment_type:
        query = query.where(Clip.moment_type == moment_type)
    clips = db.scalars(query.order_by(Clip.rank)).all()
    return [_clip_out(clip) for clip in clips]


@router.get("/clips/{clip_id}", response_model=ClipOut)
def get_clip(
    clip_id: str,
    db: Session = Depends(get_db),
    workspace: Workspace = Depends(current_workspace),
) -> ClipOut:
    return _clip_out(_scoped_clip(db, clip_id, workspace.id))


def _scoped_clip(db: Session, clip_id: str, workspace_id: str) -> Clip:
    """A clip the caller may see, found through its project's workspace.

    Clips have no ``workspace_id`` of their own; they inherit the project's.
    Joining rather than denormalising keeps the two from drifting apart.
    """
    clip = db.scalars(
        select(Clip)
        .join(Project, Project.id == Clip.project_id)
        .where(Clip.id == clip_id, Project.workspace_id == workspace_id)
    ).first()
    if clip is None:
        raise HTTPException(status_code=404, detail="clip not found")
    return clip


@router.post("/projects/{project_id}/render", response_model=list[ClipOut])
def render_project_clips(
    project_id: str,
    payload: RenderRequest,
    db: Session = Depends(get_db),
    workspace: Workspace = Depends(current_workspace),
) -> list[ClipOut]:
    get_scoped_project(db, project_id, workspace.id)
    settings = get_settings()

    # Resolve the platform before claiming any clip: an unknown key should fail
    # the request outright, not mark clips as rendering and then fail each one.
    try:
        platforms.get_spec(payload.platform) if payload.platform else None
    except platforms.UnknownPlatform as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if payload.clip_ids:
        # Both filters: the ids come from the client, so the project scope is
        # what stops a caller rendering another tenant's clip by passing its id.
        clips = list(
            db.scalars(
                select(Clip).where(
                    Clip.id.in_(payload.clip_ids), Clip.project_id == project_id
                )
            )
        )
    else:
        clips = list(
            db.scalars(
                select(Clip)
                .where(Clip.project_id == project_id)
                .order_by(Clip.rank)
                .limit(payload.max_clips)
            )
        )
    if not clips:
        raise HTTPException(status_code=400, detail="no clips available to render")

    if not settings.task_always_eager:
        # A render outlives the edge proxy's request timeout on a real video, and
        # the proxy answers 502 while ffmpeg keeps working - the caller sees a
        # failure for work that actually succeeds. Hand it to the worker and let
        # the client poll clip status, the same way processing works.
        for clip in clips:
            enqueue_render(
                clip.id,
                burn_captions=payload.burn_captions,
                vertical=payload.vertical,
                platform=payload.platform,
            )
            clip.status = ClipStatus.rendering
        db.commit()
        for clip in clips:
            db.refresh(clip)
        return [_clip_out(clip) for clip in clips]

    if settings.render_in_background:
        # No broker is configured, so there is no worker to hand the clip to and
        # the render has to run in this process. A thread keeps it out of the
        # request: the caller gets clip status immediately instead of a proxy
        # timeout on work that would have succeeded. The thread opens its own
        # session because a SQLAlchemy session is not thread-safe and the
        # request's session closes when the response is sent.
        clip_ids = [clip.id for clip in clips]
        for clip in clips:
            clip.status = ClipStatus.rendering
        # Commit before dispatching: the thread renders in its own session, and a
        # render that finishes first would otherwise be overwritten back to
        # ``rendering`` by this session's later commit.
        db.commit()
        for clip_id in clip_ids:
            _render_in_background(
                clip_id,
                burn_captions=payload.burn_captions,
                vertical=payload.vertical,
                platform=payload.platform,
            )
        return [_clip_out(clip) for clip in clips]

    rendered: list[ClipOut] = []
    for clip in clips:
        try:
            render_clip(
                db,
                clip,
                burn_captions=payload.burn_captions,
                vertical=payload.vertical,
                platform=payload.platform,
            )
        except Exception as exc:  # noqa: BLE001 - report per-clip failure
            logger.error("render.endpoint_failed", clip_id=clip.id, error=str(exc))
            raise HTTPException(
                status_code=500, detail=f"failed to render clip {clip.id}: {exc}"
            ) from exc
        rendered.append(_clip_out(clip))
    return rendered


def _render_in_background(
    clip_id: str,
    *,
    burn_captions: bool,
    vertical: bool,
    platform: str | None = None,
) -> None:
    def work() -> None:
        try:
            with session_scope() as session:
                render_clip_by_id(
                    session,
                    clip_id,
                    burn_captions=burn_captions,
                    vertical=vertical,
                    platform=platform,
                )
        except Exception as exc:  # noqa: BLE001 - the clip records the failure
            logger.error("render.background_failed", clip_id=clip_id, error=str(exc))
            _record_render_failure(clip_id, exc)

    threading.Thread(target=work, name=f"render-{clip_id}", daemon=True).start()


def _record_render_failure(clip_id: str, exc: Exception) -> None:
    """Make a failed background render visible on the clip.

    ``render_clip`` records its own ffmpeg failures, but several checks run before
    that - a missing source file, an over-long clip for the platform - and those
    used to reach only the log. The clip then kept whatever status it had, so the
    page showed nothing at all.
    """
    try:
        with session_scope() as session:
            clip = session.get(Clip, clip_id)
            if clip is None:
                return
            clip.status = ClipStatus.render_failed
            clip.render_meta = {**(clip.render_meta or {}), "error": str(exc)}
    except Exception as inner:  # noqa: BLE001 - never mask the original failure
        logger.error("render.failure_not_recorded", clip_id=clip_id, error=str(inner))


@router.get("/export-platforms")
def list_export_platforms() -> list[dict]:
    """List the export targets a render request may name.

    Clients read this rather than hardcoding the list, so a new destination is a
    server change and not a client release.
    """
    return [
        {
            "key": spec.key,
            "label": spec.label,
            "width": spec.width,
            "height": spec.height,
            "aspect": spec.aspect,
            "max_seconds": spec.max_seconds,
            "notes": spec.notes,
        }
        for spec in platforms.all_specs()
    ]


def _storage_key(clip: Clip) -> str | None:
    """The object-storage key recorded when the render was uploaded."""
    meta = clip.render_meta or {}
    key = meta.get("storage_key")
    return key if isinstance(key, str) and key else None


def _local_render_path(clip: Clip) -> Path | None:
    """The rendered MP4 on this container, if it is still there."""
    if not clip.render_path:
        return None
    path = Path(clip.render_path)
    return path if path.is_file() else None


@router.get("/clips/{clip_id}/download")
def download_clip(
    clip_id: str,
    db: Session = Depends(get_db),
    workspace: Workspace = Depends(current_workspace),
):
    clip = _scoped_clip(db, clip_id, workspace.id)
    path = _local_render_path(clip)
    if path is not None:
        return FileResponse(
            path,
            media_type="video/mp4",
            filename=f"quickreel-{clip.id}.mp4",
        )
    key = _storage_key(clip)
    if key:
        try:
            return RedirectResponse(get_storage().url_for(key), status_code=307)
        except Exception as exc:  # noqa: BLE001 - presign can fail on bad creds
            logger.error("clip.presign_failed", clip_id=clip.id, error=str(exc))
    raise HTTPException(status_code=409, detail="clip has not been rendered yet")


@router.get("/clips/{clip_id}/video")
def stream_clip(
    clip_id: str,
    db: Session = Depends(get_db),
    workspace: Workspace = Depends(current_workspace),
):
    """Same bytes as the download, served inline so a <video> tag can play it.

    A local file is served with ``FileResponse``, which sends ``Accept-Ranges``
    and honours Range headers - Safari requires both before it will play any
    video at all. When the container has been restarted on ephemeral storage the
    local file is gone; the copy in object storage is then reachable through a
    short-lived presigned URL, so redirect rather than proxying the bytes twice.
    """
    clip = _scoped_clip(db, clip_id, workspace.id)
    path = _local_render_path(clip)
    if path is not None:
        return FileResponse(
            path,
            media_type="video/mp4",
            headers={"Content-Disposition": "inline"},
        )

    key = _storage_key(clip)
    if key:
        try:
            return RedirectResponse(get_storage().url_for(key), status_code=307)
        except Exception as exc:  # noqa: BLE001 - presign can fail on bad creds
            logger.error("clip.presign_failed", clip_id=clip.id, error=str(exc))

    raise HTTPException(status_code=409, detail="clip has not been rendered yet")
