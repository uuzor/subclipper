"""Trigger contracts: the hub client, the feed parser, and their result types.

An *external trigger* is how a new video arrives without a human pasting a URL.
The mechanism is WebSub (PubSubHubbub): we tell a hub "notify me when this
channel publishes", the hub calls our callback, and we ingest what it names.

The split mirrors :mod:`quickreel.publishing`: a narrow protocol with the
failure vocabulary spelled out, a deterministic mock, and a registry that picks
between them. Nothing in the trigger logic knows which hub implementation it
has, so the whole path is exercisable without the public hub.

Error classification matters here for the same reason it does when publishing.
A hub that returns 5xx is worth retrying; a hub that returns 4xx to a subscribe
call will keep returning 4xx, so retrying is pointless and hides the real
problem (usually a bad callback URL).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


class HubError(RuntimeError):
    """Base class for hub failures."""


class TransientHubError(HubError):
    """A hub failure that may succeed on a later attempt."""


class PermanentHubError(HubError):
    """A hub failure a retry cannot fix: a bad callback URL, a rejected feed."""


@dataclass(slots=True)
class SubscriptionRequest:
    """What to ask the hub for."""

    topic_url: str
    callback_url: str
    # The hub echoes this back as ``hub.secret`` in the notification, which is
    # how a callback knows the payload is for the subscription it thinks it is.
    secret: str = ""
    lease_seconds: int = 432000


@dataclass(slots=True)
class SubscriptionResult:
    """The hub's response to a subscribe or renew call."""

    ok: bool
    # The hub answers a verification request synchronously, so a 202 already
    # means accepted. ``lease_seconds`` is what the hub granted, which may be
    # less than what was asked for and is what the renewal sweep plans around.
    lease_seconds: int = 0
    status_code: int = 0
    detail: str = ""


@dataclass(slots=True)
class Notification:
    """One parsed hub notification.

    A WebSub notification is an Atom feed with one entry. Everything the trigger
    needs is flattened here so the ingestion logic never parses XML.
    """

    channel_id: str
    video_id: str
    title: str = ""
    video_url: str = ""
    published_at: str = ""
    author: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def canonical_url(self) -> str:
        """The watch URL for the video, preferring what the feed supplied."""
        if self.video_url:
            return self.video_url
        return f"https://www.youtube.com/watch?v={self.video_id}"


@runtime_checkable
class HubClient(Protocol):
    """Manage WebSub subscriptions with one hub.

    ``name`` identifies the implementation in logs and in the registry.
    """

    name: str

    def subscribe(self, request: SubscriptionRequest) -> SubscriptionResult:
        """Ask the hub to start notifying ``callback_url`` about ``topic_url``."""

    def renew(self, request: SubscriptionRequest) -> SubscriptionResult:
        """Extend an existing subscription before its lease lapses."""

    def unsubscribe(self, request: SubscriptionRequest) -> SubscriptionResult:
        """Stop notifications. Best-effort: a failure here must not block deletion."""
