"""Feature extraction used by the viral scoring engine.

Features are derived purely from pipeline artifacts (transcript, audio events,
visual analysis), so the scorer stays a pure function that is trivial to test and
to recalibrate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..ai.base import AudioEvent, TranscriptResult, VisualAnalysis

_HOOK_PATTERNS = (
    r"\bthe (biggest|worst|best|only|real) (mistake|reason|lesson|truth)\b",
    r"\bnobody (knows|tells|talks|wants)\b",
    r"\b(most people|everyone) (thinks|believes|gets)\b",
    r"\b(never|always|guarantee|guaranteed)\b",
    r"\bwhat (actually|really) (happens|matters|works)\b",
    r"^\s*(how|why|what|when)\b",
)

_EMOTION_WORDS = frozenset(
    """love hate fear afraid angry furious thrilled excited devastated heartbroken
    amazing terrible awful incredible wonderful shocked stunned embarrassed proud
    cried laugh laughed laughing hilarious funny scared terrified regret mistake""".split()
)

_CONTRAVERSY_WORDS = frozenset(
    """wrong never everyone nobody stupid ridiculous controversial debate disagree
    actually however instead but truth myth overrated""".split()
)

_STORY_WORDS = frozenset(
    """remember story happened suddenly then because when finally realized
    discovered learned lesson years later""".split()
)

# Single-word verbal tics. Deliberately excludes ordinary words that only look
# like filler in isolation: counting "of", "you" or "know" as filler flagged
# almost every sentence, including fluent ones.
_FILLER_WORDS = frozenset("um uh er ah basically literally like".split())

# Multi-word tics, matched against the joined window text so they are not split
# across segments. They only count when set off by punctuation, because the same
# words occur in ordinary speech ("most of you know the answer" is not filler).
_FILLER_PHRASES = ("you know", "sort of", "kind of", "i mean", "you see")
_FILLER_PHRASE_RE = re.compile(
    r"(?:^|[,.!?;:])\s*(?:" + "|".join(_FILLER_PHRASES) + r")\s*(?=[,.!?;:]|$)",
    re.IGNORECASE,
)


@dataclass(slots=True)
class MomentFeatures:
    """Normalised (0..1) signals for a candidate clip window."""

    duration_seconds: float
    word_count: int
    words_per_second: float

    hook_strength: float = 0.0
    emotional_intensity: float = 0.0
    storytelling: float = 0.0
    shareability: float = 0.0
    audio_visual_engagement: float = 0.0
    standalone_context: float = 0.0

    question_opening: bool = False
    ends_on_complete_sentence: bool = False
    starts_mid_sentence: bool = False
    filler_ratio: float = 0.0
    silence_ratio: float = 0.0
    speaker_changes: int = 0
    audio_event_kinds: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "duration_seconds": round(self.duration_seconds, 2),
            "word_count": self.word_count,
            "words_per_second": round(self.words_per_second, 2),
            "hook_strength": round(self.hook_strength, 3),
            "emotional_intensity": round(self.emotional_intensity, 3),
            "storytelling": round(self.storytelling, 3),
            "shareability": round(self.shareability, 3),
            "audio_visual_engagement": round(self.audio_visual_engagement, 3),
            "standalone_context": round(self.standalone_context, 3),
            "question_opening": self.question_opening,
            "ends_on_complete_sentence": self.ends_on_complete_sentence,
            "starts_mid_sentence": self.starts_mid_sentence,
            "filler_ratio": round(self.filler_ratio, 3),
            "silence_ratio": round(self.silence_ratio, 3),
            "speaker_changes": self.speaker_changes,
            "audio_event_kinds": self.audio_event_kinds,
            "notes": self.notes,
        }


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _filler_ratio(lowered: str, word_count: int) -> float:
    """Fraction of the window that is verbal filler.

    Phrases are matched only when punctuated as discourse markers, so the words
    inside them are never also counted as single-word tics.
    """
    if word_count == 0:
        return 0.0
    hits = sum(
        len(match.group(0).strip(" ,.!?;:").split())
        for match in _FILLER_PHRASE_RE.finditer(lowered)
    )
    hits += sum(1 for word in re.findall(r"[\w']+", lowered) if word in _FILLER_WORDS)
    return _clamp(hits / max(1, word_count))


def _window_segments(transcript: TranscriptResult, start: float, end: float) -> list:
    return [
        segment for segment in transcript.segments if segment.end > start and segment.start < end
    ]


def extract_features(
    transcript: TranscriptResult,
    visual: VisualAnalysis,
    audio_events: list[AudioEvent],
    start: float,
    end: float,
) -> MomentFeatures:
    """Compute virality features for the window ``[start, end)``."""
    duration = max(0.1, end - start)
    segments = _window_segments(transcript, start, end)
    text = " ".join(segment.text for segment in segments).strip()
    lowered = text.lower()
    words = [w for w in re.findall(r"[\w']+", lowered)]
    word_count = len(words)

    features = MomentFeatures(
        duration_seconds=duration,
        word_count=word_count,
        words_per_second=word_count / duration,
    )

    # Populate the scalar signals first. The component functions below read
    # speaker_changes, filler_ratio and question_opening, so computing them
    # afterwards would score every clip against default (zero) inputs.
    features.question_opening = bool(segments) and segments[0].text.strip().endswith("?")
    features.filler_ratio = _filler_ratio(lowered, word_count)
    silences = [
        event
        for event in audio_events
        if event.kind == "silence" and event.start >= start and event.end <= end
    ]
    features.silence_ratio = _clamp(sum(event.end - event.start for event in silences) / duration)
    features.speaker_changes = _count_speaker_changes(segments)
    features.audio_event_kinds = sorted(
        {
            event.kind
            for event in audio_events
            if event.kind != "silence" and event.end > start and event.start < end
        }
    )

    # Component scores. ``standalone_context`` also sets starts_mid_sentence and
    # ends_on_complete_sentence, which the penalty rules consume.
    features.hook_strength = _hook_strength(segments, lowered)
    features.emotional_intensity = _emotional_intensity(words, audio_events, start, end, duration)
    features.storytelling = _storytelling(lowered, words, features)
    features.shareability = _shareability(words, features)
    features.audio_visual_engagement = _audio_visual_engagement(
        audio_events, visual, start, end, duration
    )
    features.standalone_context = _standalone_context(segments, features)
    return features


def _hook_strength(segments: list, lowered: str) -> float:
    if not segments:
        return 0.0
    opening = segments[0].text.lower()
    score = 0.0
    for pattern in _HOOK_PATTERNS:
        if re.search(pattern, opening, flags=re.IGNORECASE | re.MULTILINE):
            score += 0.35
    if opening.strip().endswith("?"):
        score += 0.25
    if any(word in lowered for word in _CONTRAVERSY_WORDS):
        score += 0.2
    # Numbers and superlatives tend to pull attention in the first seconds.
    if re.search(r"\b\d+\b", opening):
        score += 0.1
    return _clamp(score)


def _emotional_intensity(
    words: list[str],
    audio_events: list[AudioEvent],
    start: float,
    end: float,
    duration: float,
) -> float:
    if not words:
        lexical = 0.0
    else:
        hits = sum(1 for word in words if word in _EMOTION_WORDS)
        lexical = _clamp(hits / max(1, len(words)) * 12)

    window_events = [
        event
        for event in audio_events
        if event.kind in {"laughter", "emphasis", "applause"}
        and event.end > start
        and event.start < end
    ]
    if window_events:
        overlap = sum(
            max(0.0, min(event.end, end) - max(event.start, start)) for event in window_events
        )
        peak = max(event.intensity for event in window_events)
        audio = _clamp(0.5 * _clamp(overlap / duration * 2) + 0.5 * peak)
    else:
        audio = 0.0
    return _clamp(0.55 * lexical + 0.45 * audio)


def _storytelling(lowered: str, words: list[str], features: MomentFeatures) -> float:
    if not words:
        return 0.0
    hits = sum(1 for word in words if word in _STORY_WORDS)
    lexical = _clamp(hits / max(1, len(words)) * 15)
    # Back-and-forth signals a conversation with a payoff rather than a monologue.
    dialogue = _clamp(features.speaker_changes / 3)
    temporal = 0.2 if re.search(r"\b(then|after|before|finally|later)\b", lowered) else 0.0
    return _clamp(0.6 * lexical + 0.25 * dialogue + temporal)


def _shareability(words: list[str], features: MomentFeatures) -> float:
    if not words:
        return 0.0
    quotable = sum(1 for word in words if word in _CONTRAVERSY_WORDS)
    lexical = _clamp(quotable / max(1, len(words)) * 14)
    # 15-60s clips share best; penalise outliers.
    duration = features.duration_seconds
    if 15 <= duration <= 60:
        length_fit = 1.0
    elif duration < 15:
        length_fit = duration / 15
    else:
        length_fit = _clamp(1 - (duration - 60) / 120)
    cleanliness = 1 - features.filler_ratio
    return _clamp(0.4 * lexical + 0.35 * length_fit + 0.25 * cleanliness)


def _audio_visual_engagement(
    audio_events: list[AudioEvent],
    visual: VisualAnalysis,
    start: float,
    end: float,
    duration: float,
) -> float:
    loud = [
        event
        for event in audio_events
        if event.kind in {"laughter", "emphasis", "applause"}
        and event.end > start
        and event.start < end
    ]
    audio_score = _clamp(
        sum(
            event.intensity * _clamp((min(event.end, end) - max(event.start, start)) / duration * 3)
            for event in loud
        )
    )
    scene_hits = sum(1 for scene in visual.scenes if start <= scene.time <= end)
    visual_score = _clamp(scene_hits / max(1, duration / 20) * 0.5)
    highlights = sum(1 for item in visual.highlights if start <= float(item.get("time", -1)) <= end)
    highlight_score = _clamp(highlights / 2)
    return _clamp(0.5 * audio_score + 0.3 * visual_score + 0.2 * highlight_score)


def _standalone_context(segments: list, features: MomentFeatures) -> float:
    if not segments:
        return 0.0
    # Start below 1.0 so the positive signals below can actually raise the score;
    # starting at the ceiling made the question bonus a no-op.
    score = 0.85
    first = segments[0].text.strip()
    last = segments[-1].text.strip()

    starts_lower = first[:1].islower()
    features.starts_mid_sentence = starts_lower or bool(
        re.match(r"^(and|but|so|because|which|that)\b", first, flags=re.IGNORECASE)
    )
    if features.starts_mid_sentence:
        score -= 0.35

    features.ends_on_complete_sentence = bool(last) and last[-1] in ".!?\"'"
    if not features.ends_on_complete_sentence:
        score -= 0.3

    # A clip that opens and closes a thought reads as self-contained.
    if features.question_opening and features.ends_on_complete_sentence:
        score += 0.15

    # Very short clips rarely carry a full idea; very long ones dilute the payoff.
    duration = features.duration_seconds
    if duration < 8:
        score -= 0.25
    elif duration > 90:
        score -= 0.15
    return _clamp(score)


def _count_speaker_changes(segments: list) -> int:
    changes = 0
    previous = None
    for segment in segments:
        speaker = getattr(segment, "speaker", None)
        if speaker and previous and speaker != previous:
            changes += 1
        if speaker:
            previous = speaker
    return changes
