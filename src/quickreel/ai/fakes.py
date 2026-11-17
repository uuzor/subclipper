"""Deterministic offline providers.

These make the full pipeline executable with no API keys and no model weights,
which is what the test-suite and local development rely on. They are intentionally
simple but structurally faithful to the real providers.
"""

from __future__ import annotations

import math
import random
from pathlib import Path

from .base import (
    AudioEvent,
    CandidateMoment,
    MomentDetectionResult,
    SceneBoundary,
    TranscriptResult,
    TranscriptSegment,
    VisualAnalysis,
)

_FAKE_SCRIPT: list[tuple[float, float, str, str]] = [
    (
        0.0,
        6.5,
        "Welcome back to the show, today we are talking about building things.",
        "SPEAKER_1",
    ),
    (
        6.5,
        14.0,
        "Honestly the biggest mistake I made was trusting everyone around me.",
        "SPEAKER_1",
    ),
    (14.0, 19.5, "That is a strong statement. Can you unpack that for us?", "SPEAKER_2"),
    (
        19.5,
        29.0,
        "I gave away equity because I wanted to be liked, and it nearly killed the company.",
        "SPEAKER_1",
    ),
    (
        29.0,
        37.5,
        "University degrees do not guarantee success, and pretending they do hurts people.",
        "SPEAKER_2",
    ),
    (37.5, 45.0, "So what actually predicts whether a founder succeeds?", "SPEAKER_1"),
    (
        45.0,
        56.0,
        "Persistence, luck, and an unreasonable tolerance for being told you are wrong.",
        "SPEAKER_2",
    ),
    (56.0, 63.0, "And then everyone in the room started laughing at the same time.", "SPEAKER_1"),
    (
        63.0,
        72.0,
        "The surprising part is that the investors agreed with him immediately.",
        "SPEAKER_2",
    ),
    (
        72.0,
        84.0,
        "I think the real lesson is that nobody knows anything until they ship.",
        "SPEAKER_1",
    ),
]


class FakeTranscriptionProvider:
    """Returns a fixed, timestamped dialogue. Ignores audio content."""

    name = "fake-whisper"

    def __init__(self, duration_hint: float | None = None) -> None:
        self._duration_hint = duration_hint

    def transcribe(self, audio_path: Path) -> TranscriptResult:
        segments = [
            TranscriptSegment(start=start, end=end, text=text, speaker=speaker)
            for start, end, text, speaker in _FAKE_SCRIPT
        ]
        if self._duration_hint:
            segments = [s for s in segments if s.start < self._duration_hint] or segments[:1]
        text = " ".join(segment.text for segment in segments)
        return TranscriptResult(
            language="en",
            text=text,
            segments=segments,
            provider=self.name,
        )


class FakeVisionProvider:
    """Emits evenly spaced scene cuts and a canned multimodal summary."""

    name = "fake-vision"

    def analyze(self, video_path: Path, transcript: TranscriptResult) -> VisualAnalysis:
        end = transcript.segments[-1].end if transcript.segments else 60.0
        scenes = [SceneBoundary(time=t) for t in _frange(0.0, end, 12.0)]
        return VisualAnalysis(
            scenes=scenes,
            summary=(
                "Two speakers, medium shot, occasional cutaways. Energy rises in the "
                "second half when the conversation turns to failure and equity."
            ),
            highlights=[
                {"time": 14.0, "note": "speaker leans in, gesture emphasis"},
                {"time": 56.0, "note": "both speakers laughing, wide smiles"},
            ],
            provider=self.name,
        )


class FakeAudioAnalyzer:
    """Produces laughter/emphasis events at fixed, plausible positions."""

    name = "fake-audio"

    def analyze(self, audio_path: Path) -> list[AudioEvent]:
        return [
            AudioEvent(kind="emphasis", start=19.5, end=29.0, intensity=0.8),
            AudioEvent(kind="laughter", start=56.0, end=59.5, intensity=0.95),
            AudioEvent(kind="emphasis", start=63.0, end=72.0, intensity=0.7),
        ]


class FakeMomentProvider:
    """Selects moments with simple lexical heuristics, no network calls."""

    name = "fake-moments"

    _HOOK_WORDS = ("mistake", "wrong", "never", "nobody", "guarantee", "surprising", "truth")
    _EMOTION_WORDS = ("honestly", "killed", "hurt", "afraid", "love", "hate", "laughed")
    _CONTRAST_WORDS = ("but", "however", "actually", "instead", "yet")

    def detect(
        self,
        transcript: TranscriptResult,
        visual: VisualAnalysis,
        audio_events: list[AudioEvent],
    ) -> MomentDetectionResult:
        segments = transcript.segments
        moments: list[CandidateMoment] = []
        # Slide a window of two adjacent segments so moments carry setup + payoff.
        for index in range(len(segments)):
            window = segments[index : index + 2]
            excerpt = " ".join(seg.text for seg in window)
            lowered = excerpt.lower()
            if not any(word in lowered for word in self._HOOK_WORDS + self._CONTRAST_WORDS):
                continue
            moments.append(
                CandidateMoment(
                    start=window[0].start,
                    end=window[-1].end,
                    title=window[0].text[:60].rstrip(".") or "Untitled moment",
                    hook=window[0].text,
                    moment_type=self._classify(lowered),
                    rationale="Contains hook language or a contrast that reads as a reveal.",
                    excerpt=excerpt,
                )
            )
        if not moments:
            first = segments[0]
            moments.append(
                CandidateMoment(
                    start=first.start,
                    end=segments[min(1, len(segments) - 1)].end,
                    title=first.text[:60],
                    hook=first.text,
                    moment_type="insight",
                    rationale="Fallback: opening segment.",
                    excerpt=first.text,
                )
            )
        return MomentDetectionResult(moments=moments, provider=self.name)

    def _classify(self, lowered: str) -> str:
        if "laugh" in lowered or any(w in lowered for w in ("funny", "joke")):
            return "humor"
        if any(w in lowered for w in ("guarantee", "do not", "never", "wrong")):
            return "controversial_opinion"
        if any(w in lowered for w in self._EMOTION_WORDS):
            return "emotional_reaction"
        if any(w in lowered for w in ("surprising", "secret", "mistake")):
            return "revelation"
        return "insight"


def _frange(start: float, stop: float, step: float) -> list[float]:
    count = max(0, int(math.ceil((stop - start) / step)))
    return [start + i * step for i in range(count)]


def seeded_random(seed: int) -> random.Random:
    """Deterministic RNG for any stochastic fakes in tests."""
    return random.Random(seed)
