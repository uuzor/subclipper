"""Provider contracts for each AI stage of the pipeline.

The pipeline depends only on these protocols, never on a concrete model. That
keeps Whisper, Gemini, and the moment-selection LLM individually replaceable and
makes the whole system runnable offline via the bundled fake implementations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass(slots=True)
class TranscriptSegment:
    start: float
    end: float
    text: str
    speaker: str | None = None
    words: list[dict] = field(default_factory=list)


@dataclass(slots=True)
class TranscriptResult:
    language: str
    text: str
    segments: list[TranscriptSegment]
    provider: str


@dataclass(slots=True)
class AudioEvent:
    """A non-speech audio event used as a virality signal."""

    kind: str  # laughter | emphasis | music | silence | applause
    start: float
    end: float
    intensity: float  # 0..1


@dataclass(slots=True)
class SceneBoundary:
    time: float
    kind: str = "cut"


@dataclass(slots=True)
class VisualAnalysis:
    scenes: list[SceneBoundary] = field(default_factory=list)
    summary: str = ""
    highlights: list[dict] = field(default_factory=list)
    provider: str = "unknown"


@dataclass(slots=True)
class CandidateMoment:
    """A moment proposed by the reasoning stage, before boundary optimization."""

    start: float
    end: float
    title: str
    hook: str = ""
    moment_type: str = "insight"
    rationale: str = ""
    excerpt: str = ""


@dataclass(slots=True)
class MomentDetectionResult:
    moments: list[CandidateMoment]
    provider: str


@runtime_checkable
class TranscriptionProvider(Protocol):
    name: str

    def transcribe(self, audio_path: Path) -> TranscriptResult: ...


@runtime_checkable
class VisionProvider(Protocol):
    name: str

    def analyze(self, video_path: Path, transcript: TranscriptResult) -> VisualAnalysis: ...


@runtime_checkable
class MomentProvider(Protocol):
    name: str

    def detect(
        self,
        transcript: TranscriptResult,
        visual: VisualAnalysis,
        audio_events: list[AudioEvent],
    ) -> MomentDetectionResult: ...
