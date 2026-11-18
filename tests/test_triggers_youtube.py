"""Subscription lifecycle and ingest policy.

These drive the real service against a real SQLite session, with the
deterministic mock hub and an injected probe, so the whole subscribe -> notify
-> project path runs without network access or a download.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from quickreel.models import (
    ChannelSubscription,
    SubscriptionStatus,
    YouTubeEvent,
    YouTubeEventStatus,
)
from quickreel.triggers import Notification
from quickreel.triggers.hub import MockHubClient
from quickreel.triggers.service import (
    create_subscription,
    renew_due_subscriptions,
    revoke_subscription,
)
from quickreel.triggers.youtube import handle_notification, plan_lease_expiry

CHANNEL = "UCuAXFkgsw1L7xaCfnd5JJOw"
OTHER_CHANNEL = "UC1234567890123456789012"
VIDEO = "dQw4w9WgXcQ"


class _ProbeInfo:
    """Stands in for the downloader's RemoteVideoInfo."""

    def __init__(
        self, duration: float = 3600.0, title: str = "Long video", raw: dict | None = None
    ):
        self.duration_seconds = duration
        self.title = title
        self.raw = raw or {}


@pytest.fixture(autouse=True)
def _clean_trigger_tables(db_session):
    """Clear trigger rows before each test.

    ``handle_notification`` looks up prior events for idempotency, so rows left
    by an earlier test would make a fresh notification look like a duplicate.
    """
    from sqlalchemy import delete

    from quickreel.models import Project

    for model in (YouTubeEvent, ChannelSubscription):
        db_session.execute(delete(model))
    db_session.execute(delete(Project))
    db_session.commit()
    yield


def _notification(video_id: str = VIDEO, channel_id: str = CHANNEL) -> Notification:
    return Notification(
        channel_id=channel_id,
        video_id=video_id,
        title="A new video",
        video_url=f"https://www.youtube.com/watch?v={video_id}",
    )


def test_create_subscription_records_the_lease(db_session):
    hub = MockHubClient(lease_seconds=3600)
    subscription = create_subscription(
        db_session,
        channel_id=CHANNEL,
        channel_title="Some Channel",
        authorized_by="owner@example.com",
        license_note="I own this channel",
        hub=hub,
    )
    assert subscription.status == SubscriptionStatus.active
    assert subscription.lease_expires_at is not None
    assert len(hub.subscriptions) == 1
    # The topic the hub was asked to watch names the channel.
    assert CHANNEL in hub.subscriptions[0].topic_url


def test_create_subscription_marks_pending_when_the_hub_rejects(db_session):
    hub = MockHubClient(status_code=403)
    subscription = create_subscription(db_session, channel_id=CHANNEL, hub=hub)
    assert subscription.status == SubscriptionStatus.pending
    assert subscription.status_detail


def test_resubscribing_updates_rather_than_duplicates(db_session):
    hub = MockHubClient()
    create_subscription(db_session, channel_id=CHANNEL, authorized_by="first", hub=hub)
    second = create_subscription(db_session, channel_id=CHANNEL, authorized_by="second", hub=hub)
    assert db_session.query(ChannelSubscription).count() == 1
    assert second.authorized_by == "second"


def test_renewal_only_touches_due_leases(db_session):
    hub = MockHubClient(lease_seconds=100)
    subscription = create_subscription(db_session, channel_id=CHANNEL, hub=hub)

    # Not yet due.
    assert renew_due_subscriptions(db_session, hub=hub) == []

    # Force the lease into the past.
    subscription.lease_expires_at = datetime.now(UTC) - timedelta(minutes=1)
    db_session.commit()

    renewed = renew_due_subscriptions(db_session, hub=hub)
    assert [s.channel_id for s in renewed] == [CHANNEL]
    assert len(hub.renewals) == 1


def test_a_failed_renewal_marks_the_subscription_expired(db_session):
    hub = MockHubClient()
    subscription = create_subscription(db_session, channel_id=CHANNEL, hub=hub)
    subscription.lease_expires_at = datetime.now(UTC) - timedelta(minutes=1)
    db_session.commit()

    failing_hub = MockHubClient(status_code=500)
    renew_due_subscriptions(db_session, hub=failing_hub)
    db_session.refresh(subscription)
    assert subscription.status == SubscriptionStatus.expired


def test_revoke_sets_status_even_when_the_hub_fails(db_session):
    """A hub we cannot reach must not keep us processing the channel."""
    hub = MockHubClient()
    subscription = create_subscription(db_session, channel_id=CHANNEL, hub=hub)

    class _BrokenHub(MockHubClient):
        def unsubscribe(self, request):
            from quickreel.triggers.base import TransientHubError

            raise TransientHubError("hub down")

    revoke_subscription(db_session, subscription, hub=_BrokenHub())
    assert subscription.status == SubscriptionStatus.revoked
    assert "unsubscribe failed" in subscription.status_detail


def test_lease_renewal_is_planned_before_expiry(db_session):
    """The renewal sweep must fire before the lease lapses, not after."""
    now = datetime.now(UTC)
    expires = plan_lease_expiry(100000, now=now)
    assert expires < now + timedelta(seconds=100000)


