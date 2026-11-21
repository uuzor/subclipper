"""Scoring recalibration: the read half of the Viral Intelligence loop.

These test the property that matters most about a system that adjusts its own
model: it must not fool itself. A fit on too little data says so, a fit that
cannot explain engagement better than the current weights refuses to apply, and
the constraints that keep the weights interpretable (sum to one, floor under
each factor) hold no matter what the input looks like.

The engine is pure standard library, so these run without the optional AI extra.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pytest

from quickreel.analysis import calibration
from quickreel.analysis.calibration import (
    FACTORS,
    MIN_SAMPLES,
    WEIGHT_FLOOR,
    Observation,
    fit_weights,
    gather_observations,
)
from quickreel.models import (
    Clip,
    ClipMetric,
    Platform,
    Project,
    ProjectStatus,
    PublishTarget,
    ScheduledPost,
    ScheduledPostStatus,
    Workspace,
)

PRIOR = {
    "emotional_intensity": 0.20,
    "hook_strength": 0.20,
    "storytelling": 0.20,
    "shareability": 0.15,
    "audio_visual_engagement": 0.10,
    "standalone_context": 0.15,
}


def _structured_observations(count: int = 120, seed: int = 11) -> list[Observation]:
    """Outcomes driven mostly by hook_strength, the structure the fit should find."""
    rng = random.Random(seed)
    observations = []
    for i in range(count):
        components = {f: rng.random() for f in FACTORS}
        raw = 0.75 * components["hook_strength"] + 0.25 * components["shareability"]
        outcome = max(0.0, min(1.0, raw + rng.gauss(0, 0.05)))
        observations.append(Observation(f"clip-{i}", "tiktok", components, outcome))
    return observations


# -- the fit learns real structure -------------------------------------------


def test_the_fit_raises_the_weight_of_the_factor_that_drives_engagement():
    fit = fit_weights(_structured_observations(), PRIOR)

    assert fit.applicable, fit.reason
    assert fit.weights["hook_strength"] > PRIOR["hook_strength"]
    # The factor that actually drives the outcome should end up dominant.
    assert fit.weights["hook_strength"] == max(fit.weights.values())


def test_the_fit_explains_more_variance_than_the_weights_it_replaces():
    fit = fit_weights(_structured_observations(), PRIOR)

    assert fit.diagnostics["fitted_r_squared"] > fit.diagnostics["prior_r_squared"]
    assert fit.diagnostics["improvement"] > 0


def test_a_fit_is_deterministic_for_the_same_input():
    first = fit_weights(_structured_observations(), PRIOR)
    second = fit_weights(_structured_observations(), PRIOR)

    assert first.weights == second.weights


# -- the constraints that keep weights interpretable -------------------------


def test_fitted_weights_always_sum_to_one():
    fit = fit_weights(_structured_observations(), PRIOR)

    assert sum(fit.weights.values()) == pytest.approx(1.0, abs=1e-9)


def test_no_factor_falls_below_the_floor():
    # A factor that is pure noise should shrink, not vanish: deleting a signal
    # on this much data would be overfitting to the sample.
    fit = fit_weights(_structured_observations(), PRIOR)

    for factor in FACTORS:
        assert fit.weights[factor] >= WEIGHT_FLOOR - 1e-9


def test_every_factor_is_present_in_the_output():
    fit = fit_weights(_structured_observations(), PRIOR)

    assert set(fit.weights) == set(FACTORS)


# -- refusing to learn from noise --------------------------------------------


def test_too_few_samples_is_not_applicable_and_returns_the_prior():
    fit = fit_weights(_structured_observations(count=MIN_SAMPLES - 1), PRIOR)

    assert not fit.applicable
    assert "not enough" in fit.reason
    assert fit.weights == pytest.approx(PRIOR, abs=1e-9)


def test_identical_outcomes_are_not_applicable():
    # Every clip performing the same means there is nothing to rank, so any
    # weight vector is equally (un)justified.
    observations = [
        Observation(f"c{i}", "tiktok", {f: 0.5 for f in FACTORS}, 0.5) for i in range(50)
    ]

    fit = fit_weights(observations, PRIOR)

    assert not fit.applicable
    assert "identically" in fit.reason


def test_a_fit_worse_than_the_prior_is_not_applicable():
    # Outcomes unrelated to the factors: the fit cannot beat the prior, and
    # must say so rather than propose a change built on noise.
    rng = random.Random(5)
    observations = [
        Observation(f"c{i}", "tiktok", {f: rng.random() for f in FACTORS}, rng.random())
        for i in range(60)
    ]

    fit = fit_weights(observations, PRIOR)

    if fit.applicable:
        # If it does pass, it must be because it genuinely improved.
        assert fit.diagnostics["improvement"] > 0


def test_shrinkage_is_stronger_on_smaller_samples():
    small = fit_weights(_structured_observations(count=20), PRIOR)
    large = fit_weights(_structured_observations(count=200), PRIOR)

    assert small.diagnostics["ridge"] > large.diagnostics["ridge"]


# -- gathering observations from the database --------------------------------


@pytest.fixture()
def workspace(db_session) -> Workspace:
    ws = Workspace(name="Analytics")
    db_session.add(ws)
    db_session.commit()
    return ws


def _make_clip(db_session, workspace, *, components, views, likes=0, comments=0, shares=0):
    project = Project(
        title="Podcast", status=ProjectStatus.ready, workspace_id=workspace.id
    )
    db_session.add(project)
    db_session.flush()

    clip = Clip(
        project_id=project.id,
        start_seconds=0.0,
        end_seconds=20.0,
        title="Moment",
        status="rendered",
        score=50.0,
        score_components=components,
    )
    target = PublishTarget(
        platform=Platform.tiktok, display_name="acct", workspace_id=workspace.id
    )
    db_session.add_all([clip, target])
    db_session.flush()

    post = ScheduledPost(
        clip_id=clip.id,
        target_id=target.id,
        scheduled_for=datetime.now(UTC) - timedelta(hours=1),
        status=ScheduledPostStatus.published,
        external_post_id=f"ext-{clip.id}",
        published_at=datetime.now(UTC) - timedelta(hours=1),
    )
    db_session.add(post)
    db_session.flush()

    db_session.add(
        ClipMetric(
            post_id=post.id,
            platform=Platform.tiktok,
            views=views,
            likes=likes,
            comments=comments,
            shares=shares,
            average_watch_seconds=10.0,
        )
    )
    db_session.commit()
    return clip


def test_observations_pair_components_with_engagement(db_session, workspace):
    _make_clip(db_session, workspace, components={f: 0.5 for f in FACTORS}, views=1000, likes=50)

    observations = gather_observations(db_session, workspace.id)

    assert len(observations) == 1
    assert observations[0].platform == "tiktok"
    assert 0.0 <= observations[0].outcome <= 1.0


def test_a_clip_with_no_views_is_not_an_observation(db_session, workspace):
    _make_clip(db_session, workspace, components={f: 0.5 for f in FACTORS}, views=0)

    assert gather_observations(db_session, workspace.id) == []


def test_a_clip_without_components_is_not_an_observation(db_session, workspace):
    _make_clip(db_session, workspace, components={}, views=500, likes=10)

    assert gather_observations(db_session, workspace.id) == []


def test_a_clip_without_a_published_post_is_not_an_observation(db_session, workspace):
    project = Project(title="Unpublished", status=ProjectStatus.ready, workspace_id=workspace.id)
    db_session.add(project)
    db_session.flush()
    db_session.add(
        Clip(
            project_id=project.id,
            start_seconds=0.0,
            end_seconds=20.0,
            score=40.0,
            score_components={f: 0.5 for f in FACTORS},
        )
    )
    db_session.commit()

    assert gather_observations(db_session, workspace.id) == []


def test_another_workspace_observations_are_not_mixed_in(db_session, workspace):
    other = Workspace(name="Other")
    db_session.add(other)
    db_session.commit()
    _make_clip(db_session, other, components={f: 0.5 for f in FACTORS}, views=900, likes=40)

    assert gather_observations(db_session, workspace.id) == []


def test_cumulative_samples_are_not_double_counted(db_session, workspace):
    # Platform counters are cumulative; summing the time series would count the
    # same view once per sweep and inflate the target.
    clip = _make_clip(
        db_session, workspace, components={f: 0.5 for f in FACTORS}, views=1000, likes=100
    )
    post = db_session.query(ScheduledPost).filter_by(clip_id=clip.id).one()
    db_session.add(
        ClipMetric(
            post_id=post.id,
            platform=Platform.tiktok,
            views=500,
            likes=50,
            average_watch_seconds=10.0,
            captured_at=datetime.now(UTC) - timedelta(hours=5),
        )
    )
    db_session.commit()

    observations = gather_observations(db_session, workspace.id)

    assert len(observations) == 1


# -- proposing and applying --------------------------------------------------


def test_proposing_records_a_row_without_changing_anything(db_session, workspace):
    _make_clip(db_session, workspace, components={f: 0.5 for f in FACTORS}, views=1000, likes=50)

    record = calibration.propose_calibration(db_session, workspace.id, PRIOR)

    assert record.applied is False
    assert record.applied_at is None
    assert record.sample_size == 1
    assert record.baseline_weights == pytest.approx(PRIOR, abs=1e-6)
    assert calibration.latest_applied_weights(db_session, workspace.id) is None


def test_an_inapplicable_proposal_cannot_be_applied(db_session, workspace):
    _make_clip(db_session, workspace, components={f: 0.5 for f in FACTORS}, views=1000, likes=50)
    record = calibration.propose_calibration(db_session, workspace.id, PRIOR)

    with pytest.raises(ValueError, match="not judged applicable"):
        calibration.apply_calibration(db_session, record)


def test_applying_an_applicable_proposal_makes_it_the_active_weights(db_session, workspace):
    # Build enough structured history for a real fit.
    rng = random.Random(21)
    for _ in range(MIN_SAMPLES + 8):
        components = {f: rng.random() for f in FACTORS}
        strong = components["hook_strength"] > 0.5
        _make_clip(
            db_session,
            workspace,
            components=components,
            views=1000,
            likes=400 if strong else 20,
        )

    record = calibration.propose_calibration(db_session, workspace.id, PRIOR)

    assert record.diagnostics["applicable"], record.diagnostics
    calibration.apply_calibration(db_session, record)

    assert record.applied is True
    assert record.applied_at is not None
    assert calibration.latest_applied_weights(db_session, workspace.id) == pytest.approx(
        record.proposed_weights, abs=1e-9
    )


def test_the_pipeline_scores_with_the_applied_weights(db_session, workspace):
    """The seam that makes recalibration real.

    A fit is worthless if the pipeline keeps building its scorer from
    configuration. This is the check that an applied vector reaches scoring.
    """
    from quickreel.pipeline.orchestrator import Pipeline

    project = Project(title="Scored", status=ProjectStatus.ready, workspace_id=workspace.id)
    db_session.add(project)
    db_session.commit()

    pipeline = Pipeline(db_session)
    # Before any fit, the configured defaults are in force.
    assert pipeline._active_weights(project) == pytest.approx(PRIOR, abs=1e-9)

    custom = {f: 1.0 / len(FACTORS) for f in FACTORS}
    custom["hook_strength"] = 0.5
    custom["shareability"] = 0.5
    record = calibration.ScoringCalibration(
        workspace_id=workspace.id,
        proposed_weights=custom,
        baseline_weights=PRIOR,
        sample_size=50,
        diagnostics={"applicable": True},
        applied=True,
        applied_at=datetime.now(UTC),
    )
    db_session.add(record)
    db_session.commit()

    assert pipeline._active_weights(project) == pytest.approx(custom, abs=1e-9)
