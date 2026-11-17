"""Project, upload, transcript and moment endpoints."""

from __future__ import annotations

import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...config import get_settings
from ...db import get_db
from ...logging_setup import get_logger
from ...models import MediaAsset, Project, ProjectStatus
from ...pipeline.orchestrator import Pipeline, PipelineError
from ...pipeline.tasks import enqueue_project
from ...schemas import ProjectCreateIn, ProjectDetailOut, ProjectOut

logger = get_logger(__name__)
router = APIRouter(prefix="/api/v1/projects", tags=["projects"])

_CHUNK = 1024 * 1024


@router.post("", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
def create_project(payload: ProjectCreateIn, db: Session = Depends(get_db)) -> Project:
    project = Project(title=payload.title, source_kind="upload")
    if payload.source_url is not None:
        # Importing a remote URL requires an explicit rights check before download,
        # so URL ingestion is gated behind a separate, audited path.
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=(
                "URL ingestion is not enabled yet. Upload a file you own or "
                "are licensed to use."
            ),
        )
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


@router.get("", response_model=list[ProjectOut])
def list_projects(db: Session = Depends(get_db)) -> list[Project]:
    return list(db.scalars(select(Project).order_by(Project.created_at.desc())))


@router.get("/{project_id}", response_model=ProjectDetailOut)
def get_project(project_id: str, db: Session = Depends(get_db)) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return project


@router.post("/{project_id}/upload", response_model=ProjectDetailOut)
def upload_video(
    project_id: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> Project:
    settings = get_settings()
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")

    destination = settings.uploads_dir / f"{project.id}{_safe_suffix(file.filename)}"
    size = 0
    try:
        with destination.open("wb") as handle:
            while chunk := file.file.read(_CHUNK):
                size += len(chunk)
                if size > settings.max_upload_bytes:
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail=f"upload exceeds {settings.max_upload_mb} MB limit",
                    )
                handle.write(chunk)
    except HTTPException:
        destination.unlink(missing_ok=True)
        raise

    asset = db.scalar(select(MediaAsset).where(MediaAsset.project_id == project.id))
    if asset is None:
        asset = MediaAsset(project_id=project.id)
        db.add(asset)
    asset.path = str(destination)
    asset.size_bytes = size
    project.source_kind = "upload"
    project.status = ProjectStatus.created
    project.error = None
    db.commit()
    logger.info("upload.stored", project_id=project.id, bytes=size)
    return project


@router.post("/{project_id}/process", response_model=ProjectOut, status_code=202)
def process_project(project_id: str, db: Session = Depends(get_db)) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    if project.media is None:
        raise HTTPException(status_code=400, detail="upload a video before processing")

    settings = get_settings()
    if settings.task_always_eager:
        # Run inline so local development needs no broker.
        try:
            Pipeline(db).run(project.id)
        except PipelineError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        db.refresh(project)
    else:
        enqueue_project(project.id)
        db.refresh(project)
    return project


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project(project_id: str, db: Session = Depends(get_db)) -> None:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    if project.media is not None:
        Path(project.media.path).unlink(missing_ok=True)
    for clip in project.clips:
        if clip.render_path:
            Path(clip.render_path).unlink(missing_ok=True)
    db.delete(project)
    db.commit()


def _safe_suffix(filename: str | None) -> str:
    """Only allow a known-safe extension; never trust the client filename."""
    allowed = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}
    suffix = Path(filename or "").suffix.lower()
    return suffix if suffix in allowed else ".mp4"


def copy_local_source(source: str, project_id: str) -> str:
    """Copy an already-authorized local file into the uploads directory."""
    settings = get_settings()
    src = Path(source)
    if not src.exists():
        raise FileNotFoundError(source)
    destination = settings.uploads_dir / f"{project_id}{src.suffix.lower()}"
    shutil.copy2(src, destination)
    return str(destination)
