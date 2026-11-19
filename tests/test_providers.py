"""Tests for provider selection, fallback, and the scoring/moment contracts."""

from __future__ import annotations

import subprocess

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
from quickreel.config import get_settings
from quickreel.media import ffmpeg


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


def test_fake_moment_provider_tolerates_an_empty_transcript():
    """Silent video is a normal input, not a crash.

    A clip of music, applause, or a failed transcription yields no segments.
    Indexing segments[0] for the fallback moment raised IndexError, which failed
    the whole project at the analyze stage.
    """
    empty = TranscriptResult(language="en", text="", provider="unit", segments=[])
    result = fakes.FakeMomentProvider().detect(empty, VisualAnalysis(), [])
    assert result.moments == []


def test_audio_analyzer_needs_no_optional_dependency(monkeypatch):
    """The audio analyzer must work on an image with no ``ai`` extra.

    It previously required librosa, which drags in numpy, scipy, llvmlite and
    numba - about 1GB - to compute one percentile. On a hosted image without
    that extra every project silently scored clips against the fake analyzer's
    invented laughter events. ffmpeg is already a hard dependency, so the real
    analyzer is always available.
    """
    import importlib.util

    real_find_spec = importlib.util.find_spec

    def no_heavy_deps(name, *args, **kwargs):
        if name in {"librosa", "numpy"}:
            return None
        return real_find_spec(name, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "find_spec", no_heavy_deps)
    monkeypatch.setenv("QUICKREEL_TRANSCRIPTION_PROVIDER", "whisper")
    get_settings.cache_clear()
    try:
        assert get_audio_analyzer().name == "ffmpeg-audio"
    finally:
        get_settings.cache_clear()


def _tone(path, seconds, freq=440, volume=0.5):
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}",
            "-af", f"volume={volume}", str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


def test_audio_envelope_measures_loudness_without_numpy(tmp_path):
    """The envelope must track real level changes, not return constants."""
    loud = _tone(tmp_path / "loud.wav", 3, volume=0.9)
    quiet = _tone(tmp_path / "quiet.wav", 3, volume=0.02)

    loud_frames = ffmpeg.audio_envelope(loud, window_seconds=0.5)
    quiet_frames = ffmpeg.audio_envelope(quiet, window_seconds=0.5)

    assert len(loud_frames) >= 5
    # dBFS is negative, so a louder file has a larger (less negative) mean.
    loud_mean = sum(frame.rms for frame in loud_frames) / len(loud_frames)
    quiet_mean = sum(frame.rms for frame in quiet_frames) / len(quiet_frames)
    assert loud_mean > quiet_mean + 10


def test_audio_analyzer_marks_silence_and_emphasis(tmp_path):
    """Loud passages become emphasis events and quiet ones become silence.

    This is the regression guard for the whole stage: the fake analyzer returned
    three hardcoded events for every video, so a broken real analyzer looked
    identical to a working one.
    """
    from quickreel.ai.providers import SignalAudioAnalyzer

    path = tmp_path / "mixed.wav"
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=8",
            "-af", "volume=0.9,volume=0.02:enable='between(t,3,5)'",
            str(path),
        ],
        check=True,
        capture_output=True,
    )

    events = SignalAudioAnalyzer().analyze(path)
    kinds = {event.kind for event in events}
    assert "emphasis" in kinds
    assert "silence" in kinds

    silence = [event for event in events if event.kind == "silence"]
    # The muted window is 3s-5s; the detected silence must overlap it.
    assert any(event.start < 5 and event.end > 3 for event in silence)


def test_audio_analyzer_returns_events_inside_the_media(tmp_path):
    """Events must never point past the end of the file."""
    from quickreel.ai.providers import SignalAudioAnalyzer

    path = _tone(tmp_path / "short.wav", 4)
    events = SignalAudioAnalyzer().analyze(path)
    for event in events:
        assert 0 <= event.start <= event.end <= 4.5


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


def test_construction_failure_raises_when_fallback_is_disabled(monkeypatch):
    """A provider that cannot be built must not degrade to the fake in production.

    Regression: the construction-time path ignored ``allow_provider_fallback``,
    so a deploy whose image lacked the optional dependency silently transcribed
    every video with the fake provider and built clips from invented text.
    """

    class Unbuildable:
        def __init__(self) -> None:
            raise RuntimeError("optional dependency missing")

    # Read the live cached settings rather than the session fixture: other tests
    # clear the cache, which leaves that fixture pointing at a stale instance.
    settings = get_settings()
    monkeypatch.setattr(settings, "allow_provider_fallback", False)
    monkeypatch.setattr(settings, "transcription_provider", "auto")
    monkeypatch.setattr(settings, "nvidia_api_key", None)
    monkeypatch.setattr(settings, "gemini_api_key", None)
    monkeypatch.setattr(
        "quickreel.ai.providers.FasterWhisperTranscriptionProvider", Unbuildable
    )

    with pytest.raises(RuntimeError, match="optional dependency missing"):
        get_transcription_provider()


