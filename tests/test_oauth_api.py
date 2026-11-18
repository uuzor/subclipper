"""OAuth callback over HTTP.

The callback is reached by a redirect from a third-party site, so the tests
focus on what it must not trust: a missing state, a forged state, a state issued
for another platform, and a state that has aged out. Every failure must land the
user back in the app with a reason, never on a raw JSON error.
"""

from __future__ import annotations

import time

import pytest

from quickreel import oauth
from quickreel.config import get_settings


@pytest.fixture(autouse=True)
def _oauth_config(monkeypatch):
    monkeypatch.setenv("QUICKREEL_OAUTH_STATE_SECRET", "test-secret")
    monkeypatch.setenv("QUICKREEL_TIKTOK_CLIENT_ID", "client-123")
    monkeypatch.setenv("QUICKREEL_PUBLIC_BASE_URL", "https://clipviralmoment.com")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _clean_publish_targets():
    """Clear connected accounts before each test.

    The suite shares one SQLite file, so a target created by an earlier test
    would otherwise appear in a listing this test asserts on. Deleting up front
    makes each test's view of the accounts its own.
    """
    from sqlalchemy import delete

    from quickreel.db import get_sessionmaker
    from quickreel.models import PublishTarget

    session = get_sessionmaker()()
    try:
        session.execute(delete(PublishTarget))
        session.commit()
    finally:
        session.close()
    yield


def test_start_returns_a_consent_url(client):
    response = client.get("/api/v1/oauth/tiktok/start")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["platform"] == "tiktok"
    assert body["authorize_url"].startswith("https://www.tiktok.com/v2/auth/authorize/?")
    assert body["state"]


def test_start_reports_an_unconfigured_platform_as_conflict(client, monkeypatch):
    monkeypatch.setenv("QUICKREEL_TIKTOK_CLIENT_ID", "")
    get_settings.cache_clear()
    response = client.get("/api/v1/oauth/tiktok/start")
    assert response.status_code == 409
    assert "QUICKREEL_TIKTOK_CLIENT_ID" in response.json()["detail"]


def test_start_rejects_an_unknown_platform(client):
    assert client.get("/api/v1/oauth/myspace/start").status_code == 404


def test_providers_endpoint_reports_configuration_without_secrets(client):
    body = client.get("/api/v1/oauth/providers").json()
    by_name = {p["platform"]: p for p in body["providers"]}
    assert by_name["tiktok"]["configured"] is True
    assert by_name["instagram_reels"]["configured"] is False
    # No credential material may appear in the provider listing.
    assert "client-123" not in str(body)


def test_callback_without_state_redirects_back_with_a_reason(client):
    response = client.get("/api/v1/oauth/tiktok/callback?code=abc", follow_redirects=False)
    assert response.status_code == 303
    assert "connected=error" in response.headers["location"]


def test_callback_with_a_forged_state_is_refused(client):
    response = client.get(
        "/api/v1/oauth/tiktok/callback?code=abc&state=nonce.1.tiktok.AAAA",
        follow_redirects=False,
    )
    assert response.status_code == 303
    location = response.headers["location"]
    assert "connected=error" in location
    assert "signature" in location


