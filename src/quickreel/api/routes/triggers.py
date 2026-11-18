"""Trigger endpoints: the WebSub callback and subscription management.

The callback is a public, unauthenticated endpoint: the hub calls it from
outside, and anyone who learns the URL can POST to it. Its defences, in order:

1. **A shared secret in the path.** WebSub does not sign notifications, so a
   secret path segment is what separates a real hub call from a stranger's.
2. **An optional signature check.** If ``QUICKREEL_YOUTUBE_SIGNING_SECRET`` is
   set, an ``X-Hub-Signature`` HMAC is required. This is opt-in because the
   public PubSubHubbub hub does not sign by default.
3. **A subscription allowlist.** A notification for a channel we did not
   subscribe to is ignored, so a leaked secret still does not cause processing
   of arbitrary channels.
4. **A validated feed.** Ids and URLs are shape-checked before use.

Every one of these fails closed. The endpoint still answers 2xx to a rejected
notification, because the hub cannot fix a bad notification by retrying it, and
a 4xx would make the hub disable the subscription.
"""

from __future__ import annotations

import hashlib
import hmac

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...config import get_settings
from ...db import get_db
from ...logging_setup import get_logger
from ...media.downloader import DownloadError, get_downloader
from ...models import ChannelSubscription, SubscriptionStatus, Workspace
from ...schemas import (
    ChannelSubscriptionIn,
    ChannelSubscriptionOut,
    SubscriptionRenewOut,
    YouTubeEventOut,
)
from ...triggers import (
    NotificationParseError,
    parse_notification,
)
from ...triggers.hub import default_hub_client
from ...triggers.service import create_subscription, renew_due_subscriptions, revoke_subscription
from ...triggers.youtube import handle_notification
from ..deps import current_workspace, scoped_or_404

logger = get_logger(__name__)
router = APIRouter(prefix="/api/v1/triggers", tags=["triggers"])


def _verify_signature(body: bytes, header: str | None) -> bool:
    """Check the hub's HMAC, when a signing secret is configured."""
    secret = get_settings().youtube_signing_secret.strip()
    if not secret:
        # Not configured: the secret path segment is the authentication.
        return True
    if not header:
        return False
    expected = "sha1=" + hmac.new(secret.encode(), body, hashlib.sha1).hexdigest()
    return hmac.compare_digest(expected, header.strip())


@router.get("/youtube/notify")
@router.get("/youtube/notify/{secret}")
def verify(
    request: Request,
    secret: str = "",
    hub_mode: str = "",
    hub_topic: str = "",
    hub_challenge: str = "",
    hub_lease_seconds: str = "",
    hub_reason: str = "",
    db: Session = Depends(get_db),
) -> Response:
    """Answer the hub's verification request.

    WebSub verification is a GET with a challenge that must be echoed back
    verbatim. ``hub.mode=denied`` means the hub refused the subscription, which
    is reported to the operator through the subscription's status.
    """
    if not _secret_ok(secret):
        raise HTTPException(status_code=404, detail="not found")

    mode = hub_mode or request.query_params.get("hub.mode", "")
    challenge = hub_challenge or request.query_params.get("hub.challenge", "")

    if mode == "denied":
        logger.warning("trigger.subscription_denied", topic=hub_topic, reason=hub_reason)
        subscription = _subscription_for_topic(db, hub_topic)
        if subscription is not None:
            subscription.status = SubscriptionStatus.pending
            subscription.status_detail = f"hub denied the subscription: {hub_reason}"
            db.commit()
        # A denial still requires the challenge to be echoed, or the hub retries.
        return Response(content=challenge, media_type="text/plain")

    if not challenge:
        raise HTTPException(status_code=400, detail="missing hub.challenge")

    logger.info(
        "trigger.subscription_verified",
        mode=mode,
        topic=hub_topic,
        lease_seconds=hub_lease_seconds,
    )
    return Response(content=challenge, media_type="text/plain")


def _secret_ok(provided: str) -> bool:
    """Whether the callback path carried the expected shared secret.

    When no secret is configured (local development) the check passes. In
    production the production guard requires it to be set. Compared in constant
    time so the value cannot be recovered by timing.
    """
    expected = get_settings().youtube_callback_secret.strip()
    if not expected:
        return True
    return hmac.compare_digest(provided, expected)


def _subscription_for_topic(db: Session, topic: str) -> ChannelSubscription | None:
    """Map a topic feed URL back to the subscription it belongs to."""
    if not topic or "channel_id=" not in topic:
        return None
    channel_id = topic.split("channel_id=", 1)[1].split("&", 1)[0]
    return db.scalars(
        select(ChannelSubscription).where(ChannelSubscription.channel_id == channel_id)
    ).first()


