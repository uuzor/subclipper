"""Moment selection: detection -> dedup -> boundary optimization -> scoring -> rank.

This is the stage the product brief calls out as the differentiator. It is a pure
transformation over pipeline artifacts, so it can be tested without media or
network access.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..ai.base import (
    AudioEvent,
    CandidateMoment,
    MomentProvider,
    TranscriptResult,
    VisualAnalysis,
)
from ..logging_setup import get_logger
from .boundaries import BoundaryOptimizer, BoundaryResult
from .scoring import ScoreResult, ViralScorer

logger = get_logger(__name__)


@dataclass(slots=True)
class ScoredMoment:
    candidate: CandidateMoment
    boundary: BoundaryResult
    score: ScoreResult
    rank: int = 0

    @property
    def duration(self) -> float:
        return self.boundary.duration


class MomentSelector:
    def __init__(
        self,
        provider: MomentProvider,
        scorer: ViralScorer | None = None,
        optimizer: BoundaryOptimizer | None = None,
        overlap_tolerance: float = 0.6,
    ) -> None:
        self.provider = provider
        self.scorer = scorer or ViralScorer()
        self.optimizer = optimizer or BoundaryOptimizer()
        # Two moments sharing more than this fraction of the shorter one are
        # treated as duplicates; keep the higher-scoring one.
        self.overlap_tolerance = overlap_tolerance

    def select(
        self,
        transcript: TranscriptResult,
        visual: VisualAnalysis,
        audio_events: list[AudioEvent],
        media_duration: float | None = None,
        max_moments: int = 12,
    ) -> list[ScoredMoment]:
        detection = self.provider.detect(transcript, visual, audio_events)
        logger.info(
            "moments.detected",
            provider=detection.provider,
            count=len(detection.moments),
        )

        scored: list[ScoredMoment] = []
        for candidate in detection.moments:
            if candidate.end <= candidate.start:
                logger.debug("moments.skip_degenerate", start=candidate.start, end=candidate.end)
                continue
            boundary = self.optimizer.optimize(
                transcript,
                audio_events,
                candidate.start,
                candidate.end,
                media_duration=media_duration,
            )
            if boundary.duration <= 0:
                continue
            result = self.scorer.score_window(
                transcript,
                visual,
                audio_events,
                boundary.start,
                boundary.end,
            )
            scored.append(ScoredMoment(candidate=candidate, boundary=boundary, score=result))

        deduped = self._deduplicate(scored)
        ranked = sorted(deduped, key=lambda item: item.score.score, reverse=True)[:max_moments]
        for index, moment in enumerate(ranked):
            moment.rank = index + 1
        return ranked

    def _deduplicate(self, moments: list[ScoredMoment]) -> list[ScoredMoment]:
        # Process in descending score so the strongest overlapping moment wins.
        ordered = sorted(moments, key=lambda item: item.score.score, reverse=True)
        kept: list[ScoredMoment] = []
        for moment in ordered:
            if any(self._overlaps(moment, other) for other in kept):
                logger.debug("moments.dropped_overlap", start=moment.boundary.start)
                continue
            kept.append(moment)
        return kept

    def _overlaps(self, a: ScoredMoment, b: ScoredMoment) -> bool:
        start = max(a.boundary.start, b.boundary.start)
        end = min(a.boundary.end, b.boundary.end)
        overlap = max(0.0, end - start)
        if overlap == 0:
            return False
        shorter = min(a.duration, b.duration) or 1.0
        return overlap / shorter >= self.overlap_tolerance