def test_notification_for_an_unknown_channel_is_ignored(db_session):
    decision = handle_notification(db_session, _notification(channel_id=OTHER_CHANNEL))
    assert decision.status == YouTubeEventStatus.ignored
    assert "no subscription" in decision.reason
    # The rejection is still recorded, so it is auditable.
    assert db_session.query(YouTubeEvent).count() == 1


def test_notification_creates_a_project_for_review(db_session):
    create_subscription(db_session, channel_id=CHANNEL, hub=MockHubClient())
    decision = handle_notification(
        db_session, _notification(), probe=lambda url: _ProbeInfo(duration=3600)
    )
    assert decision.status == YouTubeEventStatus.queued
    assert decision.project_id

    from quickreel.models import Project

    project = db_session.get(Project, decision.project_id)
    assert project.source_kind == "channel_subscription"
    assert project.external_video_id == VIDEO
    # Provenance is carried from the subscription onto the project.
    assert project.authorized_by == ""


def test_provenance_flows_from_subscription_to_project(db_session):
    create_subscription(
        db_session,
        channel_id=CHANNEL,
        authorized_by="owner@example.com",
        license_note="I own this channel",
        hub=MockHubClient(),
    )
    decision = handle_notification(
        db_session, _notification(), probe=lambda url: _ProbeInfo(duration=3600)
    )
    from quickreel.models import Project

    project = db_session.get(Project, decision.project_id)
    assert project.authorized_by == "owner@example.com"
    assert project.license_note == "I own this channel"


def test_review_is_the_default_and_processing_is_opt_in(db_session):
    """A notification records a project for review; it does not spend money.

    Auto-processing runs the whole pipeline unattended, so the default must be
    the cheap path. This asserts the decision reason distinguishes the two, so
    an operator can tell from the event log which mode a video was ingested in.
    """
    create_subscription(db_session, channel_id=CHANNEL, hub=MockHubClient())
    decision = handle_notification(
        db_session, _notification(), probe=lambda url: _ProbeInfo(duration=3600)
    )
    assert "review" in decision.reason


def test_auto_process_on_the_subscription_queues_processing(db_session):
    """With auto_process set, the project is dispatched to the pipeline."""
    from quickreel.triggers.youtube import handle_notification as handle

    create_subscription(db_session, channel_id=CHANNEL, auto_process=True, hub=MockHubClient())
    dispatched: list[str] = []

    # Stub the enqueue seam rather than let a real task run: the point is that
    # ingest asks for processing, not what the pipeline does with it (that is
    # covered by the pipeline tests).
    import quickreel.pipeline.tasks as tasks

    original = tasks.enqueue_project
    tasks.enqueue_project = dispatched.append
    try:
        decision = handle(db_session, _notification(), probe=lambda url: _ProbeInfo(duration=3600))
    finally:
        tasks.enqueue_project = original

    assert decision.project_id in dispatched


def test_a_duplicate_notification_does_not_create_a_second_project(db_session):
    """WebSub permits duplicate delivery, so this is the common case, not an edge."""
    create_subscription(db_session, channel_id=CHANNEL, hub=MockHubClient())
    first = handle_notification(
        db_session, _notification(), probe=lambda url: _ProbeInfo(duration=3600)
    )
    second = handle_notification(
        db_session, _notification(), probe=lambda url: _ProbeInfo(duration=3600)
    )
    assert second.duplicate is True
    assert second.project_id == first.project_id

    from quickreel.models import Project

    assert db_session.query(Project).count() == 1


def test_a_short_video_is_ignored(db_session):
    create_subscription(db_session, channel_id=CHANNEL, hub=MockHubClient())
    decision = handle_notification(db_session, _notification(), probe=lambda url: _ProbeInfo(30))
    assert decision.status == YouTubeEventStatus.ignored
    assert "shorter" in decision.reason


def test_a_short_is_ignored(db_session):
    create_subscription(db_session, channel_id=CHANNEL, hub=MockHubClient())
    decision = handle_notification(
        db_session,
        _notification(),
        probe=lambda url: _ProbeInfo(duration=45, raw={"width": 1080, "height": 1920}),
    )
    assert decision.status == YouTubeEventStatus.ignored


def test_a_probe_failure_is_recorded_not_raised(db_session):
    """One unavailable video must not take down the callback."""
    from quickreel.media.downloader import DownloadError

    create_subscription(db_session, channel_id=CHANNEL, hub=MockHubClient())

    def failing_probe(url):
        raise DownloadError("video is private")

    decision = handle_notification(db_session, _notification(), probe=failing_probe)
    assert decision.status == YouTubeEventStatus.failed
    assert "probe failed" in decision.reason


def test_a_revoked_subscription_stops_ingesting(db_session):
    hub = MockHubClient()
    subscription = create_subscription(db_session, channel_id=CHANNEL, hub=hub)
    revoke_subscription(db_session, subscription, hub=hub)

    decision = handle_notification(
        db_session, _notification(), probe=lambda url: _ProbeInfo(duration=3600)
    )
    assert decision.status == YouTubeEventStatus.ignored
    assert "revoked" in decision.reason
