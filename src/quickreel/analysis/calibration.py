"""Recalibrate the viral scoring weights from recorded engagement.

This is the half of the feedback loop that reads. :mod:`quickreel.publishing.metrics`
records what published clips actually did; this module turns that history into a
better weight vector, so the initial design guesses in ``scoring_weights`` are
replaced by weights fitted to this workspace's own audience.

Four decisions shape it.

**Per-platform normalisation.** A view on TikTok and a view on Shorts are not the
same unit, and a niche's baseline engagement rate is not another's. Raw counts
are therefore ranked within their platform before they become a target, so the
fit learns *which clip did better than its peers*, not which platform is bigger.

**The weights stay a distribution.** Scoring is a convex combination, so the
fitted vector is constrained to sum to one with a floor under each factor. A
factor that looks useless is shrunk toward its floor rather than zeroed: six
observations is not enough evidence to delete a signal outright.

**Shrinkage toward the prior.** With little data the honest answer is "about what
you already had". A ridge term pulls the fit toward the current weights, and the
pull weakens as the sample grows. A fit that cannot beat the prior is still
recorded, marked as not applicable, so the UI can say "not enough signal yet"
rather than showing a confident-looking proposal built on noise.

**Nothing applies itself.** A fit produces a :class:`~quickreel.models.ScoringCalibration`
row. Installing it is a separate, explicit step. An automated loop that silently
rewrites the model it is judging itself by is a loop with no ground truth left.

Pure standard library: numpy is only in the optional ``ai`` extra, and calibration
must not become a dependency of the core install.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..logging_setup import get_logger
from ..models import Clip, ClipMetric, Project, ScheduledPost, ScoringCalibration

logger = get_logger(__name__)

FACTORS = (
    "emotional_intensity",
    "hook_strength",
    "storytelling",
    "shareability",
    "audio_visual_engagement",
    "standalone_context",
)

# Below this many observed clips the fit is noise, not signal. Twelve is not a
# statistical guarantee; it is the point where a six-parameter model has at
# least two observations per parameter.
MIN_SAMPLES = 12

# No factor may fall below this share of the weight mass. The floor exists so a
# factor that happens to look flat in a small sample is down-weighted, not
# deleted, and can recover as more data arrives.
WEIGHT_FLOOR = 0.02

# Ridge strength pulling the fit toward the prior weights. Decays with sample
# size, so early fits barely move and later ones are allowed to.
SHRINKAGE_BASE = 8.0


@dataclass(slots=True)
class Observation:
    """One clip's factor values paired with how it actually performed."""

    clip_id: str
    platform: str
    components: dict[str, float]
    outcome: float


@dataclass(slots=True)
class FitResult:
    """The outcome of one calibration attempt, applicable or not."""

    weights: dict[str, float]
    sample_size: int
    platform_counts: dict[str, int]
    diagnostics: dict = field(default_factory=dict)
    applicable: bool = False
    reason: str = ""


def _engagement_rate(views: int, likes: int, comments: int, shares: int) -> float:
    """Engagement per view, weighting the actions that cost more effort higher.

    Shares are the strongest short-form signal (they put the clip in front of a
    new audience), comments next (they signal a reaction worth arguing with),
    likes weakest. Dividing by views keeps a big account from dominating purely
    on reach.
    """
    if views <= 0:
        return 0.0
    weighted = likes + 2.0 * comments + 3.0 * shares
    return weighted / views


