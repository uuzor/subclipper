"""The performance feedback loop.

Every published post is re-sampled on a cadence and the platform's counters are
written to ``clip_metrics``. This is the only part of the system that gets
*harder* for a competitor to copy over time: the schedule, the adapters and the
scoring weights are all reproducible from the outside, but a year of per-niche
engagement history is not.

Two rules shape the design.

**A sampling failure is never fatal.** The loop runs unattended against third
party APIs that rate-limit and time out. One adapter raising must not stop the
other posts in the batch from being sampled, so every failure is recorded on the
outcome and the sweep continues.

**A dead credential is surfaced, not retried.** An ``AuthError`` means the
stored token is gone; the target is flagged so the UI asks for a reconnect
instead of the loop failing the same way forever.

Sampling is deliberately idempotent-ish rather than exactly-once: metrics are a
time series, so two overlapping sweeps producing two rows near the same instant
costs a duplicate sample, not a corrupted value. The cadence check reads the
latest sample's timestamp, so a post is not re-sampled until the interval has
elapsed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..logging_setup import get_logger
from ..models import (
    ClipMetric,
    Platform,
    PublishTarget,
    PublishTargetStatus,
    ScheduledPost,
    ScheduledPostStatus,
)
from .base import AdapterError, AuthError, PlatformAdapter
from .registry import get_adapter

logger = get_logger(__name__)

# Only published posts with a platform-side id can be sampled: without the id
# there is nothing to ask the platform about.
_SAMPLEABLE = ScheduledPostStatus.published


@dataclass(slots=True)
class MetricOutcome:
    """What happened to one post's sampling attempt."""

    post_id: str
    status: str  # sampled | skipped | failed
    detail: str = ""
    metric_id: str | None = None


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _as_int(value: object) -> int:
    try:
        return int(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _as_float(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def due_for_metrics(
    session: Session,
    *,
    now: datetime | None = None,
    limit: int = 200,
    workspace_id: str | None = None,
) -> list[str]:
    """Ids of published posts that are ready for a fresh performance sample.

    A post is due when it is published, carries a platform-side id, is still
    inside the sampling window (old clips stop mattering), belongs to an active
    target, and either has no sample yet or its newest sample is older than the
    configured interval.

    ``workspace_id`` narrows the sweep to one tenant, the same way the publish
    scheduler does; the periodic worker leaves it None so one pass serves every
    workspace.
    """
    settings = get_settings()
    now = now or _utcnow()
    interval = timedelta(seconds=settings.metrics_sample_interval_seconds)
    oldest = now - timedelta(days=settings.metrics_max_age_days)

    latest = (
        select(ClipMetric.post_id, func.max(ClipMetric.captured_at).label("last_sample"))
        .group_by(ClipMetric.post_id)
        .subquery()
    )

    query = (
        select(ScheduledPost.id)
        .join(PublishTarget, PublishTarget.id == ScheduledPost.target_id)
        .outerjoin(latest, latest.c.post_id == ScheduledPost.id)
        .where(
            ScheduledPost.status == _SAMPLEABLE,
            ScheduledPost.external_post_id != "",
            ScheduledPost.published_at.is_not(None),
            ScheduledPost.published_at >= oldest,
            PublishTarget.status == PublishTargetStatus.active,
            or_(
                latest.c.last_sample.is_(None),
                latest.c.last_sample <= now - interval,
            ),
        )
        .order_by(ScheduledPost.published_at)
        .limit(limit)
    )
    if workspace_id is not None:
        query = query.where(PublishTarget.workspace_id == workspace_id)
    return list(session.scalars(query).all())


def collect_metrics_for_post(
    session: Session, post_id: str, *, adapter: PlatformAdapter | None = None
) -> MetricOutcome:
    """Sample one published post and record the result.

    Never raises for a platform failure: the failure is logged and returned so a
    sweep of many posts is not aborted by one of them.
    """
    post = session.get(ScheduledPost, post_id)
    if post is None:
        return MetricOutcome(post_id=post_id, status="skipped", detail="post disappeared")

    if post.status != _SAMPLEABLE:
        return MetricOutcome(
            post_id=post_id, status="skipped", detail=f"post is {post.status}, not published"
        )
    if not post.external_post_id:
        return MetricOutcome(post_id=post_id, status="skipped", detail="post has no external id")

    target = session.get(PublishTarget, post.target_id)
    if target is None:
        return MetricOutcome(post_id=post_id, status="skipped", detail="target no longer exists")
    if target.status != PublishTargetStatus.active:
        return MetricOutcome(
            post_id=post_id,
            status="skipped",
            detail=f"target is {target.status}, not active",
        )

    if adapter is None:
        try:
            adapter = get_adapter(str(target.platform))
        except KeyError as exc:
            return MetricOutcome(post_id=post_id, status="failed", detail=str(exc))

    try:
        counters = adapter.fetch_metrics(dict(target.credentials or {}), post.external_post_id)
    except AuthError as exc:
        target.status = PublishTargetStatus.needs_reauth
        target.status_detail = str(exc)
        session.commit()
        return MetricOutcome(
            post_id=post_id, status="failed", detail=f"authentication failed: {exc}"
        )
    except AdapterError as exc:
        return MetricOutcome(post_id=post_id, status="failed", detail=str(exc))
    except Exception as exc:  # noqa: BLE001 - a buggy adapter must not kill the sweep
        return MetricOutcome(post_id=post_id, status="failed", detail=f"unexpected error: {exc}")

    metric = ClipMetric(
        post_id=post.id,
        platform=target.platform if isinstance(target.platform, Platform) else Platform(
            str(target.platform)
        ),
        views=_as_int(counters.get("views")),
        likes=_as_int(counters.get("likes")),
        comments=_as_int(counters.get("comments")),
        shares=_as_int(counters.get("shares")),
        average_watch_seconds=_as_float(counters.get("average_watch_seconds")),
    )
    session.add(metric)
    session.commit()
    logger.info(
        "metrics.sampled",
        post_id=post.id,
        platform=str(target.platform),
        views=metric.views,
        likes=metric.likes,
    )
    return MetricOutcome(post_id=post.id, status="sampled", metric_id=metric.id)


def run_metrics_collection(
    session: Session,
    *,
    now: datetime | None = None,
    limit: int = 200,
    workspace_id: str | None = None,
) -> list[MetricOutcome]:
    """Sample every post that is due. The entry point a worker calls."""
    if not get_settings().metrics_enabled:
        return []
    outcomes: list[MetricOutcome] = []
    for post_id in due_for_metrics(session, now=now, limit=limit, workspace_id=workspace_id):
        outcomes.append(collect_metrics_for_post(session, post_id))
    return outcomes


def clip_performance_summary(session: Session, clip_id: str) -> dict:
    """Aggregate every sample for a clip into the shape the learning loop reads.

    Reports the latest sample per platform plus lifetime totals, which is what a
    recalibration pass needs: the totals smooth out sampling noise, and the
    latest sample shows where a clip is now.
    """
    samples = list(
        session.scalars(
            select(ClipMetric)
            .join(ScheduledPost, ScheduledPost.id == ClipMetric.post_id)
            .where(ScheduledPost.clip_id == clip_id)
            .order_by(ClipMetric.captured_at)
        ).all()
    )
    if not samples:
        return {"sample_count": 0, "platforms": {}, "totals": {}}

    totals = {"views": 0, "likes": 0, "comments": 0, "shares": 0}
    latest_by_platform: dict[str, ClipMetric] = {}
    for sample in samples:
        latest_by_platform[str(sample.platform)] = sample

    # Totals use the newest sample per platform, not the sum across samples:
    # counters are cumulative on the platform, so summing the series would count
    # the same view once per sweep.
    for sample in latest_by_platform.values():
        totals["views"] += sample.views
        totals["likes"] += sample.likes
        totals["comments"] += sample.comments
        totals["shares"] += sample.shares

    return {
        "sample_count": len(samples),
        "platforms": {
            name: {
                "views": sample.views,
                "likes": sample.likes,
                "comments": sample.comments,
                "shares": sample.shares,
                "average_watch_seconds": sample.average_watch_seconds,
                "captured_at": sample.captured_at.isoformat(),
            }
            for name, sample in latest_by_platform.items()
        },
        "totals": totals,
    }
