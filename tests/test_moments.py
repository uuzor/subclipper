"""Tests for moment selection: detection, dedup, scoring and ranking."""

from __future__ import annotations

from quickreel.ai.base import (
    CandidateMoment,
    MomentDetectionResult,
    TranscriptResult,
    TranscriptSegment,
    VisualAnalysis,
)
from quickreel.analysis.moments import MomentSelector


class _StubProvider:
    name = "stub"

    def __init__(self, moments: list[CandidateMoment]) -> None:
        self._moments = moments

    def detect(self, transcript, visual, audio_events) -> MomentDetectionResult:
        return MomentDetectionResult(moments=self._moments, provider=self.name)


def _transcript() -> TranscriptResult:
    return TranscriptResult(
        language="en",
        text="",
        provider="unit",
        segments=[
            TranscriptSegment(0.0, 6.0, "The biggest mistake I made was trusting everyone.", "S1"),
            TranscriptSegment(6.0, 12.0, "It nearly killed the company.", "S1"),
            TranscriptSegment(12.0, 20.0, "University degrees do not guarantee success.", "S2"),
            TranscriptSegment(20.0, 28.0, "So what actually predicts success?", "S1"),
        ],
    )


def test_selector_ranks_and_assigns_rank_numbers():
    provider = _StubProvider(
        [
            CandidateMoment(0.0, 12.0, "Mistake", moment_type="revelation"),
            CandidateMoment(12.0, 20.0, "Degrees", moment_type="controversial_opinion"),
        ]
    )
    selector = MomentSelector(provider=provider)
    moments = selector.select(_transcript(), VisualAnalysis(), [])

    assert moments
    assert [m.rank for m in moments] == list(range(1, len(moments) + 1))
    scores = [m.score.score for m in moments]
    assert scores == sorted(scores, reverse=True)


def test_selector_drops_overlapping_duplicates_keeping_the_best():
    provider = _StubProvider(
        [
            CandidateMoment(0.0, 12.0, "Mistake A", moment_type="revelation"),
            CandidateMoment(0.5, 11.0, "Mistake B", moment_type="revelation"),
        ]
    )
    selector = MomentSelector(provider=provider, overlap_tolerance=0.5)
    moments = selector.select(_transcript(), VisualAnalysis(), [])
    assert len(moments) == 1


def test_selector_skips_degenerate_candidates():
    provider = _StubProvider(
        [
            CandidateMoment(10.0, 10.0, "Zero length"),
            CandidateMoment(0.0, 12.0, "Valid", moment_type="revelation"),
        ]
    )
    selector = MomentSelector(provider=provider)
    moments = selector.select(_transcript(), VisualAnalysis(), [])
    assert len(moments) == 1
    assert moments[0].candidate.title == "Valid"


def test_selector_respects_max_moments():
    candidates = [
        CandidateMoment(float(i * 30), float(i * 30 + 25), f"Moment {i}") for i in range(6)
    ]
    segments = [
        TranscriptSegment(float(i * 5), float(i * 5 + 5), f"Sentence {i} is complete.", "S1")
        for i in range(40)
    ]
    transcript = TranscriptResult(language="en", text="", provider="unit", segments=segments)
    selector = MomentSelector(provider=_StubProvider(candidates))
    moments = selector.select(transcript, VisualAnalysis(), [], max_moments=3)
    assert len(moments) == 3


def test_selector_carries_boundary_and_breakdown():
    provider = _StubProvider([CandidateMoment(0.0, 12.0, "Mistake", moment_type="revelation")])
    selector = MomentSelector(provider=provider)
    moment = selector.select(_transcript(), VisualAnalysis(), [], media_duration=30.0)[0]

    assert moment.boundary.adjustments
    assert moment.score.breakdown["components"]
    assert moment.duration > 0
    assert moment.boundary.end <= 30.0
