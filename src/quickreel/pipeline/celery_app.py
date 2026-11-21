"""Celery application.

With ``QUICKREEL_TASK_ALWAYS_EAGER=true`` (the local default) tasks execute
in-process, so the whole pipeline runs without a broker. Point
``QUICKREEL_REDIS_URL`` at Redis and set ``task_always_eager=false`` to process
videos asynchronously.
"""

from __future__ import annotations

from celery import Celery

from ..config import get_settings

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
    # The scheduler is a sweep, not an event queue: every minute it publishes
    # whatever is due. ``run_due_posts`` claims each post atomically, so a slow
    # sweep overlapping the next one cannot double-post.
    beat_schedule={
        "publish-due-posts": {
            "task": "quickreel.run_due_posts",
            "schedule": 60.0,
        },
        # Hourly, not every minute: a WebSub lease is days long, and the renewal
        # itself is a network call to the hub.
        "renew-youtube-subscriptions": {
            "task": "quickreel.renew_youtube_subscriptions",
            "schedule": 3600.0,
        },
        # The performance feedback loop. Hourly is enough: platform counters are
        # cumulative, so this is how fast the curve is followed, and a slower
        # cadence keeps the outbound call volume low. The task itself checks
        # metrics_enabled and each post's own sample interval.
        "collect-metrics": {
            "task": "quickreel.collect_metrics",
            "schedule": 3600.0,
        },
    },
)
