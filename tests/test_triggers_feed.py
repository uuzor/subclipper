"""Notification parsing.

The body is untrusted: it arrives on a public endpoint from anyone who knows the
URL. These tests pin the shape checks that stop a hostile notification from
producing a URL we later fetch or a row we later act on.
"""

from __future__ import annotations

import pytest

from quickreel.triggers.feed import (
    MAX_NOTIFICATION_BYTES,
    NotificationParseError,
    parse_notification,
)

CHANNEL = "UCuAXFkgsw1L7xaCfnd5JJOw"
VIDEO = "dQw4w9WgXcQ"


def _feed(video_id: str = VIDEO, channel_id: str = CHANNEL, link: str | None = None) -> bytes:
    url = link if link is not None else f"https://www.youtube.com/watch?v={video_id}"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015">
  <entry>
    <yt:videoId>{video_id}</yt:videoId>
    <yt:channelId>{channel_id}</yt:channelId>
    <title>A new video</title>
    <link rel="alternate" href="{url}"/>
    <published>2026-01-01T00:00:00+00:00</published>
    <author><name>Some Channel</name></author>
  </entry>
</feed>""".encode()


def test_parses_a_well_formed_notification():
    notification = parse_notification(_feed())
    assert notification.video_id == VIDEO
    assert notification.channel_id == CHANNEL
    assert notification.title == "A new video"
    assert notification.author == "Some Channel"
    assert notification.canonical_url == f"https://www.youtube.com/watch?v={VIDEO}"


def test_canonical_url_is_built_when_the_feed_omits_one():
    notification = parse_notification(_feed(link=""))
    assert notification.canonical_url == f"https://www.youtube.com/watch?v={VIDEO}"


def test_rejects_an_empty_body():
    with pytest.raises(NotificationParseError, match="empty"):
        parse_notification(b"")


def test_rejects_an_oversized_body():
    """A body this large is not a real notification, and parsing it is wasted work."""
    huge = b"<feed>" + b"a" * MAX_NOTIFICATION_BYTES + b"</feed>"
    with pytest.raises(NotificationParseError, match="exceeds"):
        parse_notification(huge)


def test_rejects_non_xml():
    with pytest.raises(NotificationParseError, match="valid XML"):
        parse_notification(b"this is not xml at all")


def test_rejects_a_feed_with_no_entry():
    with pytest.raises(NotificationParseError, match="no <entry>"):
        parse_notification(b'<feed xmlns="http://www.w3.org/2005/Atom"></feed>')


@pytest.mark.parametrize(
    "bad_video_id",
    [
        "short",
        "way-too-long-video-id",
        "has spaces",
        "../../etc/passwd",
        "dQw4w9WgXc.",
    ],
)
def test_rejects_a_video_id_that_is_not_a_real_id(bad_video_id):
    """A video id reaches a URL we later fetch, so its shape is enforced."""
    with pytest.raises(NotificationParseError, match="valid YouTube id"):
        parse_notification(_feed(video_id=bad_video_id))


@pytest.mark.parametrize("bad_channel_id", ["notachannel", "UCshort", "", "AB" * 12])
def test_rejects_a_channel_id_that_is_not_a_real_id(bad_channel_id):
    with pytest.raises(NotificationParseError):
        parse_notification(_feed(channel_id=bad_channel_id))


def test_drops_a_link_that_does_not_name_the_video():
    """A notification claiming video A but linking elsewhere is not trusted."""
    notification = parse_notification(_feed(link="https://evil.example/other"))
    assert notification.video_url == ""
    assert notification.canonical_url == f"https://www.youtube.com/watch?v={VIDEO}"


def test_entity_expansion_does_not_blow_up():
    """A billion-laughs payload must not expand into memory exhaustion."""
    payload = b"""<?xml version="1.0"?>
<!DOCTYPE lolz [
  <!ENTITY lol "lol">
  <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
]>
<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>&lol2;</title></entry></feed>"""
    # Either it is refused outright or parsed without expanding; both are fine,
    # what must not happen is unbounded expansion.
    with pytest.raises(NotificationParseError):
        parse_notification(payload)
