"""Real provider implementations.

Each class imports its heavy dependency lazily so that the optional ``ai`` extra
is only required when a provider is actually selected. Providers raise a clear
``ProviderUnavailable`` error rather than silently degrading, and the registry in
``quickreel.ai.registry`` decides whether to fall back to a fake.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import math
from pathlib import Path

from ..config import get_settings
from ..logging_setup import get_logger
from ..media import ffmpeg
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
                {"start": w.start, "end": w.end, "word": w.word} for w in (segment.words or [])
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
    """Multimodal understanding: scene changes plus conversational context.

    Long-form video is never uploaded whole. Frames are sampled at a fixed
    interval (see ``ffmpeg.extract_frames``) and sent as inline images, so a
    2-hour podcast costs a bounded number of tokens and a bounded upload.
    """

    name = "gemini"

    _PROMPT = (
        "You are analysing a long-form video for short-form clip potential. "
        "The images are frames sampled in time order, roughly every {interval:.0f} "
        'seconds starting at 0. Return strict JSON: {{"summary": str, '
        '"scenes": [{{"time": float}}], "highlights": [{{"time": float, '
        '"note": str}}]}}. Scenes are hard visual cuts. Highlights are visually '
        "notable moments. Times are in seconds from the start of the video."
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
        settings = get_settings()
        frames_dir = settings.artifacts_dir / f"{Path(video_path).stem}_frames"
        frames = ffmpeg.extract_frames(
            video_path,
            frames_dir,
            interval_seconds=settings.vision_frame_interval_seconds,
            max_frames=settings.vision_max_frames,
        )
        prompt = self._PROMPT.format(interval=settings.vision_frame_interval_seconds)
        # The transcript is trimmed for context only; the frames carry the visual
        # signal and the transcript is never the sole input.
        parts: list = [prompt, transcript.text[:8000]]
        parts.extend(self._load_frame(frame) for frame in frames)

        response = self._model.generate_content(
            parts,
            request_options={"timeout": settings.vision_timeout_seconds},
        )
        payload = _extract_json(response.text)

        scenes = [
            SceneBoundary(time=float(item["time"]))
            for item in payload.get("scenes", [])
            if isinstance(item, dict) and "time" in item
        ]
        if not scenes:
            # A model that returns no cuts should not leave the pipeline without
            # visual structure; fall back to ffmpeg's own scene detector.
            scenes = [
                SceneBoundary(time=time)
                for time in ffmpeg.detect_scenes(
                    video_path, threshold=settings.vision_scene_threshold
                )
            ]
        return VisualAnalysis(
            scenes=scenes,
            summary=payload.get("summary", ""),
            highlights=payload.get("highlights", []),
            provider=self.name,
        )

    @staticmethod
    def _load_frame(path: Path) -> dict:
        """Inline a sampled frame as a blob the SDK accepts.

        The SDK rejects raw ``bytes`` and ``PIL`` is not a dependency, so build
        the ``{"mime_type", "data"}`` dict form explicitly.
        """
        mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
        return {"mime_type": mime, "data": path.read_bytes()}


class _NvidiaClient:
    """Shared OpenAI-compatible client for the NVIDIA NIM hosted models.

    Every NIM chat model answers on the same ``/chat/completions`` shape, so one
    small client serves speech, vision and reasoning. Two quirks matter:

    * Reasoning models spend completion tokens on ``reasoning_content`` before
      emitting ``content``. A small ``max_tokens`` therefore returns an empty
      string with ``finish_reason: stop``, which looks like a model failure.
      Every call here budgets for that and retries once with more room.
    * ``temperature=0`` is not accepted by all of them, so it is omitted.
    """

    def __init__(self, *, api_key: str, base_url: str, timeout: int, transport=None) -> None:
        if not api_key:
            raise ProviderUnavailable("QUICKREEL_NVIDIA_API_KEY is not set.")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._transport = transport

    def chat(self, *, model: str, messages: list, max_tokens: int = 4096) -> str:
        import httpx

        # Reasoning models can burn the whole budget before writing an answer, so
        # a single retry with double the room turns an empty response into a
        # usable one without failing the stage.
        for attempt, budget in enumerate((max_tokens, max_tokens * 3), start=1):
            try:
                with httpx.Client(timeout=self._timeout, transport=self._transport) as client:
                    response = client.post(
                        f"{self._base_url}/chat/completions",
                        headers={
                            "Authorization": f"Bearer {self._api_key}",
                            "Content-Type": "application/json",
                        },
                        json={"model": model, "messages": messages, "max_tokens": budget},
                    )
                    response.raise_for_status()
                    payload = response.json()
                choice = payload["choices"][0]
                content = (choice["message"].get("content") or "").strip()
                if content:
                    return content
                logger.warning(
                    "nvidia.empty_content",
                    model=model,
                    attempt=attempt,
                    finish_reason=choice.get("finish_reason"),
                    reasoning_tokens=(
                        (payload.get("usage") or {})
                        .get("completion_tokens_details", {})
                        .get("reasoning_tokens")
                    ),
                )
            except Exception as exc:  # noqa: BLE001 - httpx and KeyError alike
                logger.warning(
                    "nvidia.request_failed", model=model, attempt=attempt, error=str(exc)
                )
                if attempt == 2:
                    raise ProviderUnavailable(f"NVIDIA request failed: {exc}") from exc
        raise ProviderUnavailable(
            f"NVIDIA model {model} returned no content; it spent the token budget on reasoning"
        )


class NvidiaTranscriptionProvider:
    """Speech recognition through a hosted omni model.

    The production image carries no Whisper weights, so this is what makes
    transcription possible on a small container. Audio is cut into bounded
    chunks and each chunk is transcribed separately, which keeps every upload
    small and lets timestamps be offset back onto the source timeline.
    """

    name = "nvidia-omni"

    _PROMPT = (
        "Transcribe this audio verbatim, including filler words and false starts. "
        "Return strict JSON only: "
        '{"language": str, "segments": [{"start": float, "end": float, "text": str}]}. '
        "Times are seconds from the start of this audio. Do not translate."
    )

    def __init__(self, *, client: _NvidiaClient | None = None) -> None:
        settings = get_settings()
        self._settings = settings
        self._client = client or _NvidiaClient(
            api_key=settings.nvidia_api_key,
            base_url=settings.nvidia_base_url,
            timeout=settings.nvidia_timeout_seconds,
        )

    def transcribe(self, audio_path: Path) -> TranscriptResult:
        settings = self._settings
        audio_path = Path(audio_path)
        total_seconds = ffmpeg.probe(audio_path).duration_seconds or 0.0
        chunk = max(settings.nvidia_asr_chunk_seconds, 10.0)
        if total_seconds <= 0:
            offsets = [0.0]
        else:
            # ceil, not floor+1: a 20s source cut into 10s chunks is two chunks,
            # and the extra one would be an empty slice past the end.
            count = math.ceil(total_seconds / chunk)
            # Encoders pad the container, so a 20s file can probe as 20.1s. A
            # sliver tail is not worth a whole extra request.
            if count > 1 and total_seconds - (count - 1) * chunk < 1.0:
                count -= 1
            count = min(count, max(settings.nvidia_asr_max_chunks, 1))
            offsets = [index * chunk for index in range(max(count, 1))]

        segments: list[TranscriptSegment] = []
        language = "en"
        for offset in offsets:
            piece = ffmpeg.slice_audio(
                audio_path,
                settings.artifacts_dir / f"{audio_path.stem}_asr_{int(offset)}.wav",
                start=offset,
                duration=chunk,
            )
            payload = _extract_json(self._transcribe_chunk(piece))
            language = payload.get("language") or language
            for item in payload.get("segments", []):
                if not isinstance(item, dict) or "text" not in item:
                    continue
                text = str(item["text"]).strip()
                if not text:
                    continue
                start = float(item.get("start", 0.0)) + offset
                end = float(item.get("end", start)) + offset
                segments.append(
                    TranscriptSegment(
                        start=start,
                        end=max(end, start),
                        text=text,
                        words=[],
                    )
                )
        if not segments:
            raise ProviderUnavailable("NVIDIA transcription returned no segments")
        segments.sort(key=lambda segment: segment.start)
        return TranscriptResult(
            language=str(language),
            text=" ".join(segment.text for segment in segments).strip(),
            segments=segments,
            provider=self.name,
        )

    def _transcribe_chunk(self, path: Path) -> str:
        import base64

        encoded = base64.b64encode(path.read_bytes()).decode()
        return self._client.chat(
            model=self._settings.nvidia_asr_model,
            max_tokens=8192,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": self._PROMPT},
                        {
                            "type": "input_audio",
                            "input_audio": {"data": encoded, "format": "wav"},
                        },
                    ],
                }
            ],
        )


class NvidiaVisionProvider:
    """Visual understanding: sampled frames plus the transcript summary."""

    name = "nvidia-vl"

    _PROMPT = (
        "You are analysing a long-form video for short-form clip potential. "
        "The images are frames sampled in time order, roughly every {interval:.0f} "
        "seconds starting at 0. Return strict JSON only: "
        '{{"summary": str, "scenes": [{{"time": float}}], '
        '"highlights": [{{"time": float, "note": str}}]}}. '
        "Scenes are hard visual cuts; highlights are visually notable moments. "
        "Times are seconds from the start of the video."
    )

    def __init__(self, *, client: _NvidiaClient | None = None) -> None:
        settings = get_settings()
        self._settings = settings
        self._client = client or _NvidiaClient(
            api_key=settings.nvidia_api_key,
            base_url=settings.nvidia_base_url,
            timeout=settings.vision_timeout_seconds,
        )

    def analyze(self, video_path: Path, transcript: TranscriptResult) -> VisualAnalysis:
        settings = self._settings
        frames_dir = settings.artifacts_dir / f"{Path(video_path).stem}_frames"
        frames = ffmpeg.extract_frames(
            video_path,
            frames_dir,
            interval_seconds=settings.vision_frame_interval_seconds,
            max_frames=settings.vision_max_frames,
        )
        prompt = self._PROMPT.format(interval=settings.vision_frame_interval_seconds)
        content: list = [{"type": "text", "text": prompt}]
        if transcript.text:
            content.append({"type": "text", "text": f"Transcript:\n{transcript.text[:8000]}"})
        for frame in frames:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": _data_url(frame)},
                }
            )
        payload = _extract_json(
            self._client.chat(
                model=settings.nvidia_vision_model,
                max_tokens=4096,
                messages=[{"role": "user", "content": content}],
            )
        )
        scenes = [
            SceneBoundary(time=float(item["time"]))
            for item in payload.get("scenes", [])
            if isinstance(item, dict) and "time" in item
        ]
        if not scenes:
            # A model that returns no cuts should not leave the pipeline without
            # visual structure; ffmpeg's own detector is the backstop.
            scenes = [
                SceneBoundary(time=time)
                for time in ffmpeg.detect_scenes(
                    video_path, threshold=settings.vision_scene_threshold
                )
            ]
        return VisualAnalysis(
            scenes=scenes,
            summary=str(payload.get("summary") or ""),
            highlights=payload.get("highlights", []) or [],
            provider=self.name,
        )


def _data_url(path: Path) -> str:
    """Inline an image as a data URL, the form the NIM vision models accept."""
    import base64

    mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    encoded = base64.b64encode(path.read_bytes()).decode()
    return f"data:{mime};base64,{encoded}"


class LLMMomentProvider:
    """Reasoning stage: asks a chat model to nominate candidate moments."""

    name = "llm-moments"

    _SYSTEM = (
        "You select moments from transcripts that would make people stop scrolling. "
        "Favour unexpected revelations, controversial opinions, emotional reactions, "
        "humour and strong storytelling. Every moment must stand alone without the "
        'rest of the video. Return strict JSON: {"moments": [{"start": float, '
        '"end": float, "title": str, "hook": str, "moment_type": str, '
        '"rationale": str, "excerpt": str}]}.'
    )

    def __init__(self, *, transport=None) -> None:
        settings = get_settings()
        if not settings.llm_base_url or not settings.llm_model:
            raise ProviderUnavailable("QUICKREEL_LLM_BASE_URL / QUICKREEL_LLM_MODEL are not set.")
        # ``transport`` is a test seam: production leaves it None and httpx picks
        # its default transport, while tests inject a MockTransport.
        self._transport = transport

    def detect(
        self,
        transcript: TranscriptResult,
        visual: VisualAnalysis,
        audio_events: list[AudioEvent],
    ) -> MomentDetectionResult:
        # Instance settings, not get_settings(): a subclass may be configured
        # with a different model or chunking and must not read the global cache.
        settings = getattr(self, "_settings", None) or get_settings()

        events = ", ".join(
            f"{event.kind}@{event.start:.1f}-{event.end:.1f}" for event in audio_events
        )
        moments: list[CandidateMoment] = []
        seen: set[tuple[float, float]] = set()
        windows = _transcript_windows(
            transcript,
            max_chars=settings.llm_chunk_chars,
            overlap_seconds=settings.llm_chunk_overlap_seconds,
        )
        logger.info("moment.llm_windows", windows=len(windows))
        with self._client_context(settings) as client:
            for window_text in windows:
                payload = self._detect_window(
                    client,
                    window_text,
                    visual.summary,
                    events,
                    settings,
                )
                for item in payload.get("moments", []):
                    if "start" not in item or "end" not in item:
                        continue
                    key = (round(float(item["start"]), 2), round(float(item["end"]), 2))
                    if key in seen:
                        continue
                    seen.add(key)
                    moments.append(
                        CandidateMoment(
                            start=float(item["start"]),
                            end=float(item["end"]),
                            title=item.get("title", ""),
                            hook=item.get("hook", ""),
                            moment_type=item.get("moment_type", "insight"),
                            rationale=item.get("rationale", ""),
                            excerpt=item.get("excerpt", ""),
                        )
                    )
        return MomentDetectionResult(moments=moments, provider=self.name)

    @contextlib.contextmanager
    def _client_context(self, settings):
        """Yield the HTTP client used for each window.

        Overridden by subclasses that bring their own transport, so ``detect``
        stays shared and only the per-window call differs.
        """
        import httpx

        with httpx.Client(
            timeout=settings.llm_timeout_seconds, transport=self._transport
        ) as client:
            yield client

    def _detect_window(
        self,
        client,
        window_text: str,
        visual_summary: str,
        events: str,
        settings,
    ) -> dict:
        """One chat call for one transcript window, with bounded retries."""
        import httpx

        user = (
            f"Visual summary: {visual_summary}\n"
            f"Audio events: {events}\n\n"
            f"Transcript:\n{window_text}"
        )
        last_error: Exception | None = None
        for attempt in range(1, settings.llm_max_retries + 1):
            try:
                response = client.post(
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
                )
                response.raise_for_status()
                content = response.json()["choices"][0]["message"]["content"]
                return _extract_json(content)
            except (httpx.HTTPError, KeyError, IndexError) as exc:
                last_error = exc
                logger.warning(
                    "moment.llm_attempt_failed",
                    attempt=attempt,
                    error=str(exc),
                )
        raise ProviderUnavailable(f"moment detection failed after retries: {last_error}")


class NvidiaMomentProvider(LLMMomentProvider):
    """Moment selection on the NVIDIA endpoint.

    The prompt, windowing and de-duplication are identical to the generic
    OpenAI-compatible provider; only the transport differs, so this reuses that
    logic rather than duplicating it.
    """

    name = "nvidia-moments"

    def __init__(self, *, client: _NvidiaClient | None = None) -> None:
        settings = get_settings()
        self._settings = settings
        self._client = client or _NvidiaClient(
            api_key=settings.nvidia_api_key,
            base_url=settings.nvidia_base_url,
            timeout=settings.llm_timeout_seconds,
        )

    @contextlib.contextmanager
    def _client_context(self, settings):
        # The NIM client is per-instance, so there is no per-call client to open.
        yield None

    def _detect_window(
        self,
        client,
        window_text: str,
        visual_summary: str,
        events: str,
        settings,
    ) -> dict:
        user = (
            f"Visual summary: {visual_summary}\n"
            f"Audio events: {events}\n\n"
            f"Transcript:\n{window_text}"
        )
        content = self._client.chat(
            model=self._settings.nvidia_moment_model,
            max_tokens=8192,
            messages=[
                {"role": "system", "content": self._SYSTEM},
                {"role": "user", "content": user},
            ],
        )
        return _extract_json(content)


class LibrosaAudioAnalyzer:
    """Detects laughter-like bursts, emphasis and silence from the audio signal."""

    name = "librosa"

    def __init__(self, hop_seconds: float = 0.5) -> None:
        # Probe here, not in analyze(): the registry chooses between this class
        # and the fake by catching ProviderUnavailable from the constructor, so a
        # check inside analyze() made the fallback unreachable and failed the
        # whole project on an image built without the ai extra.
        if importlib.util.find_spec("librosa") is None:  # pragma: no cover - optional extra
            raise ProviderUnavailable(
                "librosa is not installed. Install with `pip install -e '.[ai]'`."
            )
        self._hop = hop_seconds

    def analyze(self, audio_path: Path) -> list[AudioEvent]:
        import librosa  # type: ignore import-not-found
        import numpy as np  # type: ignore import-not-found

        samples, sample_rate = librosa.load(str(audio_path), sr=16000, mono=True)
        rms = librosa.feature.rms(y=samples, hop_length=int(sample_rate * self._hop))[0]
        if rms.size == 0:
            return []
        threshold = float(np.percentile(rms, 85))
        silence_threshold = float(np.percentile(rms, 10))

        events: list[AudioEvent] = []
        in_burst = False
        burst_start = 0.0
        burst_peak = 0.0
        for index, value in enumerate(rms):
            time = index * self._hop
            if value >= threshold and not in_burst:
                in_burst = True
                burst_start = time
                burst_peak = float(value)
            elif value >= threshold:
                # Track the loudest frame *inside* the burst. Reading the value at
                # the closing frame measured the post-burst decay instead.
                burst_peak = max(burst_peak, float(value))
            elif in_burst:
                in_burst = False
                if time - burst_start >= 0.6:
                    events.append(
                        AudioEvent(
                            kind="emphasis",
                            start=burst_start,
                            end=time,
                            intensity=min(1.0, burst_peak / (threshold or 1.0)),
                        )
                    )
        # Collapse contiguous quiet frames into a single silence event.
        silence_start: float | None = None
        for index, value in enumerate(rms):
            time = index * self._hop
            if value <= silence_threshold:
                if silence_start is None:
                    silence_start = time
            elif silence_start is not None:
                events.append(
                    AudioEvent(
                        kind="silence",
                        start=silence_start,
                        end=time,
                        intensity=0.0,
                    )
                )
                silence_start = None
        if silence_start is not None:
            events.append(
                AudioEvent(
                    kind="silence",
                    start=silence_start,
                    end=len(rms) * self._hop,
                    intensity=0.0,
                )
            )
        events.sort(key=lambda event: event.start)
        return events


def _transcript_windows(
    transcript: TranscriptResult,
    *,
    max_chars: int,
    overlap_seconds: float,
) -> list[str]:
    """Split a transcript into overlapping text windows for chunked LLM calls.

    A 2-hour podcast is far too long for a single request, so segments are
    grouped into character-bounded windows that overlap in time. A moment
    straddling a boundary is therefore seen whole by at least one window.
    """
    segments = transcript.segments
    if not segments:
        return []

    lines = [f"[{s.start:.1f}-{s.end:.1f}] {s.text}" for s in segments]
    windows: list[str] = []
    start_index = 0
    total = len(segments)
    while start_index < total:
        size = 0
        end_index = start_index
        while end_index < total and (
            size + len(lines[end_index]) <= max_chars or end_index == start_index
        ):
            size += len(lines[end_index]) + 1
            end_index += 1
        windows.append("\n".join(lines[start_index:end_index]))
        if end_index >= total:
            break
        # Step back so the next window overlaps the tail of this one.
        overlap_end_time = segments[end_index - 1].end
        next_start = end_index
        while (
            next_start > start_index + 1
            and segments[next_start - 1].start >= overlap_end_time - overlap_seconds
        ):
            next_start -= 1
        start_index = next_start
    return windows


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
