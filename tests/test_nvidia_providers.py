"""Tests for the NVIDIA NIM providers and the YouTube bot-check classifier.

The NIM client is stubbed at the provider boundary so the provider logic
(chunking, timestamp offsetting, frame sampling, JSON extraction) is exercised
without a network call or an API key.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from quickreel.ai.base import TranscriptResult, TranscriptSegment, VisualAnalysis
from quickreel.ai.providers import (
    NvidiaMomentProvider,
    NvidiaTranscriptionProvider,
    NvidiaVisionProvider,
    _data_url,
    _NvidiaClient,
)
from quickreel.config import Settings
from quickreel.media.downloader import _COOKIE_HINT, _is_bot_check


class StubClient:
    """Records prompts and returns queued responses, one per call."""

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def chat(self, *, model: str, messages: list, max_tokens: int = 4096) -> str:
        self.calls.append({"model": model, "messages": messages, "max_tokens": max_tokens})
        if not self.responses:
            raise AssertionError("stub client called more times than responses were queued")
        return self.responses.pop(0)


def _settings(tmp_path: Path, **overrides) -> Settings:
    values = {
        "artifacts_dir": tmp_path,
        "nvidia_api_key": "test-key",
        "nvidia_asr_chunk_seconds": 10.0,
        "nvidia_asr_max_chunks": 4,
        "vision_frame_interval_seconds": 1.0,
        "vision_max_frames": 2,
    }
    values.update(overrides)
    return Settings(**values)


def test_nvidia_client_requires_key():
    from quickreel.ai.providers import ProviderUnavailable

    with pytest.raises(ProviderUnavailable):
        _NvidiaClient(api_key="", base_url="https://x", timeout=1)


def test_nvidia_transcription_offsets_chunk_timestamps(sample_video, tmp_path):
    """A segment in the second chunk must be shifted onto the source timeline.

    This is the bug a naive implementation ships: every chunk reports times
    starting at 0, so every clip after the first lands at the top of the video.
    """
    from quickreel.media import ffmpeg as ffmpeg_module

    audio = tmp_path / "source.wav"
    ffmpeg_module.extract_audio(sample_video, audio)

    chunk_one = json.dumps(
        {"language": "en", "segments": [{"start": 1.0, "end": 3.0, "text": "first part"}]}
    )
    chunk_two = json.dumps(
        {"language": "en", "segments": [{"start": 0.5, "end": 2.0, "text": "second part"}]}
    )
    client = StubClient([chunk_one, chunk_two])
    provider = NvidiaTranscriptionProvider(client=client)
    provider._settings = _settings(tmp_path, nvidia_asr_chunk_seconds=10.0)

    result = provider.transcribe(audio)

    assert result.provider == "nvidia-omni"
    assert len(client.calls) >= 2
    texts = [segment.text for segment in result.segments]
    assert "first part" in texts and "second part" in texts
    second = next(segment for segment in result.segments if segment.text == "second part")
    # Chunk two starts at 10s, so its local 0.5s is 10.5s in the source.
    assert second.start == pytest.approx(10.5)
    assert second.end == pytest.approx(12.0)


def test_nvidia_transcription_rejects_empty_segments(sample_video, tmp_path):
    from quickreel.ai.providers import ProviderUnavailable
    from quickreel.media import ffmpeg as ffmpeg_module

    audio = tmp_path / "source.wav"
    ffmpeg_module.extract_audio(sample_video, audio)
    provider = NvidiaTranscriptionProvider(client=StubClient(['{"segments": []}']))
    provider._settings = _settings(tmp_path, nvidia_asr_chunk_seconds=600.0)

    with pytest.raises(ProviderUnavailable):
        provider.transcribe(audio)


def test_nvidia_transcription_sends_audio_as_base64(sample_video, tmp_path):
    from quickreel.media import ffmpeg as ffmpeg_module

    audio = tmp_path / "source.wav"
    ffmpeg_module.extract_audio(sample_video, audio)
    client = StubClient(
        [json.dumps({"language": "en", "segments": [{"start": 0, "end": 1, "text": "hi"}]})]
    )
    provider = NvidiaTranscriptionProvider(client=client)
    provider._settings = _settings(tmp_path, nvidia_asr_chunk_seconds=600.0)
    provider.transcribe(audio)

    content = client.calls[0]["messages"][0]["content"]
    audio_part = next(part for part in content if part["type"] == "input_audio")
    assert audio_part["input_audio"]["format"] == "wav"
    assert audio_part["input_audio"]["data"]


def test_nvidia_vision_inlines_frames_and_parses_scenes(sample_video, tmp_path):
    payload = json.dumps(
        {
            "summary": "A talking head explains a mistake.",
            "scenes": [{"time": 1.5}, {"time": 4.0}],
            "highlights": [{"time": 2.0, "note": "speaker leans in"}],
        }
    )
    client = StubClient([payload])
    provider = NvidiaVisionProvider(client=client)
    provider._settings = _settings(tmp_path)

    transcript = TranscriptResult(
        language="en",
        text="the biggest mistake was trusting everyone",
        segments=[TranscriptSegment(0.0, 2.0, "the biggest mistake was trusting everyone")],
        provider="test",
    )
    result = provider.analyze(sample_video, transcript)

    assert result.provider == "nvidia-vl"
    assert result.summary.startswith("A talking head")
    assert [scene.time for scene in result.scenes] == [1.5, 4.0]
    assert result.highlights[0]["note"] == "speaker leans in"

    content = client.calls[0]["messages"][0]["content"]
    images = [part for part in content if part["type"] == "image_url"]
    assert images, "frames must be attached as image_url parts"
    assert images[0]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_nvidia_vision_falls_back_to_ffmpeg_scenes_when_model_omits_them(
    sample_video, tmp_path, monkeypatch
):
    """A model that returns only a summary must not leave scenes empty."""
    from quickreel.ai import providers as providers_module

    monkeypatch.setattr(providers_module.ffmpeg, "detect_scenes", lambda *a, **k: [3.0, 7.0])
    provider = NvidiaVisionProvider(client=StubClient([json.dumps({"summary": "just talking"})]))
    provider._settings = _settings(tmp_path)

    result = provider.analyze(
        sample_video,
        TranscriptResult(language="en", text="", segments=[], provider="test"),
    )

    assert [scene.time for scene in result.scenes] == [3.0, 7.0]


def test_nvidia_moment_provider_parses_candidates(tmp_path):
    payload = json.dumps(
        {
            "moments": [
                {
                    "start": 10.0,
                    "end": 25.5,
                    "title": "Unexpected revelation",
                    "hook": "I trusted everyone",
                    "moment_type": "revelation",
                    "rationale": "strong emotional payoff",
                    "excerpt": "the biggest mistake I made",
                }
            ]
        }
    )
    client = StubClient([payload])
    provider = NvidiaMomentProvider(client=client)
    provider._settings = _settings(tmp_path, llm_chunk_chars=12000)

    transcript = TranscriptResult(
        language="en",
        text="the biggest mistake I made was trusting everyone",
        segments=[
            TranscriptSegment(10.0, 25.0, "the biggest mistake I made was trusting everyone")
        ],
        provider="test",
    )
    result = provider.detect(transcript, VisualAnalysis(summary="talking head"), [])

    assert result.provider == "nvidia-moments"
    assert len(result.moments) == 1
    moment = result.moments[0]
    assert (moment.start, moment.end) == (10.0, 25.5)
    assert moment.moment_type == "revelation"
    assert client.calls[0]["model"] == provider._settings.nvidia_moment_model


def test_nvidia_moment_provider_deduplicates_repeated_windows(tmp_path):
    """Overlapping transcript windows must not yield the same clip twice."""
    payload = json.dumps({"moments": [{"start": 5.0, "end": 9.0, "title": "same"}]})
    client = StubClient([payload] * 20)
    provider = NvidiaMomentProvider(client=client)
    # Each line is ~22 chars, so 60 chars forces two lines per window and the
    # transcript below spans several overlapping windows.
    provider._settings = _settings(tmp_path, llm_chunk_chars=60, llm_chunk_overlap_seconds=5.0)

    segments = [
        TranscriptSegment(float(i), float(i) + 1, f"sentence number {i}") for i in range(12)
    ]
    transcript = TranscriptResult(
        language="en",
        text=" ".join(segment.text for segment in segments),
        segments=segments,
        provider="test",
    )
    result = provider.detect(transcript, VisualAnalysis(), [])

    assert len(client.calls) > 1, "the transcript should span several windows"
    assert len(result.moments) == 1


def test_data_url_uses_png_mime_for_png(tmp_path):
    frame = tmp_path / "f.png"
    frame.write_bytes(b"\x89PNG\r\n")
    assert _data_url(frame).startswith("data:image/png;base64,")


@pytest.mark.parametrize(
    "message",
    [
        "Sign in to confirm you're not a bot.",
        "unable to download video data: HTTP Error 403: Forbidden",
        "This helps protect our community. not a bot",
    ],
)
def test_bot_check_is_recognised(message):
    assert _is_bot_check(message)


@pytest.mark.parametrize(
    "message",
    ["Video unavailable", "This video is private", "unsupported URL"],
)
def test_other_failures_are_not_misread_as_bot_checks(message):
    assert not _is_bot_check(message)


def test_bot_check_message_names_the_fix():
    """The error has to tell an operator what to change, not just what broke."""
    assert "QUICKREEL_DOWNLOADER_COOKIES_FILE" in _COOKIE_HINT
    assert "QUICKREEL_DOWNLOADER_PROXY" in _COOKIE_HINT