def _percentile_ranks(values: list[float]) -> list[float]:
    """Rank values into 0..1, ties sharing their average rank.

    Rank-based rather than z-scored because engagement distributions are heavy
    tailed: one viral clip should not define the target for every other clip.
    """
    n = len(values)
    if n == 0:
        return []
    if n == 1:
        return [0.5]

    order = sorted(range(n), key=lambda i: values[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        # Average rank across the tie group, scaled into 0..1.
        average = (i + j) / 2.0
        for k in range(i, j + 1):
            ranks[order[k]] = average / (n - 1)
        i = j + 1
    return ranks


def gather_observations(session: Session, workspace_id: str | None) -> list[Observation]:
    """Pair every scored clip with an outcome, one observation per clip.

    A clip qualifies only if it has both factor values and at least one recorded
    sample. A clip that was never published has no outcome to learn from, and a
    clip with no components predates the breakdown and cannot be fitted.
    """
    rows = session.execute(
        select(Clip, ClipMetric)
        .join(Project, Project.id == Clip.project_id)
        .join(ScheduledPost, ScheduledPost.clip_id == Clip.id)
        .join(ClipMetric, ClipMetric.post_id == ScheduledPost.id)
        .where(Project.workspace_id == workspace_id)
    ).all()

    # Collapse the time series: use the newest sample per platform, because
    # platform counters are cumulative and summing the series double counts.
    latest: dict[str, dict[str, ClipMetric]] = {}
    clips: dict[str, Clip] = {}
    for clip, metric in rows:
        clips[clip.id] = clip
        per_platform = latest.setdefault(clip.id, {})
        seen = per_platform.get(str(metric.platform))
        if seen is None or metric.captured_at >= seen.captured_at:
            per_platform[str(metric.platform)] = metric

    raw: list[tuple[str, str, dict[str, float], float, float]] = []
    for clip_id, per_platform in latest.items():
        clip = clips[clip_id]
        components = clip.score_components or {}
        if not components or any(f not in components for f in FACTORS):
            continue

        views = sum(m.views for m in per_platform.values())
        likes = sum(m.likes for m in per_platform.values())
        comments = sum(m.comments for m in per_platform.values())
        shares = sum(m.shares for m in per_platform.values())
        if views <= 0:
            # No reach yet: the clip has no performance to learn from.
            continue

        duration = max(clip.end_seconds - clip.start_seconds, 1.0)
        watched = sum(m.average_watch_seconds for m in per_platform.values()) / len(per_platform)
        retention = min(max(watched / duration, 0.0), 1.0)

        # The dominant platform is the one that actually delivered the reach.
        platform = max(per_platform.items(), key=lambda kv: kv[1].views)[0]
        raw.append(
            (
                clip_id,
                platform,
                {f: float(components[f]) for f in FACTORS},
                _engagement_rate(views, likes, comments, shares),
                retention,
            )
        )

    # Rank engagement within each platform so cross-platform scale differences
    # do not leak into the target. A platform with too few clips is ranked
    # against the whole sample instead of against itself.
    by_platform: dict[str, list[int]] = {}
    for index, row in enumerate(raw):
        by_platform.setdefault(row[1], []).append(index)

    engagement_rank: dict[int, float] = {}
    for indices in by_platform.values():
        if len(indices) >= 3:
            ranks = _percentile_ranks([raw[i][3] for i in indices])
            for i, rank in zip(indices, ranks, strict=True):
                engagement_rank[i] = rank
    if len(engagement_rank) < len(raw):
        # Fill the remainder against the full sample.
        fallback = _percentile_ranks([row[3] for row in raw])
        for i in range(len(raw)):
            engagement_rank.setdefault(i, fallback[i])

    # Retention is already 0..1 and comparable across platforms, so it enters
    # directly. Engagement dominates because it is the better-observed signal.
    return [
        Observation(
            clip_id=clip_id,
            platform=platform,
            components=components,
            outcome=0.65 * engagement_rank[index] + 0.35 * retention,
        )
        for index, (clip_id, platform, components, _rate, retention) in enumerate(raw)
    ]


def _project_to_weights(vector: list[float]) -> list[float]:
    """Project onto ``{w : w_i >= floor, sum(w) == 1}``.

    Shift by the floor, project the remainder onto the unit simplex, shift back.
    This is the standard sort-based projection, kept explicit because the
    constraint is the whole reason the fit stays interpretable.
    """
    n = len(vector)
    floor = WEIGHT_FLOOR
    shifted = [v - floor for v in vector]
    total = 1.0 - n * floor

    ordered = sorted(shifted, reverse=True)
    cumulative = 0.0
    rho = 0
    for i, value in enumerate(ordered, start=1):
        cumulative += value
        if value - (cumulative - total) / i > 0:
            rho = i
    theta = (sum(ordered[:rho]) - total) / rho
    return [max(v - theta, 0.0) + floor for v in shifted]


def _predict(weights: dict[str, float], components: dict[str, float]) -> float:
    return sum(weights.get(f, 0.0) * components[f] for f in FACTORS)


def _r_squared(predictions: list[float], targets: list[float]) -> float:
    """How much of the outcome's variance the weights explain.

    Negative values are meaningful here: a fit can be worse than predicting the
    mean, and reporting that honestly is the point.
    """
    n = len(targets)
    if n == 0:
        return 0.0
    mean = sum(targets) / n
    ss_tot = sum((t - mean) ** 2 for t in targets)
    if ss_tot <= 1e-12:
        return 0.0
    ss_res = sum((p - t) ** 2 for p, t in zip(predictions, targets, strict=True))
    return 1.0 - ss_res / ss_tot


def fit_weights(
    observations: list[Observation],
    prior: dict[str, float],
    *,
    min_samples: int = MIN_SAMPLES,
    iterations: int = 600,
    learning_rate: float = 0.5,
) -> FitResult:
    """Fit weights by projected gradient descent with shrinkage to ``prior``."""
    sample_size = len(observations)
    platform_counts: dict[str, int] = {}
    for observation in observations:
        platform_counts[observation.platform] = platform_counts.get(observation.platform, 0) + 1

    prior_vector = [float(prior.get(f, 0.0)) for f in FACTORS]
    prior_total = sum(prior_vector)
    if prior_total <= 0:
        prior_vector = [1.0 / len(FACTORS)] * len(FACTORS)
    else:
        prior_vector = [v / prior_total for v in prior_vector]

    if sample_size < min_samples:
        return FitResult(
            weights=dict(zip(FACTORS, prior_vector, strict=True)),
            sample_size=sample_size,
            platform_counts=platform_counts,
            diagnostics={"min_samples": min_samples},
            applicable=False,
            reason=(
                f"{sample_size} observed clips, {min_samples} required: "
                "not enough engagement history to recalibrate yet"
            ),
        )

    targets = [o.outcome for o in observations]
    if max(targets) - min(targets) <= 1e-9:
        return FitResult(
            weights=dict(zip(FACTORS, prior_vector, strict=True)),
            sample_size=sample_size,
            platform_counts=platform_counts,
            diagnostics={"outcome_variance": 0.0},
            applicable=False,
            reason="every observed clip performed identically; no signal to learn from",
        )

    # Shrinkage weakens as evidence accumulates.
    ridge = SHRINKAGE_BASE / sample_size
    weights = list(prior_vector)

    for _ in range(iterations):
        gradient = [0.0] * len(FACTORS)
        for observation in observations:
            error = observation.outcome - _predict(
                dict(zip(FACTORS, weights, strict=True)), observation.components
            )
            for index, factor in enumerate(FACTORS):
                gradient[index] -= 2.0 * error * observation.components[factor] / sample_size
        for index in range(len(FACTORS)):
            gradient[index] += 2.0 * ridge * (weights[index] - prior_vector[index])
        weights = [
            w - learning_rate * g for w, g in zip(weights, gradient, strict=True)
        ]
        weights = _project_to_weights(weights)

    fitted = dict(zip(FACTORS, weights, strict=True))
    prior_map = dict(zip(FACTORS, prior_vector, strict=True))
    prior_predictions = [_predict(prior_map, o.components) for o in observations]
    fitted_predictions = [_predict(fitted, o.components) for o in observations]

    prior_r2 = _r_squared(prior_predictions, targets)
    fitted_r2 = _r_squared(fitted_predictions, targets)
    diagnostics = {
        "prior_r_squared": round(prior_r2, 6),
        "fitted_r_squared": round(fitted_r2, 6),
        "improvement": round(fitted_r2 - prior_r2, 6),
        "ridge": round(ridge, 6),
        "weight_shift": round(
            sum(abs(fitted[f] - prior_vector[i]) for i, f in enumerate(FACTORS)), 6
        ),
    }

    # A fit that does not explain more variance than the weights it would
    # replace is not an improvement, whatever the numbers look like.
    if fitted_r2 <= prior_r2:
        return FitResult(
            weights=fitted,
            sample_size=sample_size,
            platform_counts=platform_counts,
            diagnostics=diagnostics,
            applicable=False,
            reason="the fitted weights do not explain engagement better than the current ones",
        )

    return FitResult(
        weights=fitted,
        sample_size=sample_size,
        platform_counts=platform_counts,
        diagnostics=diagnostics,
        applicable=True,
        reason="",
    )


def propose_calibration(
    session: Session, workspace_id: str | None, prior: dict[str, float]
) -> ScoringCalibration:
    """Fit and record a proposal. Never installs it."""
    settings = get_settings()
    observations = gather_observations(session, workspace_id)
    fit = fit_weights(
        observations, prior, min_samples=settings.scoring_calibration_min_samples
    )

    record = ScoringCalibration(
        workspace_id=workspace_id,
        proposed_weights={k: round(v, 6) for k, v in fit.weights.items()},
        baseline_weights={k: round(float(v), 6) for k, v in prior.items()},
        sample_size=fit.sample_size,
        platform_counts=fit.platform_counts,
        diagnostics={**fit.diagnostics, "applicable": fit.applicable, "reason": fit.reason},
        applied=False,
    )
    session.add(record)
    session.flush()
    logger.info(
        "scoring.calibration_proposed",
        workspace_id=workspace_id,
        sample_size=fit.sample_size,
        applicable=fit.applicable,
    )
    return record


def latest_applied_weights(session: Session, workspace_id: str | None) -> dict[str, float] | None:
    """The most recently installed weight vector for a workspace, if any."""
    record = session.scalars(
        select(ScoringCalibration)
        .where(
            ScoringCalibration.workspace_id == workspace_id,
            ScoringCalibration.applied.is_(True),
        )
        .order_by(ScoringCalibration.applied_at.desc())
        .limit(1)
    ).first()
    return dict(record.proposed_weights) if record else None


def apply_calibration(session: Session, record: ScoringCalibration) -> None:
    """Install a proposal as the workspace's active weights.

    Only a proposal the fit judged applicable can be installed; promoting a fit
    it already decided was not an improvement would defeat the check.
    """
    if not record.diagnostics.get("applicable"):
        raise ValueError("this calibration was not judged applicable and cannot be applied")

    record.applied = True
    record.applied_at = datetime.now(UTC)
    session.flush()
    logger.info(
        "scoring.calibration_applied",
        calibration_id=record.id,
        workspace_id=record.workspace_id,
    )


__all__ = [
    "FACTORS",
    "MIN_SAMPLES",
    "WEIGHT_FLOOR",
    "FitResult",
    "Observation",
    "apply_calibration",
    "fit_weights",
    "gather_observations",
    "latest_applied_weights",
    "propose_calibration",
]
