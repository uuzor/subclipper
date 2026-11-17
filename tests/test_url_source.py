"""Tests for source URL validation and SSRF protection.

The downloader is faked throughout, so no network access is required and the
tests are deterministic. The important assertions are the rejections: a public
URL must pass, and everything that points inside our own network must not.
"""

from __future__ import annotations

import pytest

from quickreel.media.url_source import (
    BlockedSourceUrl,
    InvalidSourceUrl,
    allowed_hosts_from_setting,
    validate_source_url,
)

# --- accepted -----------------------------------------------------------------


def test_accepts_plain_https_url():
    result = validate_source_url("https://example.com/watch?v=abc", resolve_dns=False)
    assert result.host == "example.com"
    assert result.url == "https://example.com/watch?v=abc"


def test_accepts_bare_host_without_scheme():
    result = validate_source_url("youtube.com/watch?v=abc", resolve_dns=False)
    assert result.url.startswith("https://youtube.com/watch?v=abc")


def test_normalizes_scheme_and_host_case():
    result = validate_source_url("HTTPS://Example.COM/Path", resolve_dns=False)
    assert result.url == "https://example.com/Path"


def test_preserves_port():
    result = validate_source_url("https://example.com:8443/x", resolve_dns=False)
    assert result.url == "https://example.com:8443/x"


def test_strips_fragment():
    result = validate_source_url("https://example.com/x#t=30", resolve_dns=False)
    assert "#" not in result.url


# --- scheme and credential rejections -----------------------------------------


@pytest.mark.parametrize("url", ["ftp://example.com/x", "file:///etc/passwd", "gopher://x/y"])
def test_rejects_disallowed_schemes(url):
    with pytest.raises(InvalidSourceUrl, match="scheme"):
        validate_source_url(url, resolve_dns=False)


def test_rejects_embedded_credentials():
    with pytest.raises(InvalidSourceUrl, match="credentials"):
        validate_source_url("https://user:secret@example.com/x", resolve_dns=False)


def test_rejects_empty_url():
    with pytest.raises(InvalidSourceUrl):
        validate_source_url("   ", resolve_dns=False)


# --- SSRF rejections ----------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:6379/",
        "http://localhost/admin",
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://redis.internal/",
        "http://db.localdomain/",
    ],
)
def test_rejects_blocked_hostnames(url):
    with pytest.raises(BlockedSourceUrl):
        validate_source_url(url, resolve_dns=False)


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",  # loopback
        "10.0.0.5",  # private
        "192.168.1.1",  # private
        "172.16.0.1",  # private
        "169.254.169.254",  # link-local cloud metadata
        "0.0.0.0",  # unspecified
        "[::1]",  # IPv6 loopback
        "[fd00::1]",  # IPv6 unique local
        "[::ffff:127.0.0.1]",  # IPv4-mapped IPv6 loopback
    ],
)
def test_rejects_non_public_literal_addresses(host):
    with pytest.raises(BlockedSourceUrl, match="non-public"):
        validate_source_url(f"http://{host}/video", resolve_dns=False)


def test_rejects_hostname_that_resolves_to_loopback():
    # localtest.me is a public DNS name that always resolves to 127.0.0.1.
    # This is the classic SSRF bypass, and it must be caught by the DNS check.
    with pytest.raises(BlockedSourceUrl):
        validate_source_url("http://localtest.me/video")


# --- allowlist ----------------------------------------------------------------


def test_allowlist_permits_exact_and_subdomain():
    allowed = ("youtube.com", "tiktok.com")
    assert validate_source_url(
        "https://youtube.com/watch?v=1", allowed_hosts=allowed, resolve_dns=False
    ).host == "youtube.com"
    assert validate_source_url(
        "https://www.youtube.com/watch?v=1", allowed_hosts=allowed, resolve_dns=False
    ).host == "www.youtube.com"


def test_allowlist_rejects_unlisted_host():
    with pytest.raises(BlockedSourceUrl, match="allowlist"):
        validate_source_url(
            "https://vimeo.com/123", allowed_hosts=("youtube.com",), resolve_dns=False
        )


def test_allowlist_does_not_match_partial_label():
    # "notyoutube.com" must not match an allowlist entry of "youtube.com".
    with pytest.raises(BlockedSourceUrl):
        validate_source_url(
            "https://notyoutube.com/x", allowed_hosts=("youtube.com",), resolve_dns=False
        )


def test_allowed_hosts_from_setting_parses_and_trims():
    assert allowed_hosts_from_setting(" youtube.com , tiktok.com ,, ") == (
        "youtube.com",
        "tiktok.com",
    )
    assert allowed_hosts_from_setting("") == ()
