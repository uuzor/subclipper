"""Validation for user-supplied source URLs.

Downloading a video from a URL the user supplies means the server makes an
outbound request on their behalf. That is a server-side request forgery (SSRF)
primitive if left unchecked: ``http://169.254.169.254/...`` or
``http://localhost:6379`` would otherwise be reachable from inside our network.

This module is the gate. It enforces, in order:

1. Scheme allowlist (``http``/``https`` only).
2. No embedded credentials (``https://user:pass@host``).
3. Host allowlist, when ``QUICKREEL_ALLOWED_SOURCE_HOSTS`` is set.
4. DNS resolution, rejecting any address in a private, loopback, link-local,
   reserved or otherwise non-global range.

The check runs before the downloader is invoked, and the resolved addresses are
what get inspected rather than the literal hostname, so ``localtest.me`` (which
resolves to 127.0.0.1) is caught.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from ..logging_setup import get_logger

logger = get_logger(__name__)

ALLOWED_SCHEMES = frozenset({"http", "https"})

# Hostnames that are never acceptable regardless of how they resolve.
_BLOCKED_HOSTNAMES = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "metadata",
        "metadata.google.internal",
    }
)
_BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".localdomain")


class SourceUrlError(ValueError):
    """Base class for rejected source URLs."""


class InvalidSourceUrl(SourceUrlError):
    """The URL is malformed or uses a disallowed scheme."""


class BlockedSourceUrl(SourceUrlError):
    """The URL targets a host or address we refuse to contact."""


@dataclass(frozen=True)
class ValidatedSource:
    url: str
    host: str
    resolved_ips: tuple[str, ...]


def _is_global_address(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True only for addresses that are safe to contact.

    ``is_global`` is False for loopback, private, link-local, multicast,
    reserved and unspecified ranges, which is exactly the set we want to refuse.
    IPv4-mapped IPv6 addresses are unwrapped first so ``::ffff:127.0.0.1`` is
    treated as loopback.
    """
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global


def _resolve(host: str) -> tuple[str, ...]:
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise InvalidSourceUrl(f"could not resolve host {host!r}") from exc
    addresses = {info[4][0] for info in infos}
    if not addresses:
        raise InvalidSourceUrl(f"host {host!r} resolved to no addresses")
    return tuple(sorted(addresses))


def _host_matches_allowlist(host: str, allowed: tuple[str, ...]) -> bool:
    host = host.lower().rstrip(".")
    for entry in allowed:
        entry = entry.lower().rstrip(".")
        if host == entry or host.endswith("." + entry):
            return True
    return False


def validate_source_url(
    raw_url: str,
    *,
    allowed_hosts: tuple[str, ...] = (),
    resolve_dns: bool = True,
) -> ValidatedSource:
    """Validate a user-supplied URL and return its canonical form.

    Raises :class:`InvalidSourceUrl` or :class:`BlockedSourceUrl` when the URL
    must not be fetched. ``resolve_dns=False`` skips the network lookup, which is
    useful in tests and for offline validation of the literal host.
    """
    if not raw_url or not raw_url.strip():
        raise InvalidSourceUrl("source URL is empty")

    candidate = raw_url.strip()
    # A bare host such as "youtube.com/watch?v=..." is a common user mistake.
    if "://" not in candidate:
        candidate = "https://" + candidate

    try:
        parts = urlsplit(candidate)
        # Accessing .port raises on malformed input such as "host:notaport".
        port = parts.port
    except ValueError as exc:
        raise InvalidSourceUrl(f"malformed URL: {exc}") from exc

    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise InvalidSourceUrl(
            f"unsupported scheme {parts.scheme!r}; only http and https are allowed"
        )
    if parts.username or parts.password:
        raise InvalidSourceUrl("URLs with embedded credentials are not allowed")

    host = parts.hostname
    if not host:
        raise InvalidSourceUrl("URL has no host")

    host_lower = host.lower().rstrip(".")
    if host_lower in _BLOCKED_HOSTNAMES or host_lower.endswith(_BLOCKED_SUFFIXES):
        raise BlockedSourceUrl(f"host {host!r} is not permitted")

    if allowed_hosts and not _host_matches_allowlist(host_lower, allowed_hosts):
        raise BlockedSourceUrl(
            f"host {host!r} is not in the configured allowlist (QUICKREEL_ALLOWED_SOURCE_HOSTS)"
        )

    resolved: tuple[str, ...] = ()
    # A literal IP address is checked directly, and must be checked even when
    # DNS resolution is skipped: there is nothing to resolve, and a literal
    # private address is exactly the SSRF case we care about.
    try:
        literal = ipaddress.ip_address(host_lower)
    except ValueError:
        literal = None
    if literal is not None:
        if not _is_global_address(literal):
            logger.warning("source_url.blocked_ip", host=host_lower, ip=str(literal))
            raise BlockedSourceUrl(f"host {host!r} is a non-public address ({literal})")
        resolved = (str(literal),)
    elif resolve_dns:
        resolved = _resolve(host_lower)
        for address in resolved:
            ip = ipaddress.ip_address(address)
            if not _is_global_address(ip):
                logger.warning("source_url.blocked_ip", host=host_lower, ip=address)
                raise BlockedSourceUrl(
                    f"host {host!r} resolves to a non-public address ({address})"
                )

    # Rebuild without userinfo and with the lowercased host. IPv6 literals must
    # be re-bracketed, since urlsplit strips the brackets from .hostname.
    display_host = f"[{host_lower}]" if ":" in host_lower else host_lower
    netloc = display_host if port is None else f"{display_host}:{port}"
    normalized = urlunsplit((parts.scheme.lower(), netloc, parts.path, parts.query, ""))
    return ValidatedSource(url=normalized, host=host_lower, resolved_ips=resolved)


def allowed_hosts_from_setting(value: str) -> tuple[str, ...]:
    """Parse the comma-separated ``QUICKREEL_ALLOWED_SOURCE_HOSTS`` setting."""
    return tuple(part.strip() for part in value.split(",") if part.strip())
