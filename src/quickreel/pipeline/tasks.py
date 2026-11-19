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
def render_clip(
    self, clip_id: str, burn_captions: bool = True, vertical: bool = True
) -> str:  # pragma: no cover - worker path
    from ..rendering.renderer import render_clip_by_id

    with session_scope() as session:
        render_clip_by_id(
            session, clip_id, burn_captions=burn_captions, vertical=vertical
        )
    return clip_id


def enqueue_project(project_id: str) -> None:
    """Dispatch processing, falling back to inline execution when eager."""
    process_project.delay(project_id)


def enqueue_render(
    clip_id: str, *, burn_captions: bool = True, vertical: bool = True
) -> None:
    render_clip.delay(clip_id, burn_captions=burn_captions, vertical=vertical)


@shared_task(name="quickreel.run_due_posts", bind=True, max_retries=0)
def run_due_posts_task(self) -> dict:  # pragma: no cover - worker path
    """Publish every scheduled post whose time has come.

    Driven by Celery beat. Safe to run concurrently: the scheduler claims each
    post atomically, so overlapping runs cannot double-publish.
    """
    from ..publishing.scheduler import run_due_posts

    with session_scope() as session:
        outcomes = run_due_posts(session)
    published = sum(1 for o in outcomes if o.status == "published")
    logger.info("task.run_due_posts.done", examined=len(outcomes), published=published)
    return {
        "examined": len(outcomes),
        "published": published,
        "failed": sum(1 for o in outcomes if o.status == "failed"),
    }


@shared_task(name="quickreel.collect_metrics", bind=True, max_retries=0)
def collect_metrics_task(self) -> dict:  # pragma: no cover - worker path
    """Sample engagement counters for every published post that is due.

    Driven by Celery beat. Safe to run concurrently: a duplicate sample is a
    repeated point on a time series, not a corrupted value, and the cadence
    check reads the newest sample so a post is not re-sampled too soon.
    """
    from ..publishing.metrics import run_metrics_collection

    with session_scope() as session:
        outcomes = run_metrics_collection(session)
    sampled = sum(1 for o in outcomes if o.status == "sampled")
    failed = sum(1 for o in outcomes if o.status == "failed")
    logger.info("task.collect_metrics.done", sampled=sampled, failed=failed)
    return {"examined": len(outcomes), "sampled": sampled, "failed": failed}


@shared_task(name="quickreel.renew_youtube_subscriptions", bind=True, max_retries=0)
def renew_youtube_subscriptions_task(self) -> dict:  # pragma: no cover - worker path
    """Renew WebSub leases before they lapse.

    Driven by Celery beat. A lease that lapses is silent: the hub simply stops
    calling, and new uploads are never seen again. This sweep is what makes the
    trigger durable rather than a one-off.
    """
    from ..triggers.service import renew_due_subscriptions

    with session_scope() as session:
        renewed = renew_due_subscriptions(session)
    logger.info("task.renew_youtube_subscriptions.done", renewed=len(renewed))
    return {"renewed": len(renewed), "channel_ids": [s.channel_id for s in renewed]}
