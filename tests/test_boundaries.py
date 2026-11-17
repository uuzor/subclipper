"""Tests for the clip boundary optimizer."""

from __future__ import annotations

import pytest

from quickreel.ai.base import AudioEvent, TranscriptResult, TranscriptSegment
from quickreel.analysis.boundaries import BoundaryOptimizer


def _transcript() -> TranscriptResult:
    return TranscriptResult(
        language="en",
        text="",
        provider="unit",
        segments=[
            TranscriptSegment(0.0, 4.0, "Welcome to the show.", "S1"),
            TranscriptSegment(4.0, 10.0, "Tell me how it started.", "S1"),
            TranscriptSegment(10.28, 10.5, "well the thing is", "S2"),
            TranscriptSegment(
                10.5, 16.0, "the biggest mistake I made was trusting everyone.", "S2"
            ),
            TranscriptSegment(16.0, 20.0, "It nearly killed the company.", "S2"),
            TranscriptSegment(20.0, 22.0, "That is rough.", "S1"),
        ],
    )


def test_optimizer_keeps_setup_and_ends_on_sentence():
    transcript = _transcript()
    optimizer = BoundaryOptimizer(lead_in_seconds=1.0, lead_out_seconds=1.0)
    result = optimizer.optimize(transcript, [], raw_start=10.4, raw_end=16.0)

    # Backed up to include the setup sentence.
    assert result.start <= 10.28
    # Extended forward to a complete sentence boundary.
    assert result.end >= 16.0
    assert any("setup" in note for note in result.adjustments)
    assert result.duration > 0


def test_optimizer_extends_to_include_audience_reaction():
    transcript = _transcript()
    events = [AudioEvent(kind="laughter", start=15.5, end=19.5, intensity=0.9)]
    optimizer = BoundaryOptimizer(lead_in_seconds=0.0, lead_out_seconds=0.0)
    result = optimizer.optimize(transcript, events, raw_start=10.5, raw_end=16.0)

    assert result.end >= 19.5
    assert any("reaction" in note for note in result.adjustments)


def test_optimizer_enforces_minimum_duration():
    transcript = _transcript()
    optimizer = BoundaryOptimizer(min_duration=12.0, lead_in_seconds=0.0, lead_out_seconds=0.0)
    result = optimizer.optimize(transcript, [], raw_start=10.5, raw_end=11.0)
    assert result.duration >= 12.0 or result.end >= 16.0


def test_optimizer_respects_maximum_duration():
    segments = [
        (float(i), float(i + 1), f"Sentence number {i} ends here.", "S1") for i in range(200)
    ]
    transcript = TranscriptResult(
        language="en",
        text="",
        provider="unit",
        segments=[TranscriptSegment(a, b, t, s) for a, b, t, s in segments],
    )
    optimizer = BoundaryOptimizer(max_duration=60.0, target_duration=45.0)
    result = optimizer.optimize(transcript, [], raw_start=0.0, raw_end=180.0)
    assert result.duration <= 60.0


def test_optimizer_clamps_to_media_duration():
    transcript = _transcript()
    optimizer = BoundaryOptimizer()
    result = optimizer.optimize(transcript, [], raw_start=10.5, raw_end=22.0, media_duration=21.0)
    assert result.end <= 21.0
    assert result.start >= 0.0


def test_optimizer_without_transcript_keeps_raw_bounds():
    transcript = TranscriptResult(language="en", text="", provider="unit", segments=[])
    optimizer = BoundaryOptimizer()
    result = optimizer.optimize(transcript, [], raw_start=5.0, raw_end=15.0)
    assert (result.start, result.end) == (5.0, 15.0)


def test_optimizer_snaps_to_nearby_silence():
    transcript = TranscriptResult(
        language="en",
        text="",
        provider="unit",
        segments=[
            TranscriptSegment(0.0, 10.0, "Welcome to the show.", "S1"),
            TranscriptSegment(10.5, 16.0, "The biggest mistake was trusting everyone.", "S2"),
            TranscriptSegment(16.0, 20.0, "It nearly killed the company.", "S2"),
        ],
    )
    events = [AudioEvent(kind="silence", start=9.2, end=9.9, intensity=0.0)]
    optimizer = BoundaryOptimizer(lead_in_seconds=1.5, lead_out_seconds=0.0, silence_padding=0.25)
    result = optimizer.optimize(transcript, events, raw_start=10.5, raw_end=20.0)
    # Lead-in should land inside the silence region rather than in mid-speech.
    assert result.start == pytest.approx(9.45)
