"""Scheduler unit tests: claiming, retry policy, and terminal failures.

These drive the real scheduler against a real SQLite session. The adapter is the
deterministic mock with injected failures, so every branch of the retry policy is
exercised without network access.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from quickreel.models import (
    Clip,
    ClipStatus,
    Platform,
    Project,
    ProjectStatus,
    PublishAttempt,
    PublishTarget,
    PublishTargetStatus,
    ScheduledPost,
    ScheduledPostStatus,
)
from quickreel.publishing import scheduler
from quickreel.publishing.mocks import MockAdapter


@pytest.fixture(autouse=True)
def _clean_publishing_tables(db_session):
    """Clear publishing rows before each test.

    ``due_posts`` deliberately scans every scheduled post, so rows left by an
    earlier test would otherwise show up as spurious due posts here. Deleting
    up front keeps each test's view of the queue its own.
    """
    from sqlalchemy import delete

    for model in (PublishAttempt, ScheduledPost, PublishTarget):
        db_session.execute(delete(model))
    db_session.commit()
    yield


@pytest.fixture()
def rendered_clip(db_session, tmp_path: Path) -> Clip:
    """A rendered clip whose file exists, so the scheduler gets past its guards."""
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"\x00" * 64)

    project = Project(title="Podcast", status=ProjectStatus.ready)
    db_session.add(project)
    db_session.flush()

    clip = Clip(
        project_id=project.id,
        start_seconds=0.0,
        end_seconds=15.0,
        title="A moment",
        status=ClipStatus.rendered,
        render_path=str(video),
    )
    db_session.add(clip)
    db_session.commit()
    return clip


@pytest.fixture()
def target(db_session) -> PublishTarget:
    target = PublishTarget(platform=Platform.tiktok, display_name="Test account")
    db_session.add(target)
    db_session.commit()
    return target


def _schedule(
    session,
    clip: Clip,
    target: PublishTarget,
    *,
    when: datetime | None = None,
    status: ScheduledPostStatus = ScheduledPostStatus.scheduled,
) -> ScheduledPost:
    post = ScheduledPost(
        clip_id=clip.id,
        target_id=target.id,
        scheduled_for=when or datetime.now(UTC) - timedelta(minutes=1),
        caption="hello",
        status=status,
    )
    session.add(post)
    session.commit()
    return post


def test_backoff_is_exponential_and_capped():
    assert scheduler.compute_backoff(1) == timedelta(seconds=60)
    assert scheduler.compute_backoff(2) == timedelta(seconds=120)
    assert scheduler.compute_backoff(3) == timedelta(seconds=240)
    # Capped so a throttled post cannot drift days out of sight.
    assert scheduler.compute_backoff(20) == timedelta(seconds=3600)


def test_backoff_honours_platform_retry_after():
    """The platform's own hint wins: ignoring it turns a throttle into a block."""
    assert scheduler.compute_backoff(5, retry_after_seconds=12) == timedelta(seconds=12)
    # ...but it is still capped by our ceiling.
    assert scheduler.compute_backoff(5, retry_after_seconds=99999) == timedelta(seconds=3600)


def test_due_posts_includes_past_and_excludes_future(db_session, rendered_clip, target):
    past = _schedule(
        db_session, rendered_clip, target, when=datetime.now(UTC) - timedelta(minutes=5)
    )
    future = _schedule(
        db_session, rendered_clip, target, when=datetime.now(UTC) + timedelta(hours=2)
    )
    due = scheduler.due_posts(db_session)
    assert past.id in due
    assert future.id not in due


def test_due_posts_skips_inactive_target(db_session, rendered_clip, target):
    _schedule(db_session, rendered_clip, target)
    target.status = PublishTargetStatus.needs_reauth
    db_session.commit()
    assert scheduler.due_posts(db_session) == []


def test_publish_succeeds_and_records_attempt(db_session, rendered_clip, target):
    post = _schedule(db_session, rendered_clip, target)
    adapter = MockAdapter(name="tiktok")

    outcome = scheduler.publish_post(db_session, post.id, adapter=adapter)

    assert outcome.status == "published"
    db_session.refresh(post)
    assert post.status == ScheduledPostStatus.published
    assert post.external_post_id.startswith("tiktok-")
    assert post.published_at is not None
    assert post.attempts == 1
    attempts = db_session.query(PublishAttempt).filter_by(post_id=post.id).all()
    assert len(attempts) == 1 and attempts[0].ok is True


def test_transient_failure_schedules_a_retry(db_session, rendered_clip, target):
    post = _schedule(db_session, rendered_clip, target)
    adapter = MockAdapter(name="tiktok", fail_times=1, failure="transient")

    outcome = scheduler.publish_post(db_session, post.id, adapter=adapter)

    assert outcome.status == "failed"
    assert outcome.will_retry is True
    db_session.refresh(post)
    assert post.status == ScheduledPostStatus.failed
    assert post.next_attempt_at is not None
    assert post.next_attempt_at > datetime.now(UTC)


