"""Celery application.

With ``QUICKREEL_TASK_ALWAYS_EAGER=true`` (the local default) tasks execute
in-process, so the whole pipeline runs without a broker. Point
``QUICKREEL_REDIS_URL`` at Redis and set ``task_always_eager=false`` to process
videos asynchronously.
"""

from __future__ import annotations

from celery import Celery

from .config import get_settings

_settings = get_settings()

celery_app = Celery(
    "quickreel",
    broker=_settings.redis_url or "memory://",
    backend=_settings.redis_url or "cache+memory://",
    include=["quickreel.pipeline.tasks"],
)

celery_app.conf.update(
    task_always_eager=_settings.task_always_eager,
    task_eager_propagates=True,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_track_started=True,
    worker_max_tasks_per_child=50,
    broker_connection_retry_on_startup=True,
)
