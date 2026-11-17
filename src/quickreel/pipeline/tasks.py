"""Celery tasks wrapping the pipeline stages."""

from __future__ import annotations

from celery import shared_task

from ..db import session_scope
from ..logging_setup import get_logger
from .orchestrator import Pipeline

logger = get_logger(__name__)


@shared_task(name="quickreel.process_project", bind=True, max_retries=0)
def process_project(self, project_id: str) -> str:  # pragma: no cover - worker path
    logger.info("task.process_project.start", project_id=project_id)
    with session_scope() as session:
        Pipeline(session).run(project_id)
    logger.info("task.process_project.done", project_id=project_id)
    return project_id


@shared_task(name="quickreel.render_clip", bind=True, max_retries=0)
def render_clip(self, clip_id: str, burn_captions: bool = True) -> str:  # pragma: no cover
    from ..rendering.renderer import render_clip_by_id

    with session_scope() as session:
        render_clip_by_id(session, clip_id, burn_captions=burn_captions)
    return clip_id


def enqueue_project(project_id: str) -> None:
    """Dispatch processing, falling back to inline execution when eager."""
    process_project.delay(project_id)
