"""Real provider implementations.

Each class imports its heavy dependency lazily so that the optional ``ai`` extra
is only required when a provider is actually selected. Providers raise a clear
``ProviderUnavailable`` error rather than silently degrading, and the registry in
``quickreel.ai.registry`` decides whether to fall back to a fake.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from ..config import get_settings
from ..logging_setup import get_logger
from .base import (
    AudioEvent,
    CandidateMoment,
    MomentDetectionResult,
    SceneBoundary,
    TranscriptResult,
    TranscriptSegment,
    VisualAnalysis,
)

logger = get_logger(__name__)


class ProviderUnavailable(RuntimeError):
    """Raised when a provider's dependency or credentials are missing."""


class FasterWhisperTranscriptionProvider:
    name = "faster-whisper"

    def __init__(self) -> None:
        settings = get_settings()
        try:
            from faster_whisper import WhisperModel  # type: ignore import-not-found
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise ProviderUnavailable(
                "faster-whisper is not installed. Install with `pip install -e '.[ai]'`."
            ) from exc
        self._model = WhisperModel(
            settings.whisper_model,
            device=settings.whisper_device,
            compute_type=settings.whisper_compute_type,
        )

    def transcribe(self, audio_path: Path) -> TranscriptResult:
        segments_iter, info = self._model.transcribe(
            str(audio_path), word_timestamps=True, vad_filter=True
        )
        segments: list[TranscriptSegment] = []
        for segment in segments_iter:
            words = [
                {"start": w.start, "end": w.end, "word": w.word}
                for w in (segment.words or [])
            ]
            segments.append(
                TranscriptSegment(
                    start=float(segment.start),
                    end=float(segment.end),
                    text=segment.text.strip(),
                    words=words,
                )
            )
        text = " ".join(segment.text for segment in segments).strip()
        return TranscriptResult(
            language=getattr(info, "language", "en") or "en",
            text=text,
            segments=segments,
            provider=self.name,
        )


class GeminiVisionProvider:
    """Multimodal understanding: scene changes plus conversational context."""

    name = "gemini"

    _PROMPT = (
        "You are analysing a long-form video for short-form clip potential. "
        "Return strict JSON: {\"summary\": str, \"scenes\": [{\"time\": float}], "
        "\"highlights\": [{\"time\": float, \"note\": str}]}. "
        "Scenes are hard visual cuts. Highlights are visually notable moments."
    )

    def __init__(self) -> None:
        settings = get_settings()
        if not settings.gemini_api_key:
            raise ProviderUnavailable("QUICKREEL_GEMINI_API_KEY is not set.")
        try:
            import google.generativeai as genai  # type: ignore import-not-found
        except ImportError as exc:  # pragma: no cover - optional extra
            raise ProviderUnavailable(
                "google-generativeai is not installed. Install with `pip install -e '.[ai]'`."
            ) from exc
        genai.configure(api_key=settings.gemini_api_key)
        self._model = genai.GenerativeModel(settings.gemini_model)

    def analyze(self, video_path: Path, transcript: TranscriptResult) -> VisualAnalysis:
        uploaded = self._model._client.files.upload(file=str(video_path))  # type: ignore[attr-defined]
        try:
            response = self._model.generate_content(
                [self._PROMPT, transcript.text[:8000], uploaded],
                request_options={"timeout": 300},
            )
            payload = _extract_json(response.text)
        finally:
            try:
                self._model._client.files.delete(name=uploaded.name)  # type: ignore[attr-defined]
            except Exception:  # pragma: no cover - best effort cleanup
                logger.warning("gemini.file_cleanup_failed", file=getattr(uploaded, "name", None))

        scenes = [
            SceneBoundary(time=float(item["time"]))
            for item in payload.get("scenes", [])
            if "time" in item
        ]
        return VisualAnalysis(
            scenes=scenes,
            summary=payload.get("summary", ""),
            highlights=payload.get("highlights", []),
            provider=self.name,
        )


