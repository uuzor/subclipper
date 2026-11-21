"""Adapter registry and mock adapter behaviour.

The registry is the seam that keeps the suite offline, so its mode switch is
worth pinning down: a live run must never silently fall back to a mock.
"""

from __future__ import annotations

import pytest

from quickreel import config
from quickreel.publishing import registry
from quickreel.publishing.base import (
    AdapterResult,
    AuthError,
    PermanentError,
    PostSpec,
    RateLimitError,
    TransientError,
)
from quickreel.publishing.mocks import MockAdapter


@pytest.fixture()
def live_mode(monkeypatch):
    """Force live mode and start from a clean registry."""
    monkeypatch.setenv("QUICKREEL_PUBLISHING_MODE", "live")
    config.get_settings.cache_clear()
    registry.reset_adapters()
    yield
    registry.reset_adapters()
    config.get_settings.cache_clear()


def test_mock_mode_resolves_every_platform(monkeypatch):
    monkeypatch.setenv("QUICKREEL_PUBLISHING_MODE", "mock")
    config.get_settings.cache_clear()
    try:
        for platform in ("tiktok", "youtube_shorts", "instagram_reels", "anything"):
            adapter = registry.get_adapter(platform)
            assert isinstance(adapter, MockAdapter)
            assert adapter.name == platform
    finally:
        config.get_settings.cache_clear()


def test_live_mode_without_a_registered_adapter_raises(live_mode):
    """A silent mock fallback in production would be worse than a loud failure."""
    with pytest.raises(KeyError, match="no live adapter registered"):
        registry.get_adapter("tiktok")


def test_live_mode_returns_the_registered_adapter(live_mode):
    registry.register_adapter("tiktok", lambda: MockAdapter(name="tiktok"))
    assert "tiktok" in registry.available_adapters()
    assert registry.get_adapter("tiktok").name == "tiktok"


def test_mock_adapter_records_publishes(tmp_path):
    video = tmp_path / "c.mp4"
    video.write_bytes(b"\x00")
    adapter = MockAdapter(name="tiktok")
    result = adapter.publish({}, "acct-1", PostSpec(video_path=video, caption="hi"))
    assert isinstance(result, AdapterResult)
    assert result.external_post_id == "tiktok-acct-1-1"
    assert len(adapter.published) == 1


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        ("transient", TransientError),
        ("rate_limit", RateLimitError),
        ("permanent", PermanentError),
        ("auth", AuthError),
    ],
)
def test_mock_adapter_injects_each_failure_type(tmp_path, failure, expected):
    video = tmp_path / "c.mp4"
    video.write_bytes(b"\x00")
    adapter = MockAdapter(name="tiktok", fail_times=1, failure=failure)
    with pytest.raises(expected):
        adapter.publish({}, "acct", PostSpec(video_path=video))
    # The injected failure is consumed, so the next call succeeds.
    assert adapter.publish({}, "acct", PostSpec(video_path=video)).external_post_id


def test_rate_limit_carries_a_retry_hint(tmp_path):
    adapter = MockAdapter(name="tiktok", fail_times=1, failure="rate_limit")
    with pytest.raises(RateLimitError) as excinfo:
        adapter.publish({}, "acct", PostSpec(video_path=tmp_path / "c.mp4"))
    assert excinfo.value.retry_after_seconds == 30.0


def test_mock_adapter_metrics_are_deterministic():
    adapter = MockAdapter(name="tiktok")
    first = adapter.fetch_metrics({}, "post-abc")
    second = adapter.fetch_metrics({}, "post-abc")
    assert first == second
    assert first["views"] > 0


def test_registry_reset_clears_registrations(live_mode):
    registry.register_adapter("tiktok", lambda: MockAdapter(name="tiktok"))
    registry.reset_adapters()
    assert registry.available_adapters() == []
