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
from ...media.downloader import DownloadError, get_downloader
from ...media.url_source import BlockedSourceUrl, SourceUrlError, validate_source_url
from ...models import MediaAsset, Project, ProjectStatus
from ...pipeline.orchestrator import Pipeline, PipelineError
from ...pipeline.tasks import enqueue_project
from ...schemas import (
    ProjectCreateIn,
    ProjectDetailOut,
    ProjectOut,
    RemoteProbeOut,
    UrlIngestIn,
)

logger = get_logger(__name__)
router = APIRouter(prefix="/api/v1/projects", tags=["projects"])

_CHUNK = 1024 * 1024


def _validate_url(raw_url: str) -> str:
    """Validate a user-supplied URL, mapping rejections to HTTP errors."""
    settings = get_settings()
    if not settings.url_ingestion_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="URL ingestion is disabled on this server",
        )
    try:
        validated = validate_source_url(raw_url, allowed_hosts=settings.allowed_source_host_list)
    except BlockedSourceUrl as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except SourceUrlError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    return validated.url


def _attach_url_source(db: Session, project: Project, url: str) -> None:
    """Mark ``project`` as URL-sourced with a placeholder media asset.

    The asset path is left empty; the pipeline downloads the file during ingest
    and fills it in. Every other stage only ever sees a local path.
    """
    project.source_kind = "url"
    project.source_url = url
    project.status = ProjectStatus.created
    project.error = None

    asset = db.scalar(select(MediaAsset).where(MediaAsset.project_id == project.id))
    if asset is None:
        db.add(MediaAsset(project_id=project.id, path=""))
    else:
        # The source is being replaced, so drop whatever the asset pointed at and
        # leave the path empty. The ingest stage then re-downloads the new URL
        # instead of silently reusing the old media.
        if asset.path:
            Path(asset.path).unlink(missing_ok=True)
        asset.path = ""
        asset.size_bytes = 0


@router.post("", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
def create_project(payload: ProjectCreateIn, db: Session = Depends(get_db)) -> Project:
    project = Project(
        title=payload.title,
        source_kind="upload",
        license_note=payload.license_note,
        authorized_by=payload.authorized_by,
    )
    db.add(project)
    db.flush()
    if payload.source_url is not None:
        url = _validate_url(str(payload.source_url))
        _attach_url_source(db, project, url)
    db.commit()
    db.refresh(project)
    return project


@router.post("/source-url/probe", response_model=RemoteProbeOut)
def probe_source_url(payload: UrlIngestIn) -> RemoteProbeOut:
    """Fetch metadata for a URL without downloading the video.

    Lets a client show the title, uploader and duration, and confirm the link is
    supported, before committing to a download.
    """
    url = _validate_url(str(payload.source_url))
    downloader = get_downloader()
    try:
        info = downloader.probe(url)
    except DownloadError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return RemoteProbeOut(
        url=url,
        title=info.title,
        uploader=info.uploader,
        duration_seconds=info.duration_seconds,
        thumbnail=info.thumbnail,
        extractor=info.extractor,
        webpage_url=info.webpage_url,
        downloader=downloader.name,
    )


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


@router.post("/{project_id}/source-url", response_model=ProjectDetailOut)
def set_project_source_url(
    project_id: str,
    payload: UrlIngestIn,
    db: Session = Depends(get_db),
) -> Project:
    """Attach a remote video URL to an existing project.

    Mirrors ``/upload``: the file is fetched when the project is processed.
    """
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")

    url = _validate_url(str(payload.source_url))
    _attach_url_source(db, project, url)
    if payload.title:
        project.title = payload.title
    db.commit()
    db.refresh(project)
    logger.info("project.source_url_set", project_id=project.id, host=url.split("/")[2])
    return project


@router.post("/{project_id}/process", response_model=ProjectOut, status_code=202)
def process_project(project_id: str, db: Session = Depends(get_db)) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    if project.media is None:
        raise HTTPException(
            status_code=400,
            detail="provide a source first: upload a file or set a source URL",
        )
    if project.source_kind == "url" and not project.source_url:
        raise HTTPException(status_code=400, detail="project has no source URL to download")

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
    if project.media is not None and project.media.path:
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
