"""Turning a verified notification into a project.

This is the policy layer: given a notification for a video we are allowed to
process, decide whether to ingest it, and record the decision either way.

The rules are deliberately conservative. An unattended pipeline that downloads
and processes every video a channel posts spends real money, so the default is
to *record* a new video and wait for a human, with auto-processing as an opt-in
per subscription.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import get_settings
from ..logging_setup import get_logger
from ..media.downloader import DownloadError, get_downloader
from ..media.url_source import BlockedSourceUrl, SourceUrlError, validate_source_url
from ..models import (
    ChannelSubscription,
    MediaAsset,
    Project,
    ProjectStatus,
    SubscriptionStatus,
    YouTubeEvent,
    YouTubeEventStatus,
)
from .base import Notification

logger = get_logger(__name__)


@dataclass(slots=True)
class IngestDecision:
    """What happened to a notification, for the caller to report."""

    status: YouTubeEventStatus
    reason: str = ""
    project_id: str | None = None
    duplicate: bool = False


def find_subscription(session: Session, channel_id: str) -> ChannelSubscription | None:
    return session.scalars(
        select(ChannelSubscription).where(ChannelSubscription.channel_id == channel_id)
    ).first()


def _record_event(
    session: Session,
    notification: Notification,
    *,
    subscription: ChannelSubscription | None,
    status: YouTubeEventStatus,
    reason: str = "",
    project_id: str | None = None,
) -> YouTubeEvent:
    event = YouTubeEvent(
        subscription_id=subscription.id if subscription else None,
        channel_id=notification.channel_id,
        video_id=notification.video_id,
        video_title=notification.title,
        video_url=notification.canonical_url,
        status=status,
        reason=reason,
        project_id=project_id,
        payload=dict(notification.raw or {}),
        processed_at=datetime.now(UTC),
    )
    session.add(event)
    return event


def handle_notification(
    session: Session,
    notification: Notification,
    *,
    probe=None,
    auto_process: bool | None = None,
) -> IngestDecision:
    """Decide what to do with one notification, and record it.

    Order of checks is deliberate: cheap rejections (unknown channel, duplicate)
    come before anything that costs a network call or a pipeline run.

    ``probe`` is injectable so a test can supply video metadata without a
    download. When it is None the real downloader is used, and a probe failure
    is recorded rather than raised: the hub does not retry on our behalf, and a
    single unavailable video must not take down the endpoint.
    """
    settings = get_settings()

    subscription = find_subscription(session, notification.channel_id)
    if subscription is None:
        # A notification for a channel we did not subscribe to. Either the
        # secret leaked or the hub is confused; either way, do not process it.
        _record_event(
            session,
            notification,
            subscription=None,
            status=YouTubeEventStatus.ignored,
            reason="no subscription for this channel",
        )
        session.commit()
        logger.warning(
            "trigger.unknown_channel",
            channel_id=notification.channel_id,
            video_id=notification.video_id,
        )
        return IngestDecision(YouTubeEventStatus.ignored, "no subscription for this channel")

    if subscription.status == SubscriptionStatus.revoked:
        _record_event(
            session,
            notification,
            subscription=subscription,
            status=YouTubeEventStatus.ignored,
            reason="subscription revoked",
        )
        session.commit()
        return IngestDecision(YouTubeEventStatus.ignored, "subscription revoked")

    # Idempotency. WebSub permits duplicate delivery, so the same video can
    # arrive more than once. A prior event for this (channel, video) means we
    # have already decided; do not decide again.
    existing = session.scalars(
        select(YouTubeEvent).where(
            YouTubeEvent.channel_id == notification.channel_id,
            YouTubeEvent.video_id == notification.video_id,
        )
    ).first()
    if existing is not None:
        logger.info(
            "trigger.duplicate_notification",
            channel_id=notification.channel_id,
            video_id=notification.video_id,
            original_status=str(existing.status),
        )
        return IngestDecision(
            YouTubeEventStatus.ignored,
            f"already handled as {existing.status}",
            project_id=existing.project_id,
            duplicate=True,
        )

    # Validate the video URL before anything is fetched. It came from an
    # untrusted feed, so it goes through the same SSRF gate a user URL does.
    try:
        validated = validate_source_url(
            notification.canonical_url, allowed_hosts=settings.allowed_source_host_list
        )
    except (BlockedSourceUrl, SourceUrlError) as exc:
        _record_event(
            session,
            notification,
            subscription=subscription,
            status=YouTubeEventStatus.failed,
            reason=f"source URL rejected: {exc}",
        )
        session.commit()
        logger.warning(
            "trigger.source_url_rejected", video_id=notification.video_id, reason=str(exc)
        )
        return IngestDecision(YouTubeEventStatus.failed, f"source URL rejected: {exc}")

    # Probe for duration and title. This is where a video that is too short to
    # clip, or a Short, is filtered out before any project exists.
    if probe is None:
        probe = get_downloader().probe

    try:
        info = probe(validated.url)
    except DownloadError as exc:
        _record_event(
            session,
            notification,
            subscription=subscription,
            status=YouTubeEventStatus.failed,
            reason=f"probe failed: {exc}",
        )
        session.commit()
        logger.warning("trigger.probe_failed", video_id=notification.video_id, reason=str(exc))
        return IngestDecision(YouTubeEventStatus.failed, f"probe failed: {exc}")

    duration = float(getattr(info, "duration_seconds", 0.0) or 0.0)
    if duration and duration < settings.youtube_min_duration_seconds:
        reason = (
            f"video is {duration:.0f}s, shorter than the "
            f"{settings.youtube_min_duration_seconds}s minimum"
        )
        _record_event(
            session,
            notification,
            subscription=subscription,
            status=YouTubeEventStatus.ignored,
            reason=reason,
        )
        session.commit()
        return IngestDecision(YouTubeEventStatus.ignored, reason)

    if settings.youtube_skip_shorts and _looks_like_short(validated.url, getattr(info, "raw", {})):
        reason = "video is a YouTube Short; it is already short-form"
        _record_event(
            session,
            notification,
            subscription=subscription,
            status=YouTubeEventStatus.ignored,
            reason=reason,
        )
        session.commit()
        return IngestDecision(YouTubeEventStatus.ignored, reason)

    # Whether this video is processed now or left as a draft. The per-call
    # argument wins, then the subscription's own setting. Auto-processing runs
    # the pipeline unattended, so it is opt-in on both.
    effective_auto = subscription.auto_process if auto_process is None else auto_process

    project = Project(
        title=notification.title or getattr(info, "title", "") or "Untitled project",
        source_kind="channel_subscription",
        source_url=validated.url,
        external_video_id=notification.video_id,
        # Provenance carries the channel's authorisation forward to every clip
        # rendered from this project.
        license_note=subscription.license_note,
        authorized_by=subscription.authorized_by,
        status=ProjectStatus.created,
    )
    session.add(project)
    session.flush()
    session.add(MediaAsset(project_id=project.id, path=""))

    reason = "project queued for processing" if effective_auto else "project created for review"

    # The unique constraint is the real guard against a race between two
    # deliveries arriving at once; the lookup above is the fast path.
    _record_event(
        session,
        notification,
        subscription=subscription,
        status=YouTubeEventStatus.queued,
        reason=reason,
        project_id=project.id,
    )
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        logger.info("trigger.concurrent_duplicate", video_id=notification.video_id)
        return IngestDecision(
            YouTubeEventStatus.ignored, "duplicate delivered concurrently", duplicate=True
        )

    if effective_auto:
        # Imported here: the pipeline imports the models the triggers package
        # already depends on, and a module-level import would close the loop.
        from ..pipeline.tasks import enqueue_project

        enqueue_project(project.id)

    logger.info(
        "trigger.project_created",
        project_id=project.id,
        video_id=notification.video_id,
        channel_id=notification.channel_id,
        duration=duration,
        auto_process=effective_auto,
    )
    return IngestDecision(YouTubeEventStatus.queued, reason, project_id=project.id)


def _looks_like_short(url: str, raw: dict) -> bool:
    """Heuristic for "this is already a Short".

    There is no reliable API field for this, so the URL shape is checked
    (``/shorts/<id>``) and, when the downloader exposes it, a duration under
    three minutes with a vertical aspect ratio is treated as one.
    """
    if "/shorts/" in url:
        return True
    duration = raw.get("duration") if isinstance(raw, dict) else None
    width = raw.get("width") if isinstance(raw, dict) else None
    height = raw.get("height") if isinstance(raw, dict) else None
    if isinstance(duration, (int, float)) and duration and duration <= 180:
        if isinstance(width, int) and isinstance(height, int) and height > width:
            return True
    return False


def plan_lease_expiry(lease_seconds: int, *, now: datetime | None = None) -> datetime:
    """When a lease granted now should be renewed.

    The renewal sweep fires before the lease lapses, with a margin, so a slow
    sweep does not let it expire between checks.
    """
    now = now or datetime.now(UTC)
    margin = get_settings().youtube_renew_before_seconds
    effective = max(lease_seconds - margin, 60)
    return now + timedelta(seconds=effective)
