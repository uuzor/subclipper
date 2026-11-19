"""Remote video downloading via yt-dlp.

yt-dlp supports YouTube, TikTok, Instagram, Vimeo, X, Facebook and roughly a
thousand other sites, which is why it is used here rather than a per-platform
API. It is an optional dependency (``pip install 'quickreel[download]'``); when
it is absent the registry falls back to a fake so the rest of the app still runs.

Only ``yt_dlp`` is imported lazily, and network access is confined to this
module and the validation step in :mod:`quickreel.media.url_source`.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from ..logging_setup import get_logger

logger = get_logger(__name__)

# Container extensions we are willing to store. yt-dlp is asked to remux into
# mp4 where possible; anything outside this set is rejected rather than passed
# to ffmpeg blindly.
_ALLOWED_EXTENSIONS = frozenset({".mp4", ".mkv", ".webm", ".mov", ".m4v"})


class DownloadError(RuntimeError):
    """Raised when a remote video cannot be fetched."""


@dataclass
class RemoteVideoInfo:
    """Metadata probed from a URL without downloading the whole video."""

    url: str
    title: str = ""
    uploader: str = ""
    duration_seconds: float = 0.0
    thumbnail: str = ""
    extractor: str = ""
    webpage_url: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class DownloadResult:
    path: Path
    info: RemoteVideoInfo
    size_bytes: int = 0


class VideoDownloader(Protocol):
    name: str

    def probe(self, url: str) -> RemoteVideoInfo:
        """Return metadata for ``url`` without downloading the media."""

    def download(self, url: str, destination_dir: Path, stem: str) -> DownloadResult:
        """Download the best available video/audio into ``destination_dir``."""


class YtDlpDownloader:
    """Real downloader backed by yt-dlp.

    ``max_height`` caps the downloaded resolution. Long-form source video is
    expensive to store and every clip is re-encoded to 1080x1920 anyway, so the
    default of 1080 keeps quality while avoiding multi-gigabyte 4K sources.
    """

    name = "yt-dlp"

    def __init__(
        self,
        *,
        max_height: int = 1080,
        timeout_seconds: int = 1800,
        cookies_file: str = "",
        cookies_text: str = "",
        proxy: str = "",
        max_bytes: int = 0,
    ) -> None:
        self.max_height = max_height
        self.timeout_seconds = timeout_seconds
        self.cookies_file = cookies_file
        self.cookies_text = cookies_text
        self.proxy = proxy
        self.max_bytes = max_bytes
        self._temp_cookies: Path | None = None

    def _cookie_path(self) -> str:
        """Path to the cookies yt-dlp should use.

        ``cookies_text`` exists because a managed platform gives you environment
        variables, not a writable path you can mount a secret into. The value is
        written to an owner-only temp file so the cookie jar is never on a
        shared, world-readable path.
        """
        if self.cookies_file:
            return self.cookies_file
        if not self.cookies_text:
            return ""
        if self._temp_cookies is None or not self._temp_cookies.exists():
            handle, name = tempfile.mkstemp(prefix="quickreel-cookies-", suffix=".txt")
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(self.cookies_text)
            os.chmod(name, 0o600)
            self._temp_cookies = Path(name)
        return str(self._temp_cookies)

    def _base_options(self) -> dict:
        options: dict = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "socket_timeout": self.timeout_seconds,
            "retries": 3,
            "fragment_retries": 3,
            "noplaylist": True,  # a playlist URL must not turn into 200 downloads
        }
        cookie_path = self._cookie_path()
        if cookie_path:
            options["cookiefile"] = cookie_path
        if self.proxy:
            options["proxy"] = self.proxy
        return options

    @staticmethod
    def _import_ytdlp():
        try:
            import yt_dlp  # type: ignore import-not-found
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise DownloadError(
                "yt-dlp is not installed; run `pip install 'quickreel[download]'`"
            ) from exc
        return yt_dlp

    def probe(self, url: str) -> RemoteVideoInfo:
        yt_dlp = self._import_ytdlp()
        options = self._base_options() | {"skip_download": True}
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as exc:  # noqa: BLE001 - yt-dlp raises many types
            raise DownloadError(_clean_message(exc)) from exc
        if info is None:
            raise DownloadError("no metadata returned for the supplied URL")
        if info.get("_type") == "playlist":
            entries = info.get("entries") or []
            if not entries:
                raise DownloadError("the URL is an empty playlist")
            info = entries[0]
        return _to_info(url, info)

    def download(self, url: str, destination_dir: Path, stem: str) -> DownloadResult:
        yt_dlp = self._import_ytdlp()
        destination_dir.mkdir(parents=True, exist_ok=True)
        template = str(destination_dir / f"{stem}.%(ext)s")
        height = self.max_height
        options = self._base_options() | {
            "format": (
                f"bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]/"
                f"bestvideo[height<={height}]+bestaudio/"
                f"best[height<={height}]/best"
            ),
            "outtmpl": template,
            "merge_output_format": "mp4",
            "overwrites": True,
        }
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(url, download=True)
                prepared = Path(ydl.prepare_filename(info))
        except Exception as exc:  # noqa: BLE001 - yt-dlp raises many types
            message = _clean_message(exc)
            if _is_bot_check(message):
                raise DownloadError(f"{_COOKIE_HINT} (yt-dlp said: {message})") from exc
            raise DownloadError(message) from exc

        path = _resolve_output(destination_dir, stem, prepared)
        if path is None:
            raise DownloadError("download finished but no media file was produced")
        size = path.stat().st_size
        if self.max_bytes and size > self.max_bytes:
            path.unlink(missing_ok=True)
            raise DownloadError(
                f"downloaded file is {size // (1024 * 1024)} MB, over the "
                f"{self.max_bytes // (1024 * 1024)} MB limit"
            )
        return DownloadResult(path=path, info=_to_info(url, info or {}), size_bytes=size)


class FakeDownloader:
    """Deterministic downloader for tests and offline development.

    It copies a caller-supplied local file into place, so the rest of the
    pipeline can be exercised without touching the network. ``source`` is the
    path to copy; ``error`` forces a failure when set.
    """

    name = "fake-downloader"

    def __init__(self, source: str | Path | None = None, error: str = "") -> None:
        self.source = Path(source) if source else None
        self.error = error
        self.calls: list[str] = []

    def probe(self, url: str) -> RemoteVideoInfo:
        self.calls.append(url)
        if self.error:
            raise DownloadError(self.error)
        return RemoteVideoInfo(
            url=url,
            title="Fake remote video",
            uploader="fake-uploader",
            duration_seconds=20.0,
            extractor="fake",
            webpage_url=url,
            raw={"id": "fake-id", "extractor": "fake", "title": "Fake remote video"},
        )

    def download(self, url: str, destination_dir: Path, stem: str) -> DownloadResult:
        self.calls.append(url)
        if self.error:
            raise DownloadError(self.error)
        if self.source is None:
            raise DownloadError("FakeDownloader has no source file configured")
        destination_dir.mkdir(parents=True, exist_ok=True)
        target = destination_dir / f"{stem}{self.source.suffix.lower() or '.mp4'}"
        target.write_bytes(self.source.read_bytes())
        return DownloadResult(
            path=target,
            info=self.probe(url),
            size_bytes=target.stat().st_size,
        )


def _resolve_output(destination_dir: Path, stem: str, prepared: Path) -> Path | None:
    """Find the produced file, tolerating a container remux changing extension."""
    if prepared.exists():
        return prepared
    candidates = sorted(
        p
        for p in destination_dir.glob(f"{stem}.*")
        if p.suffix.lower() in _ALLOWED_EXTENSIONS and not p.name.endswith(".part")
    )
    return candidates[0] if candidates else None


def _to_info(url: str, info: dict) -> RemoteVideoInfo:
    return RemoteVideoInfo(
        url=url,
        title=str(info.get("title") or ""),
        uploader=str(info.get("uploader") or info.get("channel") or ""),
        duration_seconds=float(info.get("duration") or 0.0),
        thumbnail=str(info.get("thumbnail") or ""),
        extractor=str(info.get("extractor_key") or info.get("extractor") or ""),
        webpage_url=str(info.get("webpage_url") or url),
        raw={k: v for k, v in info.items() if k != "formats"},
    )


def _clean_message(exc: Exception) -> str:
    """Strip the ``ERROR:`` prefix yt-dlp adds and collapse whitespace."""
    message = " ".join(str(exc).split())
    if message.upper().startswith("ERROR:"):
        message = message[len("ERROR:") :].strip()
    return message or "download failed"


# Phrases YouTube uses when it refuses a datacenter IP rather than the request.
# Metadata extraction still succeeds in this case, so the failure only appears
# once the media is fetched.
_BOT_CHECK_MARKERS = (
    "sign in to confirm you",
    "not a bot",
    "confirm your age",
    "http error 403",
    "forbidden",
)
_COOKIE_HINT = (
    "YouTube refuses media downloads from datacenter IPs with a bot check. "
    "Metadata is readable, so the video is valid - only the media request is "
    "blocked. Upload the file, or set QUICKREEL_DOWNLOADER_COOKIES_TEXT to the "
    "contents of a cookies.txt exported from a signed-in browser "
    "(QUICKREEL_DOWNLOADER_COOKIES_FILE also works when you can mount a file), "
    "or QUICKREEL_DOWNLOADER_PROXY to a residential proxy."
)


def _is_bot_check(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in _BOT_CHECK_MARKERS)


_downloader: VideoDownloader | None = None


def set_downloader(downloader: VideoDownloader | None) -> None:
    """Override the process-wide downloader. Used by tests and local tooling."""
    global _downloader
    _downloader = downloader


def get_downloader() -> VideoDownloader:
    """Return the configured downloader.

    Defaults to yt-dlp. If yt-dlp is not importable, a fake is returned so the
    application still starts and URL requests fail with a clear message instead
    of crashing the whole process.
    """
    global _downloader
    if _downloader is not None:
        return _downloader

    from ..config import get_settings

    settings = get_settings()
    downloader = YtDlpDownloader(
        max_height=settings.download_max_height,
        timeout_seconds=settings.download_timeout_seconds,
        cookies_file=settings.downloader_cookies_file,
        cookies_text=settings.downloader_cookies_text,
        proxy=settings.downloader_proxy,
        max_bytes=settings.max_upload_bytes,
    )
    try:
        import yt_dlp  # type: ignore import-not-found  # noqa: F401
    except ImportError:
        logger.warning(
            "downloader.yt_dlp_missing",
            hint="pip install 'quickreel[download]' to enable URL ingestion",
        )
        _downloader = FakeDownloader(error="yt-dlp is not installed on this server")
        return _downloader

    _downloader = downloader
    return _downloader
