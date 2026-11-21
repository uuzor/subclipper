"""Subscription lifecycle: create, renew, revoke.

Separated from :mod:`quickreel.triggers.youtube` (which handles incoming
notifications) because the two run at different times for different reasons: the
lifecycle runs when an operator acts or when the renewal sweep fires, and the
ingest path runs when the hub calls in.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..logging_setup import get_logger
from ..models import ChannelSubscription, SubscriptionStatus
from .base import (
    HubClient,
    HubError,
    PermanentHubError,
    SubscriptionRequest,
    TransientHubError,
)
from .hub import callback_url_for, default_hub_client, topic_url_for
from .youtube import plan_lease_expiry

logger = get_logger(__name__)


def _request_for(subscription: ChannelSubscription) -> SubscriptionRequest:
    settings = get_settings()
    return SubscriptionRequest(
        topic_url=topic_url_for(subscription.channel_id),
        callback_url=callback_url_for(settings.youtube_callback_secret),
        secret=settings.youtube_callback_secret,
        lease_seconds=432000,
    )


def create_subscription(
    session: Session,
    *,
    workspace_id: str,
    channel_id: str,
    channel_title: str = "",
    authorized_by: str = "",
    license_note: str = "",
    auto_process: bool | None = None,
    hub: HubClient | None = None,
) -> ChannelSubscription:
    """Register a channel and ask the hub to watch it.

    The row is written first, in ``pending``, and only moved to ``active`` once
    the hub accepts. That ordering means a crash between the two leaves a
    visible pending row rather than a subscription that exists at the hub with
    no local record of it.
    """
    settings = get_settings()
    hub = hub or default_hub_client()

    existing = session.scalars(
        select(ChannelSubscription).where(
            ChannelSubscription.channel_id == channel_id,
            ChannelSubscription.workspace_id == workspace_id,
        )
    ).first()
    if existing is not None:
        # Re-subscribing is how a revoked channel is re-enabled, so treat it as
        # an update rather than an error.
        existing.authorized_by = authorized_by or existing.authorized_by
        existing.license_note = license_note or existing.license_note
        existing.channel_title = channel_title or existing.channel_title
        if auto_process is not None:
            existing.auto_process = auto_process
        subscription = existing
    else:
        subscription = ChannelSubscription(
            workspace_id=workspace_id,
            channel_id=channel_id,
            channel_title=channel_title,
            authorized_by=authorized_by,
            license_note=license_note,
            auto_process=(settings.youtube_auto_schedule if auto_process is None else auto_process),
            status=SubscriptionStatus.pending,
        )
        session.add(subscription)
        session.flush()

    result = hub.subscribe(_request_for(subscription))
    if result.ok:
        subscription.status = SubscriptionStatus.active
        subscription.status_detail = result.detail
        subscription.lease_expires_at = plan_lease_expiry(result.lease_seconds)
        subscription.last_verified_at = datetime.now(UTC)
    else:
        subscription.status = SubscriptionStatus.pending
        subscription.status_detail = result.detail or "hub did not accept the subscription"

    session.commit()
    logger.info(
        "trigger.subscription_created",
        channel_id=channel_id,
        status=str(subscription.status),
        hub=hub.name,
    )
    return subscription


def renew_due_subscriptions(
    session: Session,
    *,
    now: datetime | None = None,
    hub: HubClient | None = None,
    workspace_id: str | None = None,
) -> list[ChannelSubscription]:
    """Renew every active subscription whose lease is close to lapsing.

    Returns the subscriptions that were renewed. A failure marks the row
    ``expired`` with the reason, rather than deleting it, so the channel stays
    visible and can be re-enabled in one call.
    """
    now = now or datetime.now(UTC)
    hub = hub or default_hub_client()

    query = select(ChannelSubscription).where(
        ChannelSubscription.status == SubscriptionStatus.active,
        ChannelSubscription.lease_expires_at.is_not(None),
        ChannelSubscription.lease_expires_at <= now,
    )
    if workspace_id is not None:
        query = query.where(ChannelSubscription.workspace_id == workspace_id)
    due = list(session.scalars(query).all())

    renewed: list[ChannelSubscription] = []
    for subscription in due:
        try:
            result = hub.renew(_request_for(subscription))
        except (TransientHubError, PermanentHubError) as exc:
            subscription.status = SubscriptionStatus.expired
            subscription.status_detail = f"renewal failed: {exc}"
            logger.warning(
                "trigger.renewal_failed",
                channel_id=subscription.channel_id,
                reason=str(exc),
            )
            continue
        except HubError as exc:  # pragma: no cover - defensive
            subscription.status_detail = f"renewal error: {exc}"
            continue

        if result.ok:
            subscription.lease_expires_at = plan_lease_expiry(result.lease_seconds, now=now)
            subscription.status_detail = result.detail
            renewed.append(subscription)
        else:
            subscription.status = SubscriptionStatus.expired
            subscription.status_detail = result.detail or "hub did not renew"

    session.commit()
    return renewed


def revoke_subscription(
    session: Session, subscription: ChannelSubscription, *, hub: HubClient | None = None
) -> ChannelSubscription:
    """Stop notifications for a channel.

    The hub call is best-effort: if it fails, the local status is still set to
    revoked, because the intent is to stop processing this channel's uploads and
    a hub we cannot reach must not keep us processing them.
    """
    hub = hub or default_hub_client()
    try:
        hub.unsubscribe(_request_for(subscription))
    except HubError as exc:
        logger.warning(
            "trigger.unsubscribe_failed",
            channel_id=subscription.channel_id,
            reason=str(exc),
        )
        subscription.status_detail = f"local revoke; hub unsubscribe failed: {exc}"
    else:
        subscription.status_detail = "revoked"

    subscription.status = SubscriptionStatus.revoked
    subscription.lease_expires_at = None
    session.commit()
    return subscription
