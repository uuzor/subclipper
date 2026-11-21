"""Hub clients: the deterministic mock and the real HTTP client.

The mock exists for the same reason :class:`quickreel.publishing.mocks.MockAdapter`
does: the subscribe -> notify -> ingest loop should be testable without talking
to the public hub, and every branch (accepted, throttled, rejected) should be
reachable from a test.
"""

from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from ..config import get_settings
from ..logging_setup import get_logger
from .base import (
    HubClient,
    PermanentHubError,
    SubscriptionRequest,
    SubscriptionResult,
    TransientHubError,
)

logger = get_logger(__name__)


@dataclass
class MockHubClient:
    """In-memory hub that records calls and can inject a failure.

    ``fail_times`` makes the first N calls raise ``failure``, then succeed,
    which is how the retry path is exercised. ``status_code`` is what the hub
    reports, so a 4xx and a 5xx can be distinguished without a network.
    """

    name: str = "mock"
    fail_times: int = 0
    failure: str = "transient"
    status_code: int = 202
    lease_seconds: int = 432000
    subscriptions: list[SubscriptionRequest] = field(default_factory=list)
    renewals: list[SubscriptionRequest] = field(default_factory=list)
    unsubscriptions: list[SubscriptionRequest] = field(default_factory=list)

    def _maybe_fail(self) -> None:
        if self.fail_times <= 0:
            return
        self.fail_times -= 1
        message = f"{self.name}: injected {self.failure} failure"
        if self.failure == "permanent":
            raise PermanentHubError(message)
        raise TransientHubError(message)

    def _result(self) -> SubscriptionResult:
        ok = 200 <= self.status_code < 300
        return SubscriptionResult(
            ok=ok,
            lease_seconds=self.lease_seconds if ok else 0,
            status_code=self.status_code,
            detail="mock hub" if ok else f"mock hub rejected with {self.status_code}",
        )

    def subscribe(self, request: SubscriptionRequest) -> SubscriptionResult:
        self._maybe_fail()
        self.subscriptions.append(request)
        return self._result()

    def renew(self, request: SubscriptionRequest) -> SubscriptionResult:
        self._maybe_fail()
        self.renewals.append(request)
        return self._result()

    def unsubscribe(self, request: SubscriptionRequest) -> SubscriptionResult:
        self._maybe_fail()
        self.unsubscriptions.append(request)
        return self._result()


@dataclass
class HttpHubClient:
    """Real WebSub hub client over HTTP.

    A subscribe is a form POST to the hub URL with ``hub.mode`` and friends. The
    hub then *calls back* to verify the callback before it accepts, so a 202 here
    means "verification request sent", not "subscription live". The lease is
    recorded from what the hub grants, and the status is only marked active once
    the verification callback arrives, which is why ``ChannelSubscription`` has a
    ``pending`` state.
    """

    name: str = "http"

    def _post(self, mode: str, request: SubscriptionRequest) -> SubscriptionResult:
        settings = get_settings()
        data = {
            "hub.mode": mode,
            "hub.topic": request.topic_url,
            "hub.callback": request.callback_url,
            "hub.verify": "async",
        }
        if request.secret:
            data["hub.secret"] = request.secret
        if request.lease_seconds:
            data["hub.lease_seconds"] = str(request.lease_seconds)

        encoded = urllib.parse.urlencode(data).encode("utf-8")
        http_request = urllib.request.Request(  # noqa: S310 - hub URL is configured, not user input
            settings.youtube_hub_url,
            data=encoded,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(http_request, timeout=20) as response:  # noqa: S310
                status = response.status
        except urllib.error.HTTPError as exc:
            # A 4xx to a subscribe call is permanent: the hub is telling us the
            # request is wrong (bad callback, unverifiable topic). Retrying it
            # unchanged just repeats the rejection.
            if 400 <= exc.code < 500:
                raise PermanentHubError(f"hub rejected {mode} with HTTP {exc.code}") from exc
            raise TransientHubError(f"hub returned HTTP {exc.code} for {mode}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TransientHubError(f"hub unreachable for {mode}: {exc}") from exc

        return SubscriptionResult(
            ok=200 <= status < 300,
            lease_seconds=request.lease_seconds if 200 <= status < 300 else 0,
            status_code=status,
            detail=f"hub accepted {mode} (verification pending)",
        )

    def subscribe(self, request: SubscriptionRequest) -> SubscriptionResult:
        return self._post("subscribe", request)

    def renew(self, request: SubscriptionRequest) -> SubscriptionResult:
        # WebSub has no renew verb; a second subscribe with the same topic and
        # callback extends the lease.
        return self._post("subscribe", request)

    def unsubscribe(self, request: SubscriptionRequest) -> SubscriptionResult:
        return self._post("unsubscribe", request)


def topic_url_for(channel_id: str) -> str:
    """The YouTube channel feed the hub watches.

    Built from a validated channel id only; see :mod:`quickreel.triggers.feed`.
    """
    return f"https://www.youtube.com/xml/feeds/videos.xml?channel_id={channel_id}"


def callback_url_for(secret: str = "") -> str:
    """Our callback URL, with the shared secret as a path segment.

    WebSub does not sign notifications by default, so the secret is the only
    thing distinguishing a real notification from anyone who guessed the URL.
    A secret in the path rather than a query string keeps it out of the
    ``Referer`` header on any page that links to it.
    """
    base = get_settings().public_base_url.rstrip("/")
    suffix = f"/{secret}" if secret else ""
    return f"{base}/api/v1/triggers/youtube/notify{suffix}"


def default_hub_client() -> HubClient:
    """The hub client to use, chosen by ``publishing_mode``.

    Reusing that switch keeps one notion of "live versus mock" across the app
    rather than introducing a second one that can disagree with it.
    """
    settings = get_settings()
    if settings.effective_publishing_mode == "mock":
        return MockHubClient()
    return HttpHubClient()
