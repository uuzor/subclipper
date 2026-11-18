"""Parsing the Atom feed a WebSub hub posts to the callback.

The input is untrusted: it arrives from a public endpoint, and anyone who learns
the callback URL can POST to it. Two consequences drive this module.

**The XML parser must not resolve entities.** ``xml.etree`` is safe against
external-entity expansion by default in Python's standard library, and
``defusedxml`` is used instead when it is installed, because a billion-laughs or
XXE payload in a notification body is a denial-of-service primitive otherwise.
An explicit size cap on the body is the second line of defence.

**Nothing is trusted until it is validated.** The channel id and video id are
checked against their real shapes (YouTube ids are a fixed alphabet and length)
before they are used to build a URL or written to the database. A video id is
never interpolated into a URL without passing that check.
"""

from __future__ import annotations

import re
from xml.etree import ElementTree

from ..logging_setup import get_logger
from .base import Notification

logger = get_logger(__name__)

# Atom feed namespace, plus the YouTube and media extensions the hub includes.
_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}

# YouTube ids are 11 characters from a fixed alphabet for videos, and channel
# ids start with UC. Validating the shape means a hostile notification cannot
# smuggle path traversal or a query injection into a URL we later fetch.
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")

# A notification is a few kilobytes. Anything larger is not a real one.
MAX_NOTIFICATION_BYTES = 512 * 1024


class NotificationParseError(ValueError):
    """The body was not a usable notification."""


def _text(element: ElementTree.Element | None) -> str:
    if element is None or element.text is None:
        return ""
    return element.text.strip()


def _parse_xml(body: bytes) -> ElementTree.Element:
    """Parse untrusted XML with the safest parser available.

    defusedxml refuses entity-expansion and external-entity payloads. It is an
    optional dependency; the standard library parser is used otherwise, which
    does not resolve external entities.
    """
    try:
        from defusedxml.ElementTree import fromstring as safe_fromstring
    except ImportError:
        return ElementTree.fromstring(body)  # noqa: S314 - no external entity resolution
    return safe_fromstring(body)


def parse_notification(body: bytes) -> Notification:
    """Parse a WebSub notification body into a :class:`Notification`.

    Raises :class:`NotificationParseError` for anything that is not a valid,
    well-shaped notification. The caller answers the hub 2xx even then, because
    a malformed notification is not something the hub can fix by retrying.
    """
    if not body:
        raise NotificationParseError("empty notification body")
    if len(body) > MAX_NOTIFICATION_BYTES:
        raise NotificationParseError(f"notification body exceeds {MAX_NOTIFICATION_BYTES} bytes")

    try:
        root = _parse_xml(body)
    except NotificationParseError:
        raise
    except Exception as exc:  # noqa: BLE001 - any parse failure is a rejected body
        # defusedxml raises its own exception types alongside ParseError, so the
        # catch is deliberately broad: every parse failure means the same thing.
        raise NotificationParseError(f"notification is not valid XML: {exc}") from exc

    entry = root.find("atom:entry", _NS)
    if entry is None:
        raise NotificationParseError("notification has no <entry>")

    video_id = _text(entry.find("yt:videoId", _NS))
    channel_id = _text(entry.find("yt:channelId", _NS))

    if not video_id:
        raise NotificationParseError("notification entry has no video id")
    if not _VIDEO_ID_RE.match(video_id):
        raise NotificationParseError(f"video id {video_id!r} is not a valid YouTube id")
    if not channel_id:
        raise NotificationParseError("notification entry has no channel id")
    if not _CHANNEL_ID_RE.match(channel_id):
        raise NotificationParseError(f"channel id {channel_id!r} is not a valid YouTube channel id")

    # The link element that carries the watch URL is rel="alternate".
    video_url = ""
    for link in entry.findall("atom:link", _NS):
        if link.get("rel") == "alternate" and link.get("href"):
            video_url = link.get("href", "").strip()
            break

    # If the feed supplies a URL, it must point at the video it names. A
    # notification that claims video A but links to host B is not trusted.
    if video_url and video_id not in video_url:
        logger.warning(
            "trigger.notification_url_mismatch",
            video_id=video_id,
            video_url=video_url,
        )
        video_url = ""

    title = _text(entry.find("atom:title", _NS))
    published = _text(entry.find("atom:published", _NS)) or _text(entry.find("atom:updated", _NS))
    author = _text(entry.find("atom:author/atom:name", _NS))

    return Notification(
        channel_id=channel_id,
        video_id=video_id,
        title=title,
        video_url=video_url,
        published_at=published,
        author=author,
        raw={"title": title, "channel_id": channel_id},
    )


def extract_channel_id_from_body(body: bytes) -> str:
    """Best-effort channel id, used to log a rejected notification usefully."""
    try:
        root = _parse_xml(body)
    except Exception:  # noqa: BLE001 - logging helper, never raises
        return ""
    entry = root.find("atom:entry", _NS)
    if entry is None:
        return ""
    return _text(entry.find("yt:channelId", _NS))