class LLMMomentProvider:
    """Reasoning stage: asks a chat model to nominate candidate moments."""

    name = "llm-moments"

    _SYSTEM = (
        "You select moments from transcripts that would make people stop scrolling. "
        "Favour unexpected revelations, controversial opinions, emotional reactions, "
        "humour and strong storytelling. Every moment must stand alone without the "
        "rest of the video. Return strict JSON: {\"moments\": [{\"start\": float, "
        "\"end\": float, \"title\": str, \"hook\": str, \"moment_type\": str, "
        "\"rationale\": str, \"excerpt\": str}]}."
    )

    def __init__(self) -> None:
        settings = get_settings()
        if not settings.llm_base_url or not settings.llm_model:
            raise ProviderUnavailable("QUICKREEL_LLM_BASE_URL / QUICKREEL_LLM_MODEL are not set.")

    def detect(
        self,
        transcript: TranscriptResult,
        visual: VisualAnalysis,
        audio_events: list[AudioEvent],
    ) -> MomentDetectionResult:
        settings = get_settings()
        import httpx

        rendered = "\n".join(
            f"[{segment.start:.1f}-{segment.end:.1f}] {segment.text}"
            for segment in transcript.segments
        )
        events = ", ".join(
            f"{event.kind}@{event.start:.1f}-{event.end:.1f}" for event in audio_events
        )
        user = (
            f"Visual summary: {visual.summary}\n"
            f"Audio events: {events}\n\n"
            f"Transcript:\n{rendered}"
        )
        response = httpx.post(
            f"{settings.llm_base_url.rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {settings.llm_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": settings.llm_model,
                "messages": [
                    {"role": "system", "content": self._SYSTEM},
                    {"role": "user", "content": user},
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0.2,
            },
            timeout=120,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        payload = _extract_json(content)
        moments = [
            CandidateMoment(
                start=float(item["start"]),
                end=float(item["end"]),
                title=item.get("title", ""),
                hook=item.get("hook", ""),
                moment_type=item.get("moment_type", "insight"),
                rationale=item.get("rationale", ""),
                excerpt=item.get("excerpt", ""),
            )
            for item in payload.get("moments", [])
            if "start" in item and "end" in item
        ]
        return MomentDetectionResult(moments=moments, provider=self.name)


class LibrosaAudioAnalyzer:
    """Detects laughter-like bursts, emphasis and silence from the audio signal."""

    name = "librosa"

    def __init__(self, hop_seconds: float = 0.5) -> None:
        self._hop = hop_seconds

    def analyze(self, audio_path: Path) -> list[AudioEvent]:
        try:
            import librosa  # type: ignore import-not-found
            import numpy as np  # type: ignore import-not-found
        except ImportError as exc:  # pragma: no cover - optional extra
            raise ProviderUnavailable(
                "librosa is not installed. Install with `pip install -e '.[ai]'`."
            ) from exc

        samples, sample_rate = librosa.load(str(audio_path), sr=16000, mono=True)
        rms = librosa.feature.rms(y=samples, hop_length=int(sample_rate * self._hop))[0]
        if rms.size == 0:
            return []
        threshold = float(np.percentile(rms, 85))
        silence_threshold = float(np.percentile(rms, 10))

        events: list[AudioEvent] = []
        in_burst = False
        burst_start = 0.0
        for index, value in enumerate(rms):
            time = index * self._hop
            if value >= threshold and not in_burst:
                in_burst = True
                burst_start = time
            elif value < threshold and in_burst:
                in_burst = False
                if time - burst_start >= 0.6:
                    events.append(
                        AudioEvent(
                            kind="emphasis",
                            start=burst_start,
                            end=time,
                            intensity=min(1.0, float(value / (threshold or 1.0))),
                        )
                    )
            elif value <= silence_threshold:
                events.append(
                    AudioEvent(kind="silence", start=time, end=time + self._hop, intensity=0.0)
                )
        return events


def _extract_json(text: str) -> dict:
    """Parse JSON from a model response, tolerating markdown code fences."""
    if not text:
        return {}
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```")[1]
        cleaned = cleaned.removeprefix("json").strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start == -1 or end == -1:
            return {}
        try:
            return json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError:
            logger.warning("provider.json_parse_failed", sample=cleaned[:200])
            return {}


def approximate_scene_count(duration_seconds: float, average_shot: float = 12.0) -> int:
    """Fallback scene estimate when no vision model is configured."""
    return max(1, int(math.ceil(duration_seconds / average_shot)))
