"""Triggering ingestion from an external source.

The package answers one question: "a channel published a video — now what?" It
is deliberately not part of :mod:`quickreel.pipeline`, which assumes a project
already exists, nor of :mod:`quickreel.api`, which is transport. The pieces are:

- :mod:`quickreel.triggers.base` — the hub protocol and the failure vocabulary.
- :mod:`quickreel.triggers.feed` — parsing the untrusted notification body.
- :mod:`quickreel.triggers.hub` — the mock and real hub clients, and the registry.
- :mod:`quickreel.triggers.youtube` — subscription lifecycle and ingest policy.

Adding another trigger source (an RSS poller, a webhook from another platform)
means a new hub client and a new notification parser. The subscription model,
the event log, and the ingest policy are source-agnostic.
"""

from __future__ import annotations

from .base import (
    HubClient,
    HubError,
    Notification,
    PermanentHubError,
    SubscriptionRequest,
    SubscriptionResult,
    TransientHubError,
)
from .feed import NotificationParseError, parse_notification
from .hub import (
    HttpHubClient,
    MockHubClient,
    callback_url_for,
    default_hub_client,
    topic_url_for,
)

__all__ = [
    "HttpHubClient",
    "HubClient",
    "HubError",
    "MockHubClient",
    "Notification",
    "NotificationParseError",
    "PermanentHubError",
    "SubscriptionRequest",
    "SubscriptionResult",
    "TransientHubError",
    "callback_url_for",
    "default_hub_client",
    "parse_notification",
    "topic_url_for",
]
