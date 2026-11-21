"""Tests for the yt-dlp downloader wrapper.

yt-dlp itself is replaced by a stub module so the wrapper's own logic is tested
without network access: option construction, output resolution, size limiting
and error normalisation. The stub writes a real file, so the code path that
finds the produced media file is genuinely exercised.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from quickreel.media.downloader import (
    DownloadError,
    FakeDownloader,
    YtDlpDownloader,
    _clean_message,
    _resolve_output,
    _to_info,
)


class _StubYDL:
    """Minimal stand-in for yt_dlp.YoutubeDL."""

    last_options: dict = {}
    # When set, extract_info raises this instead of writing a file.
    error: Exception | None = None
    # Extension to write the produced file with.
    written_ext: str = "mp4"
    payload: bytes = b"fake-video-bytes"

    def __init__(self, options: dict) -> None:
        _StubYDL.last_options = options

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url: str, download: bool = True) -> dict:
        if _StubYDL.error is not None:
            raise _StubYDL.error
        return {"title": "Stub video", "ext": _StubYDL.written_ext, "id": "abc"}

    def prepare_filename(self, info: dict) -> str:
        template = _StubYDL.last_options["outtmpl"]
        return template.replace("%(ext)s", _StubYDL.written_ext)


@pytest.fixture()
def stub_ytdlp(monkeypatch):
    _StubYDL.last_options = {}
    _StubYDL.error = None
    _StubYDL.written_ext = "mp4"

    module = types.ModuleType("yt_dlp")

    class _YDL(_StubYDL):
        def extract_info(self, url, download=True):
            info = super().extract_info(url, download)
            # Emulate yt-dlp actually writing the file to the outtmpl path.
            path = Path(_StubYDL.last_options["outtmpl"].replace("%(ext)s", _StubYDL.written_ext))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(_StubYDL.payload)
            return info

    module.YoutubeDL = _YDL
    monkeypatch.setitem(sys.modules, "yt_dlp", module)
    return _StubYDL


# --- option construction ------------------------------------------------------


def test_download_options_do_not_set_max_filesize():
    # Regression: passing max_filesize=0 makes yt-dlp treat it as a zero-byte
    # limit and download nothing at all.
    options = YtDlpDownloader(max_height=720)._base_options()
    assert "max_filesize" not in options


def test_download_options_are_quiet_and_playlist_free():
    options = YtDlpDownloader()._base_options()
    assert options["noplaylist"] is True
    assert options["quiet"] is True


def test_download_format_caps_resolution(tmp_path: Path, stub_ytdlp):
    # The height cap must actually reach yt-dlp's format selector.
    YtDlpDownloader(max_height=480).download("https://example.com/v", tmp_path, "clip")
    assert "height<=480" in stub_ytdlp.last_options["format"]


# --- download -----------------------------------------------------------------


def test_download_writes_file_and_reports_size(tmp_path: Path, stub_ytdlp):
    downloader = YtDlpDownloader()
    result = downloader.download("https://example.com/v", tmp_path, "clip")
    assert result.path.exists()
    assert result.path.name == "clip.mp4"
    assert result.size_bytes == len(_StubYDL.payload)
    assert result.info.title == "Stub video"


def test_download_rejects_file_over_size_limit(tmp_path: Path, stub_ytdlp):
    stub_ytdlp.payload = b"x" * (2 * 1024 * 1024)
    downloader = YtDlpDownloader(max_bytes=1 * 1024 * 1024)
    with pytest.raises(DownloadError, match="over the"):
        downloader.download("https://example.com/v", tmp_path, "clip")
    # The oversized file must be cleaned up, not left on disk.
    assert not list(tmp_path.glob("clip.*"))


def test_download_wraps_ytdlp_errors(tmp_path: Path, stub_ytdlp):
    stub_ytdlp.error = RuntimeError("ERROR: video is private")
    with pytest.raises(DownloadError, match="video is private"):
        YtDlpDownloader().download("https://example.com/v", tmp_path, "clip")


def test_download_resolves_remuxed_extension(tmp_path: Path, stub_ytdlp):
    # yt-dlp may remux to a different container than the one it reports.
    stub_ytdlp.written_ext = "mkv"
    result = YtDlpDownloader().download("https://example.com/v", tmp_path, "clip")
    assert result.path.name == "clip.mkv"


def test_probe_raises_clean_error_when_ytdlp_missing(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "yt_dlp", None)
    monkeypatch.setattr(
        "quickreel.media.downloader.YtDlpDownloader._import_ytdlp",
        staticmethod(_raise_missing),
    )
    with pytest.raises(DownloadError, match="yt-dlp is not installed"):
        YtDlpDownloader().download("https://example.com/v", tmp_path, "clip")


def _raise_missing():
    raise DownloadError("yt-dlp is not installed; run `pip install 'quickreel[download]'`")


# --- helpers ------------------------------------------------------------------


def test_resolve_output_falls_back_to_glob(tmp_path: Path):
    # prepare_filename pointed at a path that does not exist, but a sibling did.
    (tmp_path / "clip.mkv").write_bytes(b"data")
    resolved = _resolve_output(tmp_path, "clip", tmp_path / "clip.mp4")
    assert resolved is not None and resolved.name == "clip.mkv"


def test_resolve_output_ignores_partial_downloads(tmp_path: Path):
    (tmp_path / "clip.mp4.part").write_bytes(b"partial")
    assert _resolve_output(tmp_path, "clip", tmp_path / "clip.mp4") is None


def test_clean_message_strips_error_prefix():
    assert _clean_message(RuntimeError("ERROR:   something   broke ")) == "something broke"


def test_clean_message_has_fallback():
    assert _clean_message(RuntimeError("")) == "download failed"


def test_to_info_maps_fields_and_drops_formats():
    info = _to_info(
        "https://x/y",
        {
            "title": "T",
            "uploader": "U",
            "duration": 12.5,
            "extractor_key": "Youtube",
            "formats": [{"a": 1}],
        },
    )
    assert info.title == "T"
    assert info.uploader == "U"
    assert info.duration_seconds == 12.5
    assert info.extractor == "Youtube"
    assert "formats" not in info.raw


# --- fake downloader ----------------------------------------------------------


def test_fake_downloader_copies_source(tmp_path: Path):
    source = tmp_path / "src.mp4"
    source.write_bytes(b"video")
    downloader = FakeDownloader(source=source)
    result = downloader.download("https://example.com/v", tmp_path / "out", "stem")
    assert result.path.read_bytes() == b"video"
    # download() delegates to probe() for metadata, so the URL is seen twice.
    assert downloader.calls == ["https://example.com/v", "https://example.com/v"]


def test_fake_downloader_raises_when_configured(tmp_path: Path):
    downloader = FakeDownloader(error="boom")
    with pytest.raises(DownloadError, match="boom"):
        downloader.download("https://example.com/v", tmp_path, "stem")


def test_cookies_text_is_written_to_an_owner_only_temp_file(tmp_path):
    """A managed host can only give us env vars, not a mounted file.

    The jar holds a signed-in session, so it must not land on a world-readable
    path: mkstemp gives 0600, and that is the property worth asserting.
    """
    import os
    import stat

    jar = "# Netscape HTTP Cookie File\n.x\tTRUE\t/\tTRUE\t0\ta\tb\n"
    downloader = YtDlpDownloader(cookies_text=jar)
    path = downloader._cookie_path()

    assert path and Path(path).exists()
    assert Path(path).read_text().startswith("# Netscape HTTP Cookie File")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert downloader._base_options()["cookiefile"] == path


def test_cookies_file_takes_precedence_over_text(tmp_path):
    jar = tmp_path / "cookies.txt"
    jar.write_text("# Netscape HTTP Cookie File\n")
    downloader = YtDlpDownloader(cookies_file=str(jar), cookies_text="ignored")
    assert downloader._cookie_path() == str(jar)


def test_no_cookies_means_no_cookiefile_option():
    assert "cookiefile" not in YtDlpDownloader()._base_options()


def test_cookie_temp_file_is_reused_across_downloads():
    """Re-parsing the jar per download is waste; one file per downloader is enough."""
    downloader = YtDlpDownloader(cookies_text="# Netscape HTTP Cookie File\n")
    assert downloader._cookie_path() == downloader._cookie_path()
