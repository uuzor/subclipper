"""Tests for the viral scoring engine."""

from __future__ import annotations

import pytest

from quickreel.ai.base import AudioEvent, TranscriptResult, TranscriptSegment, VisualAnalysis
from quickreel.analysis.scoring import ViralScorer


def _transcript(segments: list[tuple[float, float, str, str]]) -> TranscriptResult:
    return TranscriptResult(
        language="en",
        text=" ".join(text for _, _, text, _ in segments),
        provider="unit",
        segments=[
            TranscriptSegment(start, end, text, speaker)
            for start, end, text, speaker in segments
        ],
    )


def test_weights_must_sum_to_one_and_are_renormalised():
    scorer = ViralScorer(weights={"hook_strength": 2.0, "emotional_intensity": 2.0})
    assert sum(scorer.weights.values()) == pytest.approx(1.0)
    assert scorer.weights["hook_strength"] == pytest.approx(0.5)


def test_score_is_bounded_and_explainable():
    transcript = _transcript(
        [
            (0.0, 6.0, "The biggest mistake I made was trusting everyone around me.", "S1"),
            (6.0, 12.0, "It nearly killed the company, honestly.", "S1"),
        ]
    )
    scorer = ViralScorer()
    result = scorer.score_window(transcript, VisualAnalysis(), [], 0.0, 12.0)

    assert 0.0 <= result.score <= 100.0
    assert set(result.components) == {
        "emotional_intensity",
        "hook_strength",
        "storytelling",
        "shareability",
        "audio_visual_engagement",
        "standalone_context",
    }
    breakdown = result.breakdown
    assert breakdown["base_score"] > 0
    assert len(breakdown["components"]) == 6
    assert all(0.0 <= item["value"] <= 1.0 for item in breakdown["components"])
    # Contributions must reconstruct the base score.
    total_contribution = sum(item["contribution"] for item in breakdown["components"])
    assert total_contribution == pytest.approx(breakdown["base_score"], abs=0.05)


def test_hook_strength_prefers_strong_openings():
    scorer = ViralScorer()
    strong = scorer.score_window(
        _transcript([(0.0, 8.0, "The biggest mistake I made was trusting everyone.", "S1")]),
        VisualAnalysis(),
        [],
        0.0,
        8.0,
    )
    weak = scorer.score_window(
        _transcript([(0.0, 8.0, "So anyway we continued talking about the weather.", "S1")]),
        VisualAnalysis(),
        [],
        0.0,
        8.0,
    )
    assert strong.components["hook_strength"] > weak.components["hook_strength"]
    assert strong.score > weak.score


def test_audio_events_increase_engagement_component():
    transcript = _transcript([(0.0, 20.0, "And then everybody started laughing loudly.", "S1")])
    without = ViralScorer().score_window(transcript, VisualAnalysis(), [], 0.0, 20.0)
    with_events = ViralScorer().score_window(
        transcript,
        VisualAnalysis(),
        [AudioEvent(kind="laughter", start=12.0, end=18.0, intensity=0.95)],
        0.0,
        20.0,
    )
    assert (
        with_events.components["audio_visual_engagement"]
        > without.components["audio_visual_engagement"]
    )
    assert with_events.components["emotional_intensity"] > without.components["emotional_intensity"]


def test_penalties_apply_for_incomplete_and_mid_sentence_spans():
    transcript = _transcript(
        [
            (0.0, 4.0, "and it kept going", "S1"),
            (4.0, 9.0, "without ever stopping", "S1"),
        ]
    )
    result = ViralScorer().score_window(transcript, VisualAnalysis(), [], 0.0, 9.0)
    rules = {item["rule"] for item in result.breakdown["penalties"]}
    assert "starts_mid_sentence" in rules
    assert "ends_on_incomplete_sentence" in rules
    assert result.breakdown["penalty_total"] > 0


def test_ranking_orders_by_descending_score():
    scorer = ViralScorer()
    good = scorer.score_window(
        _transcript([(0.0, 20.0, "The biggest mistake was trusting everyone.", "S1")]),
        VisualAnalysis(),
        [],
        0.0,
        20.0,
    )
    poor = scorer.score_window(
        _transcript([(0.0, 20.0, "and then we went on", "S1")]),
        VisualAnalysis(),
        [],
        0.0,
        20.0,
    )
    ranked = scorer.rank([("poor", poor), ("good", good)])
    assert [clip_id for clip_id, _, _ in ranked] == ["good", "poor"]
    assert [rank for _, _, rank in ranked] == [1, 2]


def test_duration_penalties_for_too_short_and_too_long():
    scorer = ViralScorer()
    short = scorer.score_window(
        _transcript([(0.0, 3.0, "Wow. Great.", "S1")]), VisualAnalysis(), [], 0.0, 3.0
    )
    long = scorer.score_window(
        _transcript([(0.0, 200.0, "The biggest mistake was trusting everyone.", "S1")]),
        VisualAnalysis(),
        [],
        0.0,
        200.0,
    )
    assert "too_short" in {item["rule"] for item in short.breakdown["penalties"]}
    assert "too_long" in {item["rule"] for item in long.breakdown["penalties"]}
