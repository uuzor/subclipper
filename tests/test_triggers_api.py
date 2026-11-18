"""Trigger endpoints over HTTP.

The callback is public and unauthenticated, so the tests focus on what stops a
stranger from making us process an arbitrary channel: the shared secret, the
optional signature, and the subscription allowlist. Each is checked to fail
closed.
"""

from __future__ import annotations

import hashlib
import hmac

import pytest

from quickreel.config import get_settings
from quickreel.models import ChannelSubscription

CHANNEL = "UCuAXFkgsw1L7xaCfnd5JJOw"
VIDEO = "dQw4w9WgXcQ"


def _feed(video_id: str = VIDEO, channel_id: str = CHANNEL) -> bytes:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015">
  <entry>
    <yt:videoId>{video_id}</yt:videoId>
    <yt:channelId>{channel_id}</yt:channelId>
    <title>A new video</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v={video_id}"/>
  </entry>
</feed>""".encode()


@pytest.fixture(autouse=True)
def _trigger_env(monkeypatch):
    monkeypatch.setenv("QUICKREEL_PUBLISHING_MODE", "mock")
    monkeypatch.setenv("QUICKREEL_PUBLIC_BASE_URL", "https://clipviralmoment.com")
    monkeypatch.setenv("QUICKREEL_YOUTUBE_CALLBACK_SECRET", "")
    monkeypatch.setenv("QUICKREEL_YOUTUBE_SIGNING_SECRET", "")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _clean_trigger_rows():
    from sqlalchemy import delete

    from quickreel.db import get_sessionmaker
    from quickreel.models import YouTubeEvent

    session = get_sessionmaker()()
    try:
        for model in (YouTubeEvent, ChannelSubscription):
            session.execute(delete(model))
        session.commit()
    finally:
        session.close()
    yield


def test_hub_info_reports_the_callback_url(client):
    body = client.get("/api/v1/triggers/youtube/hub").json()
    assert body["callback_url"].endswith("/api/v1/triggers/youtube/notify")
    assert body["client"] == "mock"


def test_subscribe_creates_an_active_subscription(client):
    response = client.post(
        "/api/v1/triggers/youtube/subscriptions",
        json={"channel_id": CHANNEL, "channel_title": "Some Channel", "authorized_by": "owner"},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "active"
    assert body["channel_id"] == CHANNEL


def test_subscribe_rejects_a_malformed_channel_id(client):
    response = client.post(
        "/api/v1/triggers/youtube/subscriptions", json={"channel_id": "not-a-channel"}
    )
    assert response.status_code == 422


def test_verification_echoes_the_challenge(client):
    response = client.get(
        "/api/v1/triggers/youtube/notify",
        params={"hub.mode": "subscribe", "hub.challenge": "abc123", "hub.topic": "t"},
    )
    assert response.status_code == 200
    assert response.text == "abc123"


def test_verification_without_a_challenge_is_a_bad_request(client):
    response = client.get("/api/v1/triggers/youtube/notify", params={"hub.mode": "subscribe"})
    assert response.status_code == 400


def test_notification_for_a_subscribed_channel_creates_a_project(client, monkeypatch):
    """The full path: subscribe, then a hub notification, then a project."""
    from quickreel.triggers import hub as hub_module

    # The probe is what fetches metadata; stub it so no download happens.
    class _Info:
        duration_seconds = 3600.0
        title = "Long video"
        raw: dict = {}

    monkeypatch.setattr(
        "quickreel.api.routes.triggers.get_downloader",
        lambda: type("D", (), {"probe": staticmethod(lambda url: _Info())})(),
    )
    assert hub_module is not None  # imported for clarity

    client.post(
        "/api/v1/triggers/youtube/subscriptions",
        json={"channel_id": CHANNEL, "authorized_by": "owner"},
    )

    response = client.post(
        "/api/v1/triggers/youtube/notify",
        content=_feed(),
        headers={"Content-Type": "application/atom+xml"},
    )
    assert response.status_code == 204

    events = client.get("/api/v1/triggers/youtube/events").json()
    assert len(events) == 1
    assert events[0]["status"] == "queued"
    assert events[0]["project_id"]


def test_notification_for_an_unsubscribed_channel_is_recorded_but_ignored(client):
    response = client.post(
        "/api/v1/triggers/youtube/notify",
        content=_feed(channel_id="UC1234567890123456789012"),
    )
    # Still 204: the hub cannot fix this by retrying.
    assert response.status_code == 204
    events = client.get("/api/v1/triggers/youtube/events").json()
    assert events[0]["status"] == "ignored"


def test_a_malformed_notification_is_accepted_and_logged(client):
    response = client.post("/api/v1/triggers/youtube/notify", content=b"not xml")
    assert response.status_code == 204
    # Nothing was recorded, because nothing could be parsed.
    assert client.get("/api/v1/triggers/youtube/events").json() == []


def test_a_configured_secret_is_required_on_the_callback(client, monkeypatch):
    monkeypatch.setenv("QUICKREEL_YOUTUBE_CALLBACK_SECRET", "s3cret")
    get_settings.cache_clear()

    # Without the secret: not found.
    assert client.post("/api/v1/triggers/youtube/notify", content=_feed()).status_code == 404
    # With the wrong secret: not found.
    assert client.post("/api/v1/triggers/youtube/notify/guess", content=_feed()).status_code == 404
    # With the right secret: accepted.
    assert client.post("/api/v1/triggers/youtube/notify/s3cret", content=_feed()).status_code == 204


def test_a_configured_signing_secret_is_enforced(client, monkeypatch):
    monkeypatch.setenv("QUICKREEL_YOUTUBE_SIGNING_SECRET", "signing-key")
    get_settings.cache_clear()

    body = _feed()
    # Missing signature is refused.
    assert client.post("/api/v1/triggers/youtube/notify", content=body).status_code == 403

    # A correct signature is accepted.
    signature = "sha1=" + hmac.new(b"signing-key", body, hashlib.sha1).hexdigest()
    response = client.post(
        "/api/v1/triggers/youtube/notify",
        content=body,
        headers={"X-Hub-Signature": signature},
    )
    assert response.status_code == 204


def test_delete_subscription_revokes_it(client):
    created = client.post("/api/v1/triggers/youtube/subscriptions", json={"channel_id": CHANNEL})
    subscription_id = created.json()["id"]
    deleted = client.delete(f"/api/v1/triggers/youtube/subscriptions/{subscription_id}")
    assert deleted.status_code == 204
    listing = client.get("/api/v1/triggers/youtube/subscriptions").json()
    assert listing[0]["status"] == "revoked"


def test_renew_endpoint_reports_what_it_renewed(client):
    client.post("/api/v1/triggers/youtube/subscriptions", json={"channel_id": CHANNEL})
    # A fresh lease is not due, so nothing is renewed yet.
    body = client.post("/api/v1/triggers/youtube/subscriptions/renew").json()
    assert body["renewed"] == 0
