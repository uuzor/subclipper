"""Clip boundary optimizer.

A detected moment is a *semantic* span, not a cut point. This module expands and
trims that span so the exported clip opens on a clean sentence, keeps the setup
that makes the payoff land, and ends after any trailing reaction (laughter,
applause) instead of mid-word.

Example from the product brief:

    statement starts 10:32, explanation begins 10:28,
    statement ends 10:48, laughter at 10:50  ->  clip 10:26 - 10:54
"""

from __future__ import annotations

from dataclasses import dataclass

from ..ai.base import AudioEvent, TranscriptResult, TranscriptSegment
from ..logging_setup import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class BoundaryResult:
    start: float
    end: float
    raw_start: float
    raw_end: float
    adjustments: list[str]
    sentence_start_index: int
    sentence_end_index: int

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


class BoundaryOptimizer:
    def __init__(
        self,
        lead_in_seconds: float = 2.0,
        lead_out_seconds: float = 2.0,
        reaction_tail_seconds: float = 4.0,
        min_duration: float = 8.0,
        max_duration: float = 90.0,
        target_duration: float = 45.0,
        silence_padding: float = 0.25,
    ) -> None:
        self.lead_in_seconds = lead_in_seconds
        self.lead_out_seconds = lead_out_seconds
        self.reaction_tail_seconds = reaction_tail_seconds
        self.min_duration = min_duration
        self.max_duration = max_duration
        self.target_duration = target_duration
        self.silence_padding = silence_padding

    def optimize(
        self,
        transcript: TranscriptResult,
        audio_events: list[AudioEvent],
        raw_start: float,
        raw_end: float,
        media_duration: float | None = None,
    ) -> BoundaryResult:
        segments = transcript.segments
        adjustments: list[str] = []

        if not segments:
            start, end = raw_start, raw_end
            return BoundaryResult(
                start=start,
                end=end,
                raw_start=raw_start,
                raw_end=raw_end,
                adjustments=["no transcript; kept raw boundaries"],
                sentence_start_index=-1,
                sentence_end_index=-1,
            )

        start_index = self._segment_index_covering(segments, raw_start)
        end_index = self._segment_index_covering(segments, raw_end)
        if end_index < start_index:
            end_index = start_index

        # 1. Include the setup sentence so the payoff is understandable.
        if self._starts_mid_sentence(segments[start_index]) and start_index > 0:
            start_index -= 1
            adjustments.append("extended back to include setup sentence")

        # 2. Preserve complete sentences at the tail.
        end_index = self._extend_to_sentence_end(segments, end_index)
        adjustments.append("snapped end to sentence boundary")

        start = segments[start_index].start
        end = segments[end_index].end

        # 3. Include a trailing reaction (laughter, applause) that belongs to the moment.
        reaction_end = self._reaction_end(audio_events, end)
        if reaction_end > end:
            end = reaction_end
            adjustments.append("extended to include audience reaction")

        # 4. Lead-in / lead-out breathing room, snapped to nearby silence.
        start = self._snap_earlier_to_silence(audio_events, start, self.lead_in_seconds)
        end = self._snap_later_to_silence(audio_events, end, self.lead_out_seconds)

        # 5. Enforce duration limits.
        start, end, duration_notes = self._enforce_duration(segments, start, end)
        adjustments.extend(duration_notes)

        if media_duration:
            start = max(0.0, min(start, media_duration))
            end = max(0.0, min(end, media_duration))

        if end <= start:
            end = min(start + self.min_duration, media_duration or start + self.min_duration)
            adjustments.append("degenerate span; applied minimum duration")

        return BoundaryResult(
            start=round(start, 3),
            end=round(end, 3),
            raw_start=raw_start,
            raw_end=raw_end,
            adjustments=adjustments,
            sentence_start_index=start_index,
            sentence_end_index=end_index,
        )

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _segment_index_covering(segments: list[TranscriptSegment], time: float) -> int:
        for index, segment in enumerate(segments):
            if segment.start <= time <= segment.end:
                return index
        # Fall back to nearest segment by start time.
        nearest = 0
        best = float("inf")
        for index, segment in enumerate(segments):
            distance = abs(segment.start - time)
            if distance < best:
                best = distance
                nearest = index
        return nearest

    @staticmethod
    def _starts_mid_sentence(segment: TranscriptSegment) -> bool:
        text = segment.text.strip()
        if not text:
            return False
        return text[0].islower()

    @staticmethod
    def _extend_to_sentence_end(segments: list[TranscriptSegment], index: int) -> int:
        """Move forward until the segment's text terminates a sentence."""
        for candidate in range(index, len(segments)):
            text = segments[candidate].text.strip()
            if text and text[-1] in ".!?\"'":
                return candidate
        return index

    def _reaction_end(self, audio_events: list[AudioEvent], end: float) -> float:
        best = end
        for event in audio_events:
            if event.kind not in {"laughter", "applause"}:
                continue
            # Reaction starts at or just before the moment ends.
            if end - 1.5 <= event.start <= end + self.reaction_tail_seconds:
                best = max(best, event.end)
        return best

    def _snap_earlier_to_silence(
        self, audio_events: list[AudioEvent], start: float, lead_in: float
    ) -> float:
        target = max(0.0, start - lead_in)
        for event in audio_events:
            if event.kind != "silence":
                continue
            if target <= event.start <= start:
                return max(0.0, event.start + self.silence_padding)
        return target

    def _snap_later_to_silence(
        self, audio_events: list[AudioEvent], end: float, lead_out: float
    ) -> float:
        target = end + lead_out
        for event in audio_events:
            if event.kind != "silence":
                continue
            if end <= event.start <= target:
                return max(end, event.start + self.silence_padding)
        return target

    def _enforce_duration(
        self, segments: list[TranscriptSegment], start: float, end: float
    ) -> tuple[float, float, list[str]]:
        notes: list[str] = []
        duration = end - start

        if duration < self.min_duration:
            needed = self.min_duration - duration
            end = self._extend_to_sentence_end_time(segments, end, needed)
            notes.append("extended to reach minimum duration")

        duration = end - start
        if duration > self.max_duration:
            # Trim from the end first (protect the hook), then from the start.
            overflow = duration - self.target_duration
            new_start = self._advance_to_sentence_start(segments, start, overflow)
            if new_start > start:
                start = new_start
                notes.append("trimmed opening to fit maximum duration")
            if end - start > self.max_duration:
                end = self._trim_to_sentence_end(segments, start, self.max_duration)
                notes.append("trimmed ending to fit maximum duration")

        return start, end, notes

    @staticmethod
    def _extend_to_sentence_end_time(
        segments: list[TranscriptSegment], end: float, needed: float
    ) -> float:
        for segment in segments:
            if segment.end >= end + needed and segment.text.strip().endswith((".", "!", "?")):
                return segment.end
        return end + needed

    @staticmethod
    def _advance_to_sentence_start(
        segments: list[TranscriptSegment], start: float, overflow: float
    ) -> float:
        target = start + overflow
        for segment in segments:
            if segment.start >= target and segment.text.strip()[:1].isupper():
                return segment.start
        return start

    @staticmethod
    def _trim_to_sentence_end(
        segments: list[TranscriptSegment], start: float, max_duration: float
    ) -> float:
        limit = start + max_duration
        last = start + max_duration
        for segment in segments:
            if start < segment.end <= limit and segment.text.strip().endswith((".", "!", "?")):
                last = segment.end
        return last
