"""OAuth connect flow.

The state parameter is the security boundary here: it is what lets a callback
that arrives on a separate request prove this server started the flow. These
tests pin the properties that make that true, and the failure modes that must
not be silently accepted.
"""

from __future__ import annotations

import time

import pytest

from quickreel import oauth
from quickreel.config import get_settings
from quickreel.oauth import OAuthError


@pytest.fixture(autouse=True)
def _oauth_secret(monkeypatch):
    """A known secret, so signatures are reproducible across a test."""
    monkeypatch.setenv("QUICKREEL_OAUTH_STATE_SECRET", "test-secret")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_state_round_trips_and_names_its_platform():
    state = oauth.sign_state("tiktok")
    assert oauth.verify_state(state) == "tiktok"


def test_state_is_rejected_after_its_ttl():
    state = oauth.sign_state("tiktok", now=time.time() - 3600)
    with pytest.raises(OAuthError, match="expired"):
        oauth.verify_state(state)


def test_state_with_a_forged_signature_is_rejected():
    state = oauth.sign_state("tiktok")
    nonce, issued, platform, _signature = state.split(".")
    forged = f"{nonce}.{issued}.{platform}.{'A' * 43}"
    with pytest.raises(OAuthError, match="signature"):
        oauth.verify_state(forged)


def test_state_cannot_be_edited_to_change_platform():
    """The platform is inside the signed payload, so it cannot be swapped."""
    state = oauth.sign_state("tiktok")
    nonce, issued, _platform, signature = state.split(".")
    tampered = f"{nonce}.{issued}.instagram_reels.{signature}"
    with pytest.raises(OAuthError, match="signature"):
        oauth.verify_state(tampered)


def test_malformed_state_is_rejected():
    for bad in ("", "one.two", "a.b.c.d.e", "...."):
        with pytest.raises(OAuthError):
            oauth.verify_state(bad)


def test_each_flow_gets_a_distinct_state():
    """Two flows started at the same instant must not share a state."""
    assert oauth.sign_state("tiktok") != oauth.sign_state("tiktok")


def test_a_different_secret_cannot_verify_a_state(monkeypatch):
    state = oauth.sign_state("tiktok")
    monkeypatch.setenv("QUICKREEL_OAUTH_STATE_SECRET", "a-different-secret")
    get_settings.cache_clear()
    with pytest.raises(OAuthError, match="signature"):
        oauth.verify_state(state)


def test_redirect_uri_uses_the_configured_public_base(monkeypatch):
    monkeypatch.setenv("QUICKREEL_PUBLIC_BASE_URL", "https://clipviralmoment.com")
    get_settings.cache_clear()
    assert oauth.redirect_uri("tiktok") == (
        "https://clipviralmoment.com/api/v1/oauth/tiktok/callback"
    )


def test_authorization_url_requires_a_configured_client(monkeypatch):
    """A missing client id is a setup error with a fixable message, not a crash."""
    monkeypatch.setenv("QUICKREEL_TIKTOK_CLIENT_ID", "")
    get_settings.cache_clear()
    with pytest.raises(OAuthError, match="QUICKREEL_TIKTOK_CLIENT_ID"):
        oauth.authorization_url("tiktok")


def test_authorization_url_carries_the_expected_parameters(monkeypatch):
    monkeypatch.setenv("QUICKREEL_TIKTOK_CLIENT_ID", "client-123")
    get_settings.cache_clear()
    url, state = oauth.authorization_url("tiktok")
    assert url.startswith("https://www.tiktok.com/v2/auth/authorize/?")
    assert "client_id=client-123" in url
    assert "response_type=code" in url
    assert "video.publish" in url
    assert oauth.verify_state(state) == "tiktok"


def test_scopes_are_minimal_for_publishing():
    """Requesting more than we need is what makes a review slow, or fail."""
    for platform in ("tiktok", "instagram_reels", "youtube_shorts"):
        provider = oauth.provider_for(platform)
        joined = " ".join(provider.scopes)
        for over_broad in ("direct_messages", "ads_management", "mail", "followers"):
            assert over_broad not in joined


def test_credentials_are_redacted_for_logging():
    rendered = oauth.credentials_json(
        {"access_token": "secret-a", "refresh_token": "secret-b", "scope": "video.publish"}
    )
    assert "secret-a" not in rendered
    assert "secret-b" not in rendered
    assert "video.publish" in rendered


def test_unknown_platform_is_rejected():
    with pytest.raises(OAuthError, match="no OAuth provider"):
        oauth.provider_for("myspace")


def test_state_fingerprint_is_not_the_state():
    state = oauth.sign_state("tiktok")
    fingerprint = oauth.state_fingerprint(state)
    assert fingerprint != state
    assert state not in fingerprint