def test_audio_analyzer_does_not_invent_laughter(tmp_path):
    """Plain speech must not be labelled as laughter.

    An earlier version gated laughter on astats' crest factor, which is reported
    on a scale spanning six orders of magnitude, so the test passed for every
    frame: 13% of ordinary speech came back as "laughter". That silently moved
    clip boundaries and inflated the emotional-intensity score, so this pins the
    honest behaviour - loudness-only analysis - until a real classifier exists.
    """
    from quickreel.ai.providers import SignalAudioAnalyzer

    path = _tone(tmp_path / "speechlike.wav", 6, freq=200, volume=0.5)
    events = SignalAudioAnalyzer().analyze(path)
    assert not [event for event in events if event.kind == "laughter"]


def test_audio_envelope_has_no_infinities(tmp_path):
    """Digital silence must floor to a finite dB value.

    astats reports -inf for a silent window. Callers sort and average these
    levels to pick thresholds, and one infinity silently skews every percentile.
    """
    path = tmp_path / "silent.wav"
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono", "-t", "2", str(path),
        ],
        check=True,
        capture_output=True,
    )
    frames = ffmpeg.audio_envelope(path)
    assert frames
    for frame in frames:
        assert frame.rms == frame.rms  # NaN check
        assert frame.rms != float("-inf")
        assert frame.peak != float("-inf")


def test_segments_are_cut_from_word_timings():
    """Word timings must become sentence-sized cues, not one blob.

    Pulse returns a flat word array. Clipping needs cues bounded by sentence and
    pause, so a 2-hour podcast does not arrive as a single segment whose
    start/end spans the whole file.
    """
    from quickreel.ai.providers import _segments_from_words

    words = [
        {"word": "Hello", "start": 0.0, "end": 0.4},
        {"word": "there.", "start": 0.4, "end": 0.9},
        {"word": "Second", "start": 1.0, "end": 1.5},
        {"word": "sentence.", "start": 1.5, "end": 2.2},
        # A long pause must also break the cue, even without punctuation.
        {"word": "After", "start": 4.0, "end": 4.4},
        {"word": "pause", "start": 4.4, "end": 4.8},
    ]
    segments = _segments_from_words(words)

    assert [segment.text for segment in segments] == [
        "Hello there.",
        "Second sentence.",
        "After pause",
    ]
    assert segments[0].start == 0.0
    assert segments[0].end == 0.9
    assert segments[2].start == 4.0
    assert segments[0].words[0]["word"] == "Hello"


def test_smallest_provider_parses_a_real_response_shape(monkeypatch, tmp_path):
    """Pin the documented Pulse response so a shape change fails here.

    This mirrors the live response body: a flat ``words`` array with
    ``word``/``start``/``end``, plus ``transcription`` and ``language``.
    """
    from quickreel.ai.providers import SmallestTranscriptionProvider

    class FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {
                "status": "success",
                "transcription": "The biggest mistake I made was trusting everyone.",
                "language": "en",
                "words": [
                    {"word": "The", "start": 0.16, "end": 0.24, "confidence": 0.98},
                    {"word": "biggest", "start": 0.32, "end": 0.64, "confidence": 0.99},
                    {"word": "mistake", "start": 0.72, "end": 1.12, "confidence": 0.98},
                    {"word": "everyone.", "start": 2.48, "end": 3.2, "confidence": 0.99},
                ],
                "metadata": {"duration": 3.4},
            }

    class FakeSession:
        def post(self, url, **kwargs):
            assert url.endswith("/waves/v1/stt/")
            assert kwargs["params"]["word_timestamps"] == "true"
            assert kwargs["headers"]["Authorization"].startswith("Bearer ")
            return FakeResponse()

    monkeypatch.setenv("QUICKREEL_SMALLEST_API_KEY", "test-key")
    get_settings.cache_clear()
    try:
        provider = SmallestTranscriptionProvider(session=FakeSession())
    finally:
        get_settings.cache_clear()
    audio = tmp_path / "sample.wav"
    audio.write_bytes(b"RIFF....WAVEfmt ")
    result = provider.transcribe(audio)

    assert result.provider == "smallest-pulse"
    assert result.language == "en"
    # The sample omits "I" and "was", so the cue breaks at the 1.36s gap before
    # "everyone." - exactly the pause-splitting the segmenter is meant to do.
    assert len(result.segments) == 2
    assert result.segments[0].start == 0.16
    assert result.segments[0].end == 1.12
    assert result.segments[1].end == 3.2
    assert sum(len(segment.words) for segment in result.segments) == 4
