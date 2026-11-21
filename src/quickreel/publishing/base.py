"""The platform adapter contract.

An adapter answers one question: "put this rendered file on this account, and
tell me what happened." Everything platform-specific — OAuth refresh, upload
resumption, caption limits, the difference between a rate limit and a rejected
video — lives behind this interface.

Error classification is part of the contract, not an implementation detail. The
scheduler decides whether to retry based on the exception type, so an adapter
that reports a permanent rejection as transient makes the system hammer a
platform that has already said no. The three subclasses below are the vocabulary:

- :class:`TransientError` — worth retrying (network, 5xx, a rate limit).
- :class:`RateLimitError` — retry, but honour ``retry_after_seconds``.
- :class:`PermanentError` — do not retry; a retry cannot succeed.
- :class:`AuthError` — do not retry; the target needs reconnecting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable


class AdapterError(RuntimeError):
    """Base class for adapter failures."""


class TransientError(AdapterError):
    """A failure that may succeed on a later attempt."""


class RateLimitError(TransientError):
    """The platform is throttling us. ``retry_after_seconds`` is a hint."""

    def __init__(self, message: str, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class PermanentError(AdapterError):
    """A failure a retry cannot fix: rejected media, bad caption, blocked post."""


class AuthError(AdapterError):
    """The stored credentials are no longer valid. The target needs reconnecting."""


@dataclass(slots=True)
class PostSpec:
    """Everything an adapter needs to publish one clip.

    ``video_path`` must point at an existing rendered file; the scheduler checks
    this before calling the adapter so a missing render is a clear error rather
    than a mysterious upload failure.
    """

    video_path: Path
    caption: str = ""
    title: str = ""
    # Platform-specific knobs the caller may set (privacy level, hashtags,
    # cover timestamp, whether to allow comments/duet/stitch, ...). Adapters
    # ignore keys they do not understand.
    options: dict = field(default_factory=dict)


@dataclass(slots=True)
class AdapterResult:
    """The outcome of a successful publish."""

    external_post_id: str
    url: str = ""
    # Some platforms accept a post and process it asynchronously. When this is
    # true the scheduler polls ``check_status`` rather than assuming it is live.
    pending: bool = False
    detail: dict = field(default_factory=dict)


@dataclass(slots=True)
class PostStatus:
    """The platform's view of a previously accepted post."""

    state: str  # pending | published | failed
    detail: str = ""
    metrics: dict = field(default_factory=dict)


@runtime_checkable
class PlatformAdapter(Protocol):
    """Publish rendered clips to one platform.

    ``name`` must match the registry key and the ``Platform`` enum value so the
    two never drift apart.
    """

    name: str

    def publish(self, target_credentials: dict, account_id: str, spec: PostSpec) -> AdapterResult:
        """Upload and publish one clip. Raises an ``AdapterError`` subclass."""

    def check_status(self, target_credentials: dict, external_post_id: str) -> PostStatus:
        """Report the platform's current state for a post we already submitted."""

    def fetch_metrics(self, target_credentials: dict, external_post_id: str) -> dict:
        """Return engagement counters for a published post, if the platform exposes them."""
