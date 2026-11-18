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
            if self._active is self._fallback:
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
    fake = fake_factory()
    try:
        real = real_factory()
    except Exception as exc:  # noqa: BLE001
        logger.info("provider.fallback_to_fake", kind=kind, reason=str(exc))
        return fake
    return _FallbackProxy(kind, real, fake)


def get_transcription_provider() -> TranscriptionProvider:
    settings = get_settings()
    if settings.transcription_provider == "fake":
        return fakes.FakeTranscriptionProvider()
    from .providers import FasterWhisperTranscriptionProvider

    if settings.transcription_provider == "faster-whisper":
        return FasterWhisperTranscriptionProvider()
    return _auto_with_runtime_fallback(
        "transcription", FasterWhisperTranscriptionProvider, fakes.FakeTranscriptionProvider
    )


def get_vision_provider() -> VisionProvider:
    settings = get_settings()
    if settings.vision_provider == "fake":
        return fakes.FakeVisionProvider()
    from .providers import GeminiVisionProvider

    if settings.vision_provider == "gemini":
        return GeminiVisionProvider()
    return _auto_with_runtime_fallback("vision", GeminiVisionProvider, fakes.FakeVisionProvider)


def get_moment_provider() -> MomentProvider:
    settings = get_settings()
    if settings.moment_provider == "fake":
        return fakes.FakeMomentProvider()
    from .providers import LLMMomentProvider

    if settings.moment_provider == "llm":
        return LLMMomentProvider()
    return _auto_with_runtime_fallback("moment", LLMMomentProvider, fakes.FakeMomentProvider)


def get_audio_analyzer():
    """Audio event detection: real analyser when librosa is importable."""
    settings = get_settings()
    if settings.transcription_provider == "fake":
        return fakes.FakeAudioAnalyzer()
    from .providers import LibrosaAudioAnalyzer, ProviderUnavailable

    try:
        return LibrosaAudioAnalyzer()
    except ProviderUnavailable:
        logger.info("provider.fallback_to_fake", kind="audio")
        return fakes.FakeAudioAnalyzer()
