"""Tests for the ffmpeg media layer against a real generated video."""

from __future__ import annotations

import subprocess

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


def test_render_vertical_clip_caps_encoder_threads(sample_video, tmp_path, monkeypatch):
    """A container on a many-core host reports that core count, and x264 sizes
    its frame pools from it. Uncapped, a 512MB instance is OOM-killed mid-render,
    which takes the whole service down rather than failing one clip."""
    from quickreel.config import get_settings

    seen: list[list[str]] = []
    real_run = ffmpeg._run

    def spy(cmd, timeout=None):
        seen.append(list(cmd))
        return real_run(cmd, timeout=timeout)

    monkeypatch.setattr(ffmpeg, "_run", spy)
    get_settings.cache_clear()
    try:
        ffmpeg.render_vertical_clip(
            sample_video, tmp_path / "threaded.mp4", start=2.0, end=4.0
        )
    finally:
        get_settings.cache_clear()

    cmd = seen[-1]
    assert cmd[cmd.index("-threads") + 1] == "2"
    assert cmd[cmd.index("-filter_threads") + 1] == "2"


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


def test_extract_frames_clears_stale_frames_from_previous_run(sample_video, tmp_path):
    frames_dir = tmp_path / "frames"
    # A previous run with a larger cap left extra frames behind.
    frames_dir.mkdir()
    for index in range(1, 6):
        (frames_dir / f"frame_{index:04d}.jpg").write_bytes(b"stale")

    frames = ffmpeg.extract_frames(sample_video, frames_dir, interval_seconds=5.0, max_frames=2)
    assert len(frames) == 2, "stale frames from a previous run must not be returned"
    assert all(frame.read_bytes() != b"stale" for frame in frames)


@pytest.mark.parametrize(
    ("platform_key", "width", "height"),
    [
        ("tiktok", 1080, 1920),
        ("youtube_shorts", 1080, 1920),
        ("instagram_reels", 1080, 1920),
        ("youtube", 1920, 1080),
        ("square", 1080, 1080),
        ("portrait", 1080, 1350),
    ],
)
def test_platform_specs_render_the_documented_canvas(
    sample_video, tmp_path, platform_key, width, height
):
    """Each export target must actually produce its advertised frame size.

    The spec table is only worth having if ffmpeg honours it, so this renders a
    real file per platform rather than asserting the dataclass against itself.
    """
    from quickreel.rendering import platforms

    spec = platforms.get_spec(platform_key)
    assert (spec.width, spec.height) == (width, height)

    target = tmp_path / f"{platform_key}.mp4"
    ffmpeg.render_clip(
        sample_video,
        target,
        start=1.0,
        end=3.0,
        width=spec.width,
        height=spec.height,
        video_bitrate=spec.video_bitrate,
        audio_bitrate=spec.audio_bitrate,
    )

    info = ffmpeg.probe(target)
    assert (info.width, info.height) == (width, height)
    assert info.has_audio is True


def test_caption_safe_area_is_applied_only_when_requested(sample_video, tmp_path):
    """Platform chrome hides bottom-anchored captions, so a safe margin must
    change the filter graph; without one the historical filter must be intact."""
    subtitle = tmp_path / "clip.srt"
    subtitle.write_text(
        "1\n00:00:00,000 --> 00:00:02,000\nHello world\n\n", encoding="utf-8"
    )

    seen: list[list[str]] = []
    real_run = ffmpeg._run

    def spy(cmd, timeout=None):
        seen.append(list(cmd))
        return real_run(cmd, timeout=timeout)

    original = ffmpeg._run
    ffmpeg._run = spy
    try:
        ffmpeg.render_clip(
            sample_video,
            tmp_path / "safe.mp4",
            start=1.0,
            end=3.0,
            burn_captions=True,
            subtitle_path=subtitle,
            bottom_safe_pixels=384,
        )
        ffmpeg.render_clip(
            sample_video,
            tmp_path / "plain.mp4",
            start=1.0,
            end=3.0,
            burn_captions=True,
            subtitle_path=subtitle,
        )
    finally:
        ffmpeg._run = original

    safe_filters = seen[0][seen[0].index("-vf") + 1]
    plain_filters = seen[1][seen[1].index("-vf") + 1]
    # 384px at a 1920px frame is 384 / (1920 / 288) = 58 libass units.
    assert "MarginV=58" in safe_filters
    assert "MarginV" not in plain_filters


def test_caption_safe_area_moves_text_above_the_platform_chrome():
    """Measure where the caption actually lands, in pixels.

    ``MarginV`` is in libass script units, not output pixels. Passing a pixel
    value straight through multiplies the lift by ~6.7 on a 1920px frame, which
    pushed captions off the top of the frame where they vanished. This renders a
    black frame and reads the rows the text occupies, so the conversion is
    checked against the image rather than against the string we passed in.
    """
    from quickreel.media.ffmpeg import _subtitle_filter

    height = 1920
    safe_pixels = 422
    subtitle = "/tmp/caption_safe_area.srt"
    with open(subtitle, "w", encoding="utf-8") as handle:
        handle.write("1\n00:00:00,000 --> 00:00:05,000\nThe biggest mistake\n\n")

    filters = _subtitle_filter(subtitle, height, safe_pixels)
    frame = "/tmp/caption_safe_area.png"
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", f"color=c=black:s=1080x{height}:d=1",
            "-vf", filters, "-frames:v", "1", frame,
        ],
        check=True,
        timeout=120,
    )

    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", frame, "-f", "rawvideo", "-pix_fmt", "gray", "-"],
        capture_output=True,
        timeout=120,
    ).stdout
    width = 1080
    text_rows = [
        y
        for y in range(height)
        if max(raw[y * width : (y + 1) * width], default=0) > 40
    ]

    # The caption must be visible at all, and entirely above the chrome line.
    assert text_rows, "caption was rendered off-frame"
    assert max(text_rows) < height - safe_pixels


def test_unknown_platform_is_rejected_with_the_known_keys():
    from quickreel.rendering import platforms

    with pytest.raises(platforms.UnknownPlatform) as excinfo:
        platforms.get_spec("myspace")
    message = str(excinfo.value)
    assert "myspace" in message
    # The error must list valid options or the caller cannot recover from it.
    assert "tiktok" in message
