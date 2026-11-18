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
            TranscriptSegment(start, end, text, speaker) for start, end, text, speaker in segments
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


# --- regression tests for feature extraction ----------------------------------
#
# These pin the *inputs* the component functions depend on. They previously read
# fields that had not been assigned yet (speaker_changes, filler_ratio,
# question_opening), so the dialogue, cleanliness and question-bonus terms were
# silently dead for every clip.


def test_speaker_changes_feed_the_storytelling_component():
    """A back-and-forth exchange must score higher on storytelling than a monologue."""
    dialogue = _transcript(
        [
            (0.0, 5.0, "Then what happened next?", "S1"),
            (5.0, 10.0, "The investors finally agreed with him.", "S2"),
        ]
    )
    monologue = _transcript(
        [
            (0.0, 5.0, "Then what happened next?", "S1"),
            (5.0, 10.0, "The investors finally agreed with him.", "S1"),
        ]
    )
    scorer = ViralScorer()
    with_dialogue = scorer.score_window(dialogue, VisualAnalysis(), [], 0.0, 10.0)
    without = scorer.score_window(monologue, VisualAnalysis(), [], 0.0, 10.0)
    assert with_dialogue.features.speaker_changes == 1
    assert without.features.speaker_changes == 0
    assert with_dialogue.components["storytelling"] > without.components["storytelling"], (
        "speaker changes must influence storytelling"
    )


def test_question_opening_bonus_reaches_standalone_context():
    scorer = ViralScorer()
    question = scorer.score_window(
        _transcript([(0.0, 10.0, "What actually predicts success?", "S1")]),
        VisualAnalysis(),
        [],
        0.0,
        10.0,
    )
    statement = scorer.score_window(
        _transcript([(0.0, 10.0, "Something predicts success here.", "S1")]),
        VisualAnalysis(),
        [],
        0.0,
        10.0,
    )
    assert question.features.question_opening is True
    assert question.components["standalone_context"] > statement.components["standalone_context"], (
        "a question opening plus complete sentence must earn the bonus"
    )


def test_ordinary_words_are_not_treated_as_filler():
    """`of`, `you` and `know` are ordinary English, not verbal tics."""
    scorer = ViralScorer()
    fluent = scorer.score_window(
        _transcript(
            [(0.0, 12.0, "The best part of the story is that most of you know the answer.", "S1")]
        ),
        VisualAnalysis(),
        [],
        0.0,
        12.0,
    )
    assert fluent.features.filler_ratio == pytest.approx(0.0)
    assert "excessive_filler" not in {item["rule"] for item in fluent.breakdown["penalties"]}


def test_real_filler_is_still_detected_including_phrases():
    scorer = ViralScorer()
    filler_heavy = scorer.score_window(
        _transcript([(0.0, 12.0, "Um, you know, like, basically it was sort of fine.", "S1")]),
        VisualAnalysis(),
        [],
        0.0,
        12.0,
    )
    assert filler_heavy.features.filler_ratio > 0.3
    assert "excessive_filler" in {item["rule"] for item in filler_heavy.breakdown["penalties"]}


def test_filler_phrase_counts_once_not_word_by_word():
    """`you know` is one tic (2 words), not three separate hits."""
    from quickreel.analysis.features import _filler_ratio

    # "um" (1) + "you know" (2) = 3 of 6 words.
    assert _filler_ratio("um, you know, it was fine", 6) == pytest.approx(0.5)


def test_mid_sentence_you_know_is_not_filler():
    """The same words in ordinary syntax are not a discourse marker."""
    from quickreel.analysis.features import _filler_ratio

    assert _filler_ratio("most of you know the answer", 6) == pytest.approx(0.0)


def test_shareability_cleanliness_reflects_actual_filler():
    """Clean delivery must out-score a filler-heavy one of the same length."""
    scorer = ViralScorer()
    clean = scorer.score_window(
        _transcript([(0.0, 30.0, "Persistence and luck predict success far better.", "S1")]),
        VisualAnalysis(),
        [],
        0.0,
        30.0,
    )
    messy = scorer.score_window(
        _transcript([(0.0, 30.0, "Um, like, you know, basically sort of whatever.", "S1")]),
        VisualAnalysis(),
        [],
        0.0,
        30.0,
    )
    assert clean.components["shareability"] > messy.components["shareability"], (
        "cleanliness must be a live input to shareability"
    )
