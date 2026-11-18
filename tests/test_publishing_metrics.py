"""The performance feedback loop: sampling, cadence, isolation, and failures.

These drive the real metrics service against a real SQLite session, with the
deterministic mock adapter as the platform. The point of the loop is that it runs
unattended against third-party APIs, so the tests concentrate on the properties
that matter there: a failure on one post does not stop the others, a dead
credential is surfaced rather than retried, and a clip's samples are only
readable by the workspace that owns it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import delete

from quickreel import config
from quickreel.models import (
    Clip,
    ClipMetric,
    Platform,
    Project,
    ProjectStatus,
    PublishTarget,
    PublishTargetStatus,
    ScheduledPost,
    ScheduledPostStatus,
)
from quickreel.publishing import metrics
from quickreel.publishing.base import AdapterError
from quickreel.publishing.mocks import MockAdapter


@pytest.fixture(autouse=True)
def _clean_metric_tables(db_session):
    """Clear publishing rows before each test.

    ``due_for_metrics`` scans every published post, so rows left by an earlier
    test would show up as spurious due posts here.
    """
    for model in (ClipMetric, ScheduledPost, PublishTarget):
        db_session.execute(delete(model))
    db_session.commit()
    yield


@pytest.fixture()
def published_post(db_session, tmp_path: Path) -> ScheduledPost:
    """A published post with an external id, so the loop has something to sample."""
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
        status="rendered",
        render_path=str(video),
    )
    target = PublishTarget(platform=Platform.tiktok, display_name="Test account")
    db_session.add_all([clip, target])
    db_session.flush()

    post = ScheduledPost(
        clip_id=clip.id,
        target_id=target.id,
        scheduled_for=datetime.now(UTC) - timedelta(hours=1),
        caption="hello",
        status=ScheduledPostStatus.published,
        external_post_id="tiktok-abc-1",
        published_at=datetime.now(UTC) - timedelta(hours=1),
    )
    db_session.add(post)
    db_session.commit()
    return post


def test_a_published_post_is_sampled_and_a_row_is_written(db_session, published_post):
    outcome = metrics.collect_metrics_for_post(db_session, published_post.id)

    assert outcome.status == "sampled"
    stored = db_session.query(ClipMetric).filter_by(post_id=published_post.id).one()
    assert stored.views > 0
    assert stored.platform == Platform.tiktok
    # Retention is the signal short-form scoring leans on, so it is captured.
    assert stored.average_watch_seconds > 0


def test_an_unpublished_post_is_skipped_not_failed(db_session, published_post):
    published_post.status = ScheduledPostStatus.scheduled
    db_session.commit()

    outcome = metrics.collect_metrics_for_post(db_session, published_post.id)

    assert outcome.status == "skipped"
    assert db_session.query(ClipMetric).count() == 0


def test_a_post_without_an_external_id_is_skipped(db_session, published_post):
    published_post.external_post_id = ""
    db_session.commit()

    outcome = metrics.collect_metrics_for_post(db_session, published_post.id)

    assert outcome.status == "skipped"
    assert db_session.query(ClipMetric).count() == 0


def test_due_for_metrics_includes_a_fresh_post_and_honours_the_interval(
    db_session, published_post
):
    # No samples yet: due.
    assert published_post.id in metrics.due_for_metrics(db_session)

    metrics.collect_metrics_for_post(db_session, published_post.id)

    # Sampled just now, so the interval has not elapsed and it is not due again.
    assert published_post.id not in metrics.due_for_metrics(db_session)
    # ...but once the interval has passed it is due again.
    later = datetime.now(UTC) + timedelta(hours=2)
    assert published_post.id in metrics.due_for_metrics(db_session, now=later)


def test_posts_older_than_the_sampling_window_are_left_alone(db_session, published_post):
    settings = config.get_settings()
    published_post.published_at = datetime.now(UTC) - timedelta(
        days=settings.metrics_max_age_days + 1
    )
    db_session.commit()

    assert metrics.due_for_metrics(db_session) == []


def test_due_for_metrics_skips_an_inactive_target(db_session, published_post):
    target = db_session.get(PublishTarget, published_post.target_id)
    target.status = PublishTargetStatus.needs_reauth
    db_session.commit()

    assert metrics.due_for_metrics(db_session) == []


def test_an_auth_failure_flags_the_target_instead_of_retrying(db_session, published_post):
    """A dead credential must surface as a reconnect prompt, not a loop."""
    adapter = MockAdapter(name="tiktok", fail_times=99, failure="auth")

    outcome = metrics.collect_metrics_for_post(db_session, published_post.id, adapter=adapter)

    assert outcome.status == "failed"
    target = db_session.get(PublishTarget, published_post.target_id)
    assert target.status == PublishTargetStatus.needs_reauth
    assert db_session.query(ClipMetric).count() == 0


def test_a_platform_failure_does_not_stop_the_batch(db_session, published_post):
    """One bad post must not abort a sweep of otherwise fine ones."""

    class FlakyAdapter(MockAdapter):
        def fetch_metrics(self, target_credentials, external_post_id):
            if external_post_id.endswith("1"):
                raise AdapterError("boom")
            return super().fetch_metrics(target_credentials, external_post_id)

    # A second published post on a different target, so the batch has two.
    target = PublishTarget(platform=Platform.youtube_shorts, display_name="Shorts")
    db_session.add(target)
    db_session.flush()
    good = ScheduledPost(
        clip_id=published_post.clip_id,
        target_id=target.id,
        scheduled_for=datetime.now(UTC) - timedelta(hours=1),
        status=ScheduledPostStatus.published,
        external_post_id="youtube-xyz-2",
        published_at=datetime.now(UTC) - timedelta(hours=1),
    )
    db_session.add(good)
    db_session.commit()

    bad_outcome = metrics.collect_metrics_for_post(
        db_session, published_post.id, adapter=FlakyAdapter(name="tiktok")
    )
    good_outcome = metrics.collect_metrics_for_post(
        db_session, good.id, adapter=FlakyAdapter(name="youtube_shorts")
    )

    assert bad_outcome.status == "failed"
    assert good_outcome.status == "sampled"
    assert db_session.query(ClipMetric).count() == 1


def test_run_metrics_collection_is_disabled_by_config(db_session, published_post, monkeypatch):
    monkeypatch.setenv("QUICKREEL_METRICS_ENABLED", "false")
    config.get_settings.cache_clear()
    try:
        assert metrics.run_metrics_collection(db_session) == []
        assert db_session.query(ClipMetric).count() == 0
    finally:
        config.get_settings.cache_clear()


def test_run_metrics_collection_samples_every_due_post(db_session, published_post):
    outcomes = metrics.run_metrics_collection(db_session)

    assert [o.status for o in outcomes] == ["sampled"]
    assert db_session.query(ClipMetric).count() == 1


def test_clip_performance_summary_totals_the_latest_sample_per_platform(
    db_session, published_post
):
    """Counters are cumulative, so totals use the newest sample, not the sum.

    Summing the series would count the same view once per sweep, which is the
    mistake that makes a learning loop learn the wrong thing.
    """
    adapter = MockAdapter(name="tiktok")
    metrics.collect_metrics_for_post(db_session, published_post.id, adapter=adapter)
    metrics.collect_metrics_for_post(db_session, published_post.id, adapter=adapter)

    summary = metrics.clip_performance_summary(db_session, published_post.clip_id)

    assert summary["sample_count"] == 2
    latest = summary["platforms"]["tiktok"]
    assert summary["totals"]["views"] == latest["views"]
    assert summary["totals"]["likes"] == latest["likes"]


def test_clip_performance_summary_is_empty_without_samples(db_session, published_post):
    summary = metrics.clip_performance_summary(db_session, published_post.clip_id)

    assert summary == {"sample_count": 0, "platforms": {}, "totals": {}}


def test_run_due_metrics_is_scoped_to_one_workspace(db_session, published_post, tmp_path):
    """A sweep for one workspace must not sample another workspace's post."""
    from quickreel.models import Workspace

    target = db_session.get(PublishTarget, published_post.target_id)
    mine = Workspace(name="Mine")
    theirs = Workspace(name="Theirs")
    db_session.add_all([mine, theirs])
    db_session.flush()
    target.workspace_id = mine.id
    db_session.commit()

    # A sweep scoped to the other workspace finds nothing.
    assert metrics.run_metrics_collection(db_session, workspace_id=theirs.id) == []
    # The owning workspace's sweep finds it.
    outcomes = metrics.run_metrics_collection(db_session, workspace_id=mine.id)
    assert [o.status for o in outcomes] == ["sampled"]