def test_callback_with_a_state_for_another_platform_is_refused(client):
    """A state issued for one platform must not work on another's callback."""
    state = oauth.sign_state("instagram_reels")
    response = client.get(
        f"/api/v1/oauth/tiktok/callback?code=abc&state={state}",
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "connected=error" in response.headers["location"]


def test_callback_with_an_expired_state_is_refused(client):
    state = oauth.sign_state("tiktok", now=time.time() - 7200)
    response = client.get(
        f"/api/v1/oauth/tiktok/callback?code=abc&state={state}",
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "expired" in response.headers["location"]


def test_user_denial_redirects_with_the_platform_reason(client):
    response = client.get(
        "/api/v1/oauth/tiktok/callback?error=access_denied&error_description=User+said+no",
        follow_redirects=False,
    )
    assert response.status_code == 303
    location = response.headers["location"]
    assert "connected=error" in location
    assert "User" in location


def test_successful_callback_stores_the_target_and_never_leaks_tokens(client):
    """The happy path, with the token exchange stubbed at the oauth seam."""
    state = oauth.sign_state("tiktok")

    def fake_exchange(platform: str, code: str) -> dict:
        assert platform == "tiktok"
        assert code == "auth-code"
        return {
            "access_token": "access-token-value",
            "refresh_token": "refresh-token-value",
            "open_id": "open-id-42",
            "expires_in": 86400,
            "scope": "video.publish",
            "display_name": "My TikTok",
        }

    import quickreel.api.routes.oauth as oauth_routes

    original = oauth_routes.oauth.exchange_code
    oauth_routes.oauth.exchange_code = fake_exchange
    try:
        response = client.get(
            f"/api/v1/oauth/tiktok/callback?code=auth-code&state={state}",
            follow_redirects=False,
        )
    finally:
        oauth_routes.oauth.exchange_code = original

    assert response.status_code == 303
    assert "connected=tiktok" in response.headers["location"]

    listing = client.get("/api/v1/publish-targets")
    targets = listing.json()
    assert len(targets) == 1
    assert targets[0]["external_account_id"] == "open-id-42"
    assert targets[0]["display_name"] == "My TikTok"
    assert targets[0]["status"] == "active"

    # The token must not be readable through the API.
    assert "access-token-value" not in listing.text
    assert "refresh-token-value" not in listing.text
    assert "credentials" not in listing.text


def test_reconnecting_updates_the_existing_target(client):
    """Re-authorising must not create a duplicate account row."""

    def exchange_with(token: str):
        return lambda platform, code: {
            "access_token": token,
            "open_id": "same-account",
            "expires_in": 100,
        }

    import quickreel.api.routes.oauth as oauth_routes

    original = oauth_routes.oauth.exchange_code
    try:
        oauth_routes.oauth.exchange_code = exchange_with("first-token")
        client.get(
            f"/api/v1/oauth/tiktok/callback?code=c&state={oauth.sign_state('tiktok')}",
            follow_redirects=False,
        )
        oauth_routes.oauth.exchange_code = exchange_with("second-token")
        client.get(
            f"/api/v1/oauth/tiktok/callback?code=c&state={oauth.sign_state('tiktok')}",
            follow_redirects=False,
        )
    finally:
        oauth_routes.oauth.exchange_code = original

    targets = client.get("/api/v1/publish-targets").json()
    assert len(targets) == 1
    assert targets[0]["external_account_id"] == "same-account"


def test_reconnect_clears_a_needs_reauth_target(client):
    """The reconnect prompt exists to fix this state, so reconnecting must clear it."""
    created = client.post(
        "/api/v1/publish-targets",
        json={
            "platform": "tiktok",
            "external_account_id": "stale-account",
            "display_name": "Stale",
            "credentials": {"access_token": "old"},
        },
    )
    assert created.status_code == 201
    target_id = created.json()["id"]

    # Put the target into the needs_reauth state, as a failed publish would.
    from quickreel.db import get_sessionmaker
    from quickreel.models import PublishTarget, PublishTargetStatus

    session = get_sessionmaker()()
    try:
        target = session.get(PublishTarget, target_id)
        target.status = PublishTargetStatus.needs_reauth
        target.status_detail = "token expired"
        session.commit()
    finally:
        session.close()

    def exchange(platform: str, code: str) -> dict:
        return {"access_token": "fresh", "open_id": "stale-account", "expires_in": 100}

    import quickreel.api.routes.oauth as oauth_routes

    original = oauth_routes.oauth.exchange_code
    oauth_routes.oauth.exchange_code = exchange
    try:
        response = client.get(
            f"/api/v1/oauth/tiktok/callback?code=c&state={oauth.sign_state('tiktok')}",
            follow_redirects=False,
        )
    finally:
        oauth_routes.oauth.exchange_code = original

    assert response.status_code == 303
    targets = client.get("/api/v1/publish-targets").json()
    assert len(targets) == 1
    assert targets[0]["status"] == "active"
    assert targets[0]["status_detail"] == ""
