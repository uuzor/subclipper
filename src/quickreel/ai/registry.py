"""Provider registry.

Resolves a configured provider name to a concrete implementation. ``auto`` picks
the best available option and transparently falls back to the deterministic fake
implementations so the pipeline always runs, even with no credentials.
"""

from __future__ import annotations

from ..config import get_settings
from ..logging_setup import get_logger
from . import fakes
from .base import MomentProvider, TranscriptionProvider, VisionProvider

logger = get_logger(__name__)


class _FallbackProxy:
    """Calls the real provider, falling back to a fake if the call itself fails.

    Construction-time failures are handled by ``_auto_with_runtime_fallback``. This covers runtime
    failures (a missing model, a transient API error) so one flaky stage does not
    fail the whole pipeline in ``auto`` mode. The fallback is logged, never silent.

    Set ``QUICKREEL_ALLOW_PROVIDER_FALLBACK=false`` to re-raise instead, so a
    production run fails rather than emitting plausible-looking fake output.
    """

    def __init__(self, kind: str, primary, fallback) -> None:
        self._kind = kind
        self._primary = primary
        self._fallback = fallback
        self.name = getattr(primary, "name", kind)
        self._active = primary

    def _call(self, method: str, *args, **kwargs):
        try:
            result = getattr(self._active, method)(*args, **kwargs)
            return result
        except Exception as exc:  # noqa: BLE001 - deliberate graceful degradation
            if self._active is self._fallback or not get_settings().allow_provider_fallback:
                raise
            logger.warning(
                "provider.runtime_fallback",
                kind=self._kind,
                method=method,
                reason=str(exc),
            )
            self._active = self._fallback
            self.name = getattr(self._fallback, "name", self._kind)
            return getattr(self._fallback, method)(*args, **kwargs)

    def transcribe(self, *args, **kwargs):
        return self._call("transcribe", *args, **kwargs)

    def analyze(self, *args, **kwargs):
        return self._call("analyze", *args, **kwargs)

    def detect(self, *args, **kwargs):
        return self._call("detect", *args, **kwargs)


def _auto_with_runtime_fallback(kind: str, real_factory, fake_factory):
    try:
        real = real_factory()
    except Exception as exc:  # noqa: BLE001
        # A provider that cannot even be constructed is a configuration error,
        # not a transient fault: nothing will recover by retrying the call. With
        # fallback disabled it must raise here, otherwise a deploy missing an
        # optional dependency silently transcribes every video with the fake
        # provider and produces clips from invented text.
        if not get_settings().allow_provider_fallback:
            raise
        logger.warning("provider.fallback_to_fake", kind=kind, reason=str(exc))
        return fake_factory()
    return _FallbackProxy(kind, real, fake_factory())


def get_transcription_provider() -> TranscriptionProvider:
    settings = get_settings()
    if settings.transcription_provider == "fake":
        return fakes.FakeTranscriptionProvider()
    from .providers import FasterWhisperTranscriptionProvider, NvidiaTranscriptionProvider

    if settings.transcription_provider == "faster-whisper":
        return FasterWhisperTranscriptionProvider()
    if settings.transcription_provider == "nvidia":
        return NvidiaTranscriptionProvider()
    if settings.transcription_provider == "smallest":
        from .providers import SmallestTranscriptionProvider

        return SmallestTranscriptionProvider()
    if settings.transcription_provider == "gemini":
        from .providers import GeminiTranscriptionProvider

        return GeminiTranscriptionProvider()
    # ``auto`` prefers a purpose-built ASR model when a key is present. Pulse
    # returns real word timestamps; the multimodal models only estimate them.
    if settings.smallest_api_key:
        from .providers import SmallestTranscriptionProvider

        return _auto_with_runtime_fallback(
            "transcription", SmallestTranscriptionProvider, fakes.FakeTranscriptionProvider
        )
    # ``auto`` prefers hosted speech when a key is present: it needs no model
    # weights, which is the difference between working and not working on a
    # small container.
    if settings.nvidia_api_key:
        return _auto_with_runtime_fallback(
            "transcription", NvidiaTranscriptionProvider, fakes.FakeTranscriptionProvider
        )
    # Gemini accepts audio and its key is already configured for vision, so it
    # is the cheapest hosted speech option. Prefer it over local Whisper, which
    # needs model weights the small hosting image does not carry.
    if settings.gemini_api_key:
        from .providers import GeminiTranscriptionProvider

        return _auto_with_runtime_fallback(
            "transcription", GeminiTranscriptionProvider, fakes.FakeTranscriptionProvider
        )
    return _auto_with_runtime_fallback(
        "transcription", FasterWhisperTranscriptionProvider, fakes.FakeTranscriptionProvider
    )


def get_vision_provider() -> VisionProvider:
    settings = get_settings()
    if settings.vision_provider == "fake":
        return fakes.FakeVisionProvider()
    from .providers import GeminiVisionProvider, NvidiaVisionProvider

    if settings.vision_provider == "gemini":
        return GeminiVisionProvider()
    if settings.vision_provider == "nvidia":
        return NvidiaVisionProvider()
    if settings.nvidia_api_key:
        return _auto_with_runtime_fallback(
            "vision", NvidiaVisionProvider, fakes.FakeVisionProvider
        )
    return _auto_with_runtime_fallback("vision", GeminiVisionProvider, fakes.FakeVisionProvider)


def get_moment_provider() -> MomentProvider:
    settings = get_settings()
    if settings.moment_provider == "fake":
        return fakes.FakeMomentProvider()
    from .providers import LLMMomentProvider, NvidiaMomentProvider

    if settings.moment_provider == "llm":
        return LLMMomentProvider()
    if settings.moment_provider == "nvidia":
        return NvidiaMomentProvider()
    if settings.nvidia_api_key:
        return _auto_with_runtime_fallback(
            "moment", NvidiaMomentProvider, fakes.FakeMomentProvider
        )
    return _auto_with_runtime_fallback("moment", LLMMomentProvider, fakes.FakeMomentProvider)


def get_audio_analyzer():
    """Audio event detection, computed from ffmpeg's envelope.

    This needs no optional dependency: ffmpeg is a hard requirement of the image,
    so there is no fallback path to a fake. It previously depended on librosa,
    which meant every hosted deployment silently scored clips against invented
    laughter events.
    """
    settings = get_settings()
    if settings.transcription_provider == "fake":
        return fakes.FakeAudioAnalyzer()
    from .providers import SignalAudioAnalyzer

    return SignalAudioAnalyzer()
