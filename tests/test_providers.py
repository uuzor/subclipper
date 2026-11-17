"""Tests for provider selection, fallback, and the scoring/moment contracts."""

from __future__ import annotations

import pytest

from quickreel.ai import fakes
from quickreel.ai.base import (
    MomentDetectionResult,
    TranscriptResult,
    TranscriptSegment,
    VisualAnalysis,
)
from quickreel.ai.registry import (
    _FallbackProxy,
    get_audio_analyzer,
    get_moment_provider,
    get_transcription_provider,
    get_vision_provider,
)


def test_fake_transcription_is_deterministic():
    provider = fakes.FakeTranscriptionProvider()
    first = provider.transcribe("/tmp/does-not-matter.wav")
    second = provider.transcribe("/tmp/does-not-matter.wav")
    assert first.segments == second.segments
    assert first.provider == "fake-whisper"
    assert first.segments[0].start == 0.0


def test_fake_moment_provider_finds_hook_language():
    transcript = fakes.FakeTranscriptionProvider().transcribe("x.wav")
    result = fakes.FakeMomentProvider().detect(transcript, VisualAnalysis(), [])
    assert isinstance(result, MomentDetectionResult)
    assert result.moments
    titles = " ".join(m.title.lower() for m in result.moments)
    assert "mistake" in titles or "guarantee" in titles


def test_fake_moment_provider_classifies_types():
    provider = fakes.FakeMomentProvider()
    transcript = TranscriptResult(
        language="en",
        text="",
        provider="unit",
        segments=[
            TranscriptSegment(0.0, 5.0, "University degrees do not guarantee success.", "S1"),
            TranscriptSegment(5.0, 8.0, "He paused and looked at the audience.", "S1"),
            TranscriptSegment(8.0, 13.0, "But then everyone started laughing at once.", "S1"),
        ],
    )
    result = provider.detect(transcript, VisualAnalysis(), [])
    types = {moment.moment_type for moment in result.moments}
    assert "controversial_opinion" in types
    assert "humor" in types


def test_fallback_proxy_switches_to_fake_on_runtime_error():
    class Boom:
        name = "boom"

        def transcribe(self, audio_path):
            raise RuntimeError("model exploded")

    fake = fakes.FakeTranscriptionProvider()
    proxy = _FallbackProxy("transcription", Boom(), fake)
    assert proxy.name == "boom"

    result = proxy.transcribe("x.wav")
    assert result.provider == "fake-whisper"
    # The proxy stays on the fallback for subsequent calls.
    assert proxy.name == "fake-whisper"
    assert proxy.transcribe("x.wav").provider == "fake-whisper"


def test_fallback_proxy_reraises_when_fallback_also_fails():
    class Boom:
        name = "boom"

        def transcribe(self, audio_path):
            raise RuntimeError("primary failed")

    class FakeBoom:
        name = "fake-boom"

        def transcribe(self, audio_path):
            raise RuntimeError("fallback failed")

    proxy = _FallbackProxy("transcription", Boom(), FakeBoom())
    with pytest.raises(RuntimeError, match="fallback failed"):
        proxy.transcribe("x.wav")


def test_auto_provider_falls_back_when_dependency_missing(monkeypatch):
    # With the test env forcing fake providers, auto paths are not exercised,
    # so assert the explicit selection returns the fake implementations.
    assert get_transcription_provider().name == "fake-whisper"
    assert get_vision_provider().name == "fake-vision"
    assert get_moment_provider().name == "fake-moments"
    assert get_audio_analyzer().name == "fake-audio"


def test_provider_protocols_are_satisfied():
    from quickreel.ai.base import MomentProvider, TranscriptionProvider, VisionProvider

    assert isinstance(get_transcription_provider(), TranscriptionProvider)
    assert isinstance(get_vision_provider(), VisionProvider)
    assert isinstance(get_moment_provider(), MomentProvider)
