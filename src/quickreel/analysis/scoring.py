"""Viral scoring engine.

A weighted, explainable model over six factors. The score is deliberately a
prediction, not a guarantee: the weights live in configuration and the engine
records a full breakdown per clip so the model can be recalibrated later against
real views, retention and share data (the "Viral Intelligence" loop).

    score = 100 * sum(weight_i * component_i)

Components are normalised to 0..1 by :mod:`quickreel.analysis.features`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..ai.base import AudioEvent, TranscriptResult, VisualAnalysis
from ..config import get_settings
from ..logging_setup import get_logger
from .features import MomentFeatures, extract_features

logger = get_logger(__name__)

COMPONENT_LABELS = {
    "emotional_intensity": "Emotional intensity",
    "hook_strength": "Hook strength",
    "storytelling": "Storytelling / curiosity",
    "shareability": "Shareability",
    "audio_visual_engagement": "Audio & visual engagement",
    "standalone_context": "Standalone context",
}

# A short human-readable reason for each component, used in the API payload so
# the UI can explain *why* a clip scored the way it did.
_COMPONENT_EXPLAINERS = {
    "hook_strength": "Does the first 2-3 seconds capture attention?",
    "emotional_intensity": "Emotional language and reactive audio (laughter, emphasis).",
    "storytelling": "Narrative shape: setup, turn and payoff.",
    "shareability": "Quotability, length fit and cleanliness of delivery.",
    "audio_visual_engagement": "Loudness peaks, scene changes and visual highlights.",
    "standalone_context": "Does the clip make sense without the full video?",
}

_PENALTY_RULES = (
    ("starts_mid_sentence", 6.0, "Opens mid-sentence"),
    ("ends_on_incomplete_sentence", 5.0, "Ends without closing the thought"),
    ("too_short", 4.0, "Shorter than the minimum usable length"),
    ("too_long", 4.0, "Longer than the ideal short-form length"),
    ("excessive_filler", 3.0, "Filler-heavy delivery"),
    ("long_silences", 3.0, "Contains long pauses"),
)

_MIN_DURATION = 8.0
_MAX_DURATION = 120.0
_IDEAL_MIN = 15.0
_IDEAL_MAX = 60.0


@dataclass(slots=True)
class ScoreResult:
    score: float
    components: dict[str, float]
    breakdown: dict = field(default_factory=dict)
    features: MomentFeatures | None = None


class ViralScorer:
    """Weighted multi-factor scorer."""

    def __init__(self, weights: dict[str, float] | None = None) -> None:
        settings = get_settings()
        self.weights = dict(weights or settings.scoring_weights)
        self._normalise_weights()

    def _normalise_weights(self) -> None:
        total = sum(self.weights.values())
        if total <= 0:
            raise ValueError("scoring weights must sum to a positive value")
        if abs(total - 1.0) > 1e-6:
            # Re-normalise rather than fail: misconfigured weights should not
            # take the pipeline down, but they must be visible in the logs.
            logger.warning("scoring.weights_renormalised", original_total=total)
            self.weights = {key: value / total for key, value in self.weights.items()}

    def score_window(
        self,
        transcript: TranscriptResult,
        visual: VisualAnalysis,
        audio_events: list[AudioEvent],
        start: float,
        end: float,
    ) -> ScoreResult:
        features = extract_features(transcript, visual, audio_events, start, end)
        return self.score_features(features)

    def score_features(self, features: MomentFeatures) -> ScoreResult:
        components = {
            "emotional_intensity": features.emotional_intensity,
            "hook_strength": features.hook_strength,
            "storytelling": features.storytelling,
            "shareability": features.shareability,
            "audio_visual_engagement": features.audio_visual_engagement,
            "standalone_context": features.standalone_context,
        }

        weighted = sum(self.weights.get(key, 0.0) * value for key, value in components.items())
        base = weighted * 100.0

        penalties = self._penalties(features)
        total_penalty = sum(value for _, value, _ in penalties)
        score = max(0.0, min(100.0, base - total_penalty))

        breakdown = {
            "base_score": round(base, 2),
            "penalty_total": round(total_penalty, 2),
            "penalties": [
                {"rule": rule, "points": value, "reason": reason}
                for rule, value, reason in penalties
            ],
            "components": [
                {
                    "key": key,
                    "label": COMPONENT_LABELS[key],
                    "weight": round(self.weights.get(key, 0.0), 4),
                    "value": round(value, 4),
                    "contribution": round(self.weights.get(key, 0.0) * value * 100.0, 2),
                    "explainer": _COMPONENT_EXPLAINERS[key],
                }
                for key, value in components.items()
            ],
            "features": features.as_dict(),
            "weights": {k: round(v, 4) for k, v in self.weights.items()},
        }
        return ScoreResult(
            score=round(score, 2),
            components={k: round(v, 4) for k, v in components.items()},
            breakdown=breakdown,
            features=features,
        )

    def _penalties(self, features: MomentFeatures) -> list[tuple[str, float, str]]:
        penalties: list[tuple[str, float, str]] = []
        rules = dict((rule, (points, reason)) for rule, points, reason in _PENALTY_RULES)

        if features.starts_mid_sentence:
            penalties.append(("starts_mid_sentence", *rules["starts_mid_sentence"]))
        if not features.ends_on_complete_sentence:
            penalties.append(("ends_on_incomplete_sentence", *rules["ends_on_incomplete_sentence"]))
        if features.duration_seconds < _MIN_DURATION:
            penalties.append(("too_short", *rules["too_short"]))
        elif features.duration_seconds > _MAX_DURATION:
            penalties.append(("too_long", *rules["too_long"]))
        if features.filler_ratio > 0.06:
            penalties.append(("excessive_filler", *rules["excessive_filler"]))
        if features.silence_ratio > 0.15:
            penalties.append(("long_silences", *rules["long_silences"]))
        return penalties

    def rank(self, results: list[tuple[str, ScoreResult]]) -> list[tuple[str, ScoreResult, int]]:
        """Return ``(clip_id, result, rank)`` sorted by descending score."""
        ordered = sorted(results, key=lambda item: item[1].score, reverse=True)
        return [(clip_id, result, index + 1) for index, (clip_id, result) in enumerate(ordered)]
