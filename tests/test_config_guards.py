"""Configuration safety guards.

The production guards exist because both failure modes are silent: publishing
through a stub reports success while nothing reaches the platform, and a missing
OAuth state secret makes account-linking forgeable. Neither should be
discoverable by a user noticing their posts never appeared.
"""

from __future__ import annotations

import pytest

from quickreel.config import Settings


def _settings(**overrides) -> Settings:
    """Build Settings from an explicit mapping, ignoring the ambient .env."""
    base = {
        "env": "local",
        "data_dir": "/tmp/quickreel-cfg-test",
        "publishing_mode": "",
        "oauth_state_secret": "",
        "youtube_callback_secret": "",
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


def _production(**overrides) -> Settings:
    """Production settings with every required secret present, so a test can
    disable exactly one and see only that failure."""
    base = {
        "env": "production",
        "publishing_mode": "live",
        "oauth_state_secret": "s3cret",
        "youtube_callback_secret": "callback-secret",
        "public_base_url": "https://clipviralmoment.com",
    }
    base.update(overrides)
    return _settings(**base)


def test_local_defaults_to_mock_mode():
    """The offline default is what makes the loop testable without credentials."""
    assert _settings(env="local").effective_publishing_mode == "mock"


def test_production_defaults_to_live_mode():
    """Forgetting to set the mode in production must fail safe, not publish to a stub."""
    assert _production().effective_publishing_mode == "live"


def test_production_refuses_mock_mode():
    with pytest.raises(ValueError, match="PUBLISHING_MODE=mock"):
        _production(publishing_mode="mock")


def test_production_refuses_missing_oauth_state_secret():
    with pytest.raises(ValueError, match="OAUTH_STATE_SECRET"):
        _production(oauth_state_secret="")


def test_production_refuses_missing_youtube_callback_secret():
    with pytest.raises(ValueError, match="YOUTUBE_CALLBACK_SECRET"):
        _production(youtube_callback_secret="")


def test_production_reports_every_problem_at_once():
    """One restart should surface all misconfiguration, not one per attempt."""
    with pytest.raises(ValueError) as excinfo:
        _settings(env="production", publishing_mode="mock")
    message = str(excinfo.value)
    assert "PUBLISHING_MODE" in message
    assert "OAUTH_STATE_SECRET" in message
    assert "YOUTUBE_CALLBACK_SECRET" in message


@pytest.mark.parametrize("env", ["local", "test", "staging", "development"])
def test_non_production_allows_mock_mode(env):
    assert _settings(env=env, publishing_mode="mock").effective_publishing_mode == "mock"


def test_unknown_publishing_mode_is_rejected():
    """A typo like 'moc' must not silently mean live."""
    with pytest.raises(ValueError, match="must be 'mock' or 'live'"):
        _settings(publishing_mode="moc")


def test_publishing_mode_is_normalised():
    assert _settings(publishing_mode="  LIVE  ").effective_publishing_mode == "live"


def test_production_refuses_s3_without_a_bucket():
    """Ephemeral disk plus no bucket is a render that vanishes on redeploy.

    The row still says ``rendered`` and the player 404s, which reads as a
    rendering bug rather than a storage misconfiguration.
    """
    with pytest.raises(ValueError, match="QUICKREEL_S3_BUCKET"):
        _production(storage_backend="s3", s3_bucket="")


def test_production_accepts_s3_with_a_bucket():
    settings = _production(storage_backend="s3", s3_bucket="clips")
    assert settings.s3_bucket == "clips"


def test_postgres_urls_get_the_driver_that_is_installed():
    """A provider's URL must work unmodified.

    Neon, Render and Heroku all hand out ``postgres://`` or plain
    ``postgresql://``, which SQLAlchemy maps to psycopg2. This project depends
    on psycopg 3, so an unrewritten URL fails on the first connection with a
    missing-module error that looks like a code fault.
    """
    for url in ("postgres://u:p@host/db", "postgresql://u:p@host/db"):
        rewritten = _settings(database_url=url).database_url
        assert rewritten == "postgresql+psycopg://u:p@host/db"


def test_an_already_pinned_postgres_url_is_left_alone():
    url = "postgresql+psycopg://u:p@host/db"
    assert _settings(database_url=url).database_url == url


def test_sqlite_urls_are_accepted():
    url = "sqlite+pysqlite:///./data/quickreel.db"
    assert _settings(database_url=url).database_url == url


def test_s3_settings_read_a_provisioners_plain_names(monkeypatch):
    """A provisioner writes S3_BUCKET, not QUICKREEL_S3_BUCKET.

    Re-provisioning must not silently stop configuring the app, so both
    spellings are read.
    """
    monkeypatch.setenv("S3_BUCKET", "provisioned-bucket")
    monkeypatch.setenv("S3_ENDPOINT", "https://s3.example.com")
    monkeypatch.setenv("S3_REGION", "eu-west-1")
    monkeypatch.setenv("S3_ACCESS_KEY_ID", "key-id")
    monkeypatch.setenv("S3_SECRET_ACCESS_KEY", "secret")

    settings = _settings()

    assert settings.s3_bucket == "provisioned-bucket"
    assert settings.s3_endpoint_url == "https://s3.example.com"
    assert settings.s3_region == "eu-west-1"
    assert settings.s3_access_key_id == "key-id"
    assert settings.s3_secret_access_key == "secret"