def test_retry_then_succeed_on_second_attempt(db_session, rendered_clip, target):
    post = _schedule(db_session, rendered_clip, target)
    # Fails once, then succeeds.
    adapter = MockAdapter(name="tiktok", fail_times=1, failure="transient")
    first = scheduler.publish_post(db_session, post.id, adapter=adapter)
    assert first.status == "failed"

    # Make the backoff already elapsed, then run the sweep.
    db_session.refresh(post)
    post.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
    db_session.commit()

    outcomes = scheduler.run_due_posts(db_session)
    assert [o.status for o in outcomes] == ["published"]
    db_session.refresh(post)
    assert post.attempts == 2
    assert db_session.query(PublishAttempt).filter_by(post_id=post.id).count() == 2


def test_permanent_failure_is_not_retried(db_session, rendered_clip, target):
    post = _schedule(db_session, rendered_clip, target)
    adapter = MockAdapter(name="tiktok", fail_times=99, failure="permanent")

    outcome = scheduler.publish_post(db_session, post.id, adapter=adapter)

    assert outcome.status == "failed"
    assert outcome.will_retry is False
    db_session.refresh(post)
    assert post.next_attempt_at is None
    # And it is not picked up by a later sweep.
    assert post.id not in scheduler.due_posts(db_session)


def test_auth_failure_flags_the_target_for_reconnect(db_session, rendered_clip, target):
    post = _schedule(db_session, rendered_clip, target)
    adapter = MockAdapter(name="tiktok", fail_times=99, failure="auth")

    outcome = scheduler.publish_post(db_session, post.id, adapter=adapter)

    assert outcome.status == "failed"
    assert outcome.will_retry is False
    assert "authentication failed" in outcome.detail
    db_session.refresh(target)
    assert target.status == PublishTargetStatus.needs_reauth
    assert target.status_detail


def test_retries_stop_at_the_attempt_cap(db_session, rendered_clip, target, monkeypatch):
    """A persistently transient failure must eventually give up, not loop forever."""
    from quickreel import config

    config.get_settings.cache_clear()
    monkeypatch.setenv("QUICKREEL_PUBLISH_MAX_ATTEMPTS", "2")
    try:
        post = _schedule(db_session, rendered_clip, target)
        adapter = MockAdapter(name="tiktok", fail_times=99, failure="transient")

        first = scheduler.publish_post(db_session, post.id, adapter=adapter)
        assert first.will_retry is True

        db_session.refresh(post)
        post.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
        db_session.commit()

        second = scheduler.publish_post(db_session, post.id, adapter=adapter)
        assert second.status == "failed"
        assert second.will_retry is False
        db_session.refresh(post)
        assert post.next_attempt_at is None
    finally:
        config.get_settings.cache_clear()


def test_a_post_is_claimed_only_once(db_session, rendered_clip, target):
    """Two workers racing for the same post must produce exactly one publish."""
    post = _schedule(db_session, rendered_clip, target)
    adapter = MockAdapter(name="tiktok")

    first = scheduler.publish_post(db_session, post.id, adapter=adapter)
    second = scheduler.publish_post(db_session, post.id, adapter=adapter)

    assert first.status == "published"
    assert second.status == "skipped"
    assert len(adapter.published) == 1


def test_unrendered_clip_is_a_terminal_failure(db_session, rendered_clip, target):
    rendered_clip.status = ClipStatus.candidate
    db_session.commit()
    post = _schedule(db_session, rendered_clip, target)
    adapter = MockAdapter(name="tiktok")

    outcome = scheduler.publish_post(db_session, post.id, adapter=adapter)

    assert outcome.status == "failed"
    assert outcome.will_retry is False
    assert "not rendered" in outcome.detail
    assert adapter.published == []


def test_missing_render_file_is_a_terminal_failure(db_session, rendered_clip, target):
    Path(rendered_clip.render_path).unlink()
    post = _schedule(db_session, rendered_clip, target)
    adapter = MockAdapter(name="tiktok")

    outcome = scheduler.publish_post(db_session, post.id, adapter=adapter)

    assert outcome.status == "failed"
    assert outcome.will_retry is False
    assert "no rendered file" in outcome.detail


def test_rate_limit_uses_platform_hint(db_session, rendered_clip, target):
    post = _schedule(db_session, rendered_clip, target)
    adapter = MockAdapter(name="tiktok", fail_times=1, failure="rate_limit")

    outcome = scheduler.publish_post(db_session, post.id, adapter=adapter)

    assert outcome.will_retry is True
    # MockAdapter sends retry_after_seconds=30, so the delay should be ~30s.
    delta = outcome.next_attempt_at - datetime.now(UTC)
    assert timedelta(seconds=20) < delta <= timedelta(seconds=31)


def test_published_post_is_never_reswept(db_session, rendered_clip, target):
    post = _schedule(db_session, rendered_clip, target)
    adapter = MockAdapter(name="tiktok")
    scheduler.publish_post(db_session, post.id, adapter=adapter)

    # Even if the scheduled time is in the past, a published post is not due.
    assert post.id not in scheduler.due_posts(db_session)
