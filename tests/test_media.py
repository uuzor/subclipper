"""Tests for the ffmpeg media layer against a real generated video."""

from __future__ import annotations

import pytest

from quickreel.media import ffmpeg


def test_probe_reports_dimensions_and_audio(sample_video):
    info = ffmpeg.probe(sample_video)
    assert info.width == 640
    assert info.height == 360
    assert info.duration_seconds == pytest.approx(20.0, abs=1.0)
    assert info.has_audio is True
    assert info.fps == pytest.approx(25.0, abs=1.0)
    assert info.aspect_ratio == pytest.approx(640 / 360)


def test_extract_audio_produces_mono_wav(sample_video, tmp_path):
    target = tmp_path / "audio.wav"
    ffmpeg.extract_audio(sample_video, target)
    assert target.exists()
    assert target.stat().st_size > 0

    info = ffmpeg.probe(target)
    assert info.has_audio is True
    assert info.width == 0  # audio-only


def test_detect_silences_returns_pairs(sample_video):
    silences = ffmpeg.detect_silences(sample_video)
    assert isinstance(silences, list)
    for start, end in silences:
        assert end >= start


def test_render_vertical_clip_outputs_9x16(sample_video, tmp_path):
    target = tmp_path / "clip.mp4"
    ffmpeg.render_vertical_clip(sample_video, target, start=2.0, end=7.0)

    assert target.exists()
    info = ffmpeg.probe(target)
    assert info.width == 1080
    assert info.height == 1920
    assert info.duration_seconds == pytest.approx(5.0, abs=0.6)
    assert info.has_audio is True


def test_render_vertical_clip_burns_captions(sample_video, tmp_path):
    subtitle = tmp_path / "clip.srt"
    subtitle.write_text(
        "1\n00:00:00,000 --> 00:00:02,000\nHello world\n\n"
        "2\n00:00:02,000 --> 00:00:04,000\nSecond line\n\n",
        encoding="utf-8",
    )
    target = tmp_path / "captioned.mp4"
    ffmpeg.render_vertical_clip(
        sample_video,
        target,
        start=1.0,
        end=5.0,
        burn_captions=True,
        subtitle_path=subtitle,
    )
    assert target.exists()
    assert ffmpeg.probe(target).height == 1920


def test_extract_frames_samples_and_caps(sample_video, tmp_path):
    frames = ffmpeg.extract_frames(
        sample_video,
        tmp_path / "frames",
        interval_seconds=5.0,
        max_frames=3,
    )
    # 20s clip at one frame per 5s would be 4; the cap must win.
    assert len(frames) == 3
    assert all(frame.exists() for frame in frames)
    assert frames == sorted(frames)


def test_detect_scenes_returns_sorted_times(sample_video):
    scenes = ffmpeg.detect_scenes(sample_video, threshold=0.1)
    assert scenes == sorted(scenes)
    assert all(isinstance(time, float) for time in scenes)
