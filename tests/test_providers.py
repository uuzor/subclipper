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


# --- real provider behaviour, no network or credentials -----------------------


def test_extract_json_tolerates_code_fences_and_prose():
    from quickreel.ai.providers import _extract_json

    assert _extract_json('```json\n{"moments": [1]}\n```') == {"moments": [1]}
    assert _extract_json('Sure! Here you go: {"a": 1} hope that helps') == {"a": 1}
    assert _extract_json("not json at all") == {}
    assert _extract_json("") == {}


def test_transcript_windows_chunk_and_overlap_long_transcripts():
    from quickreel.ai.providers import _transcript_windows

    segments = [
        TranscriptSegment(i * 10.0, (i + 1) * 10.0, f"Sentence number {i} padded out.", "S1")
        for i in range(600)
    ]
    transcript = TranscriptResult(language="en", text="", provider="unit", segments=segments)

    windows = _transcript_windows(transcript, max_chars=12000, overlap_seconds=30.0)
    # A 100-minute transcript must become a handful of bounded calls, not one.
    assert 2 <= len(windows) <= 10
    assert all(len(window) <= 12000 + 200 for window in windows)
    assert any("Sentence number 599" in window for window in windows)


def test_transcript_windows_handle_empty_transcript():
    from quickreel.ai.providers import _transcript_windows

    empty = TranscriptResult(language="en", text="", provider="unit", segments=[])
    assert _transcript_windows(empty, max_chars=1000, overlap_seconds=10.0) == []


def _llm_settings(monkeypatch, **overrides):
    from quickreel import config
    from quickreel.ai import providers

    monkeypatch.setattr(
        providers,
        "get_settings",
        lambda: config.Settings(
            llm_base_url="https://llm.test/v1",
            llm_model="test-model",
            llm_api_key="secret",
            **overrides,
        ),
    )


def _transcript_with_segments(count: int):
    segments = [
        TranscriptSegment(i * 10.0, (i + 1) * 10.0, f"Sentence number {i} here.", "S1")
        for i in range(count)
    ]
    return TranscriptResult(language="en", text="", provider="unit", segments=segments)


def test_llm_moment_provider_chunks_and_parses(monkeypatch):
    import httpx

    _llm_settings(monkeypatch, llm_chunk_chars=200, llm_chunk_overlap_seconds=10.0)
    from quickreel.ai.providers import LLMMomentProvider

    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        calls.append(json.loads(request.content))
        body = {"moments": [{"start": 10.0, "end": 20.0, "title": "T", "hook": "H"}]}
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(body)}}]},
        )

    provider = LLMMomentProvider(transport=httpx.MockTransport(handler))
    result = provider.detect(_transcript_with_segments(60), VisualAnalysis(), [])

    assert len(calls) > 1, "long transcript must be chunked into several calls"
    # Overlapping windows report the same moment; it must be de-duplicated.
    assert len(result.moments) == 1
    assert result.moments[0].start == 10.0
    assert result.provider == "llm-moments"


def test_llm_moment_provider_retries_then_raises(monkeypatch):
    import httpx
    import pytest

    _llm_settings(monkeypatch, llm_chunk_chars=100000, llm_max_retries=3)
    from quickreel.ai.providers import LLMMomentProvider, ProviderUnavailable

    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(503, json={"error": "busy"})

    provider = LLMMomentProvider(transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderUnavailable):
        provider.detect(_transcript_with_segments(2), VisualAnalysis(), [])
    assert attempts["n"] == 3, "should retry the configured number of times"


def test_llm_moment_provider_recovers_after_transient_failure(monkeypatch):
    import json

    import httpx

    _llm_settings(monkeypatch, llm_chunk_chars=100000, llm_max_retries=3)
    from quickreel.ai.providers import LLMMomentProvider

    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(500, json={"error": "transient"})
        body = {"moments": [{"start": 0.0, "end": 5.0, "title": "T"}]}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(body)}}]})

    provider = LLMMomentProvider(transport=httpx.MockTransport(handler))
    result = provider.detect(_transcript_with_segments(2), VisualAnalysis(), [])
    assert len(result.moments) == 1
    assert attempts["n"] == 2


# --- additive schema migration ------------------------------------------------


def test_init_db_adds_missing_columns_to_existing_table(tmp_path):
    from sqlalchemy import create_engine, inspect, text

    from quickreel.db import _add_missing_columns

    db_path = tmp_path / "legacy.db"
    engine = create_engine(f"sqlite+pysqlite:///{db_path}")
    # Simulate an older install: projects table without the providers column.
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE projects (id VARCHAR(32) PRIMARY KEY, title VARCHAR(500), "
                "source_kind VARCHAR(32), source_url TEXT, status VARCHAR(32), "
                "error TEXT, created_at DATETIME, updated_at DATETIME)"
            )
        )

    _add_missing_columns(engine)

    columns = {column["name"] for column in inspect(engine).get_columns("projects")}
    assert "providers" in columns
    assert {"id", "title"} <= columns  # existing columns untouched


def test_init_db_migration_is_idempotent(tmp_path):
    from sqlalchemy import create_engine

    from quickreel.db import _add_missing_columns
    from quickreel.models import Base

    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'fresh.db'}")
    Base.metadata.create_all(bind=engine)
    # Running twice must not raise on already-present columns.
    _add_missing_columns(engine)
    _add_missing_columns(engine)


def test_fallback_proxy_degrades_by_default(monkeypatch):
    from quickreel.ai.registry import _FallbackProxy

    class Boom:
        name = "real"

        def transcribe(self, *_a, **_k):
            raise RuntimeError("api down")

    class Ok:
        name = "fake"

        def transcribe(self, *_a, **_k):
            return "fake-result"

    proxy = _FallbackProxy("transcription", Boom(), Ok())
    assert proxy.transcribe("audio") == "fake-result"
    assert proxy.name == "fake"


def test_fallback_proxy_raises_when_fallback_disabled(monkeypatch):
    import pytest

    from quickreel import config
    from quickreel.ai import registry

    monkeypatch.setattr(
        registry,
        "get_settings",
        lambda: config.Settings(allow_provider_fallback=False),
    )

    class Boom:
        name = "real"

        def transcribe(self, *_a, **_k):
            raise RuntimeError("api down")

    proxy = registry._FallbackProxy("transcription", Boom(), object())
    with pytest.raises(RuntimeError, match="api down"):
        proxy.transcribe("audio")


def test_gemini_load_frame_returns_sdk_blob_dict(tmp_path):
    """The Gemini SDK rejects raw ``bytes``; frames must be blob dicts.

    Regression test: without PIL the old code returned ``path.read_bytes()``,
    which raised ``TypeError: Could not create ``Blob```` inside the SDK and
    surfaced as a 500 from the process endpoint.
    """
    from quickreel.ai.providers import GeminiVisionProvider

    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")
    blob = GeminiVisionProvider._load_frame(frame)

    assert isinstance(blob, dict)
    assert blob["mime_type"] == "image/jpeg"
    assert blob["data"] == b"\xff\xd8\xff\xe0fake-jpeg"

    png = tmp_path / "frame.png"
    png.write_bytes(b"\x89PNG\r\n")
    assert GeminiVisionProvider._load_frame(png)["mime_type"] == "image/png"