@router.post("/youtube/notify")
@router.post("/youtube/notify/{secret}")
async def notify(
    request: Request,
    secret: str = "",
    db: Session = Depends(get_db),
) -> Response:
    """Receive a notification that a channel published a video."""
    # The secret path segment, when configured, must match.
    if not _secret_ok(secret):
        logger.warning("trigger.callback_secret_mismatch", path=request.url.path)
        raise HTTPException(status_code=404, detail="not found")

    body = await request.body()
    if not _verify_signature(body, request.headers.get("X-Hub-Signature")):
        logger.warning("trigger.signature_mismatch")
        raise HTTPException(status_code=403, detail="signature verification failed")

    try:
        notification = parse_notification(body)
    except NotificationParseError as exc:
        # The hub cannot fix a malformed notification by retrying, so accept it
        # and log. A 4xx would make the hub mark the subscription unhealthy.
        logger.warning("trigger.unparseable_notification", reason=str(exc))
        return Response(status_code=204)

    try:
        decision = handle_notification(db, notification, probe=get_downloader().probe)
    except DownloadError as exc:  # pragma: no cover - probe path already guards
        logger.warning("trigger.ingest_failed", video_id=notification.video_id, reason=str(exc))
        return Response(status_code=204)

    logger.info(
        "trigger.notification_handled",
        video_id=notification.video_id,
        channel_id=notification.channel_id,
        status=str(decision.status),
        project_id=decision.project_id,
    )
    # 2xx always: the notification was received. Whether it produced a project
    # is recorded in the event log, not signalled through the status code.
    return Response(status_code=204)


@router.post("/youtube/subscriptions", response_model=ChannelSubscriptionOut, status_code=201)
def subscribe(
    payload: ChannelSubscriptionIn,
    db: Session = Depends(get_db),
    workspace: Workspace = Depends(current_workspace),
) -> ChannelSubscription:
    subscription = create_subscription(
        db,
        workspace_id=workspace.id,
        channel_id=payload.channel_id,
        channel_title=payload.channel_title,
        authorized_by=payload.authorized_by,
        license_note=payload.license_note,
        auto_process=payload.auto_process,
    )
    return subscription


@router.get("/youtube/subscriptions", response_model=list[ChannelSubscriptionOut])
def list_subscriptions(
    db: Session = Depends(get_db), workspace: Workspace = Depends(current_workspace)
) -> list[ChannelSubscription]:
    return list(
        db.scalars(
            select(ChannelSubscription)
            .where(ChannelSubscription.workspace_id == workspace.id)
            .order_by(ChannelSubscription.created_at)
        ).all()
    )


@router.delete("/youtube/subscriptions/{subscription_id}", status_code=204)
def delete_subscription(
    subscription_id: str,
    db: Session = Depends(get_db),
    workspace: Workspace = Depends(current_workspace),
) -> None:
    subscription = scoped_or_404(
        db, ChannelSubscription, subscription_id, workspace.id, "subscription"
    )
    revoke_subscription(db, subscription)


@router.post("/youtube/subscriptions/renew", response_model=SubscriptionRenewOut)
def renew(
    db: Session = Depends(get_db), workspace: Workspace = Depends(current_workspace)
) -> SubscriptionRenewOut:
    """Renew the caller's lapsed or soon-to-lapse leases.

    The periodic worker calls :func:`renew_due_subscriptions` directly with no
    workspace, so one sweep serves everyone; this endpoint is the per-tenant
    "renew mine now" button.
    """
    renewed = renew_due_subscriptions(db, workspace_id=workspace.id)
    return SubscriptionRenewOut(renewed=len(renewed), channel_ids=[s.channel_id for s in renewed])


@router.get("/youtube/events", response_model=list[YouTubeEventOut])
def list_events(
    db: Session = Depends(get_db), workspace: Workspace = Depends(current_workspace)
) -> list:
    """Recent notifications for the caller's channels only.

    Events are reached through their subscription, which is what carries the
    workspace. An event with no subscription (an unknown channel we ignored) has
    no tenant and is therefore never shown to anyone.
    """
    from ...models import YouTubeEvent

    query = (
        select(YouTubeEvent)
        .join(ChannelSubscription, ChannelSubscription.id == YouTubeEvent.subscription_id)
        .where(ChannelSubscription.workspace_id == workspace.id)
        .order_by(YouTubeEvent.received_at.desc())
        .limit(200)
    )
    return list(db.scalars(query).all())


@router.get("/youtube/hub")
def hub_info(workspace: Workspace = Depends(current_workspace)) -> dict:
    """What the hub integration needs, so an operator can see the callback URL."""
    settings = get_settings()
    from ...triggers.hub import callback_url_for

    client = default_hub_client()
    return {
        "hub_url": settings.youtube_hub_url,
        "callback_url": callback_url_for(settings.youtube_callback_secret),
        "client": client.name,
        "signing_enabled": bool(settings.youtube_signing_secret.strip()),
        "secret_configured": bool(settings.youtube_callback_secret.strip()),
    }
