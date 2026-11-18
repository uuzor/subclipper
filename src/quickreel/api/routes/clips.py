"""Clip listing, scoring explanation, and rendering endpoints."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...db import get_db
from ...logging_setup import get_logger
from ...models import Clip, Project, Workspace
from ...rendering.renderer import render_clip
from ...schemas import ClipOut, RenderRequest
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

    rendered: list[ClipOut] = []
    for clip in clips:
        try:
            render_clip(
                db,
                clip,
                burn_captions=payload.burn_captions,
                vertical=payload.vertical,
            )
        except Exception as exc:  # noqa: BLE001 - report per-clip failure
            logger.error("render.endpoint_failed", clip_id=clip.id, error=str(exc))
            raise HTTPException(
                status_code=500, detail=f"failed to render clip {clip.id}: {exc}"
            ) from exc
        rendered.append(_clip_out(clip))
    return rendered


@router.get("/clips/{clip_id}/download")
def download_clip(
    clip_id: str,
    db: Session = Depends(get_db),
    workspace: Workspace = Depends(current_workspace),
) -> FileResponse:
    clip = _scoped_clip(db, clip_id, workspace.id)
    if not clip.render_path or not Path(clip.render_path).exists():
        raise HTTPException(status_code=409, detail="clip has not been rendered yet")
    return FileResponse(
        clip.render_path,
        media_type="video/mp4",
        filename=f"quickreel-{clip.id}.mp4",
    )
