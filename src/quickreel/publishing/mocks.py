"""Deterministic adapter for tests and offline development.

Mirrors the ``FakeDownloader`` idea: the scheduler and the HTTP API are fully
exercisable without credentials or network access. Failure modes are injectable
so every branch of the retry logic can be tested without a real platform
rejecting anything.
"""

from __future__ import annotations

from .base import (
    AdapterResult,
    AuthError,
    PermanentError,
    PostSpec,
    PostStatus,
    RateLimitError,
    TransientError,
)


class MockAdapter:
    """In-memory adapter that records calls and returns stable ids.

    ``fail_times`` makes the first N publish attempts raise ``failure``, then
    succeed. That is how the retry-then-succeed path is tested. ``always_fail``
    is for the exhaustion path.
    """

    def __init__(
        self,
        name: str = "mock",
        *,
        fail_times: int = 0,
        failure: str = "transient",
        pending: bool = False,
    ) -> None:
        self.name = name
        self.fail_times = fail_times
        self.failure = failure
        self.pending = pending
        self.published: list[PostSpec] = []
        self.status_checks: list[str] = []
        self.metrics_checks: list[str] = []
        self._seq = 0

    def _maybe_fail(self) -> None:
        if self.fail_times <= 0:
            return
        self.fail_times -= 1
        message = f"{self.name}: injected {self.failure} failure"
        if self.failure == "rate_limit":
            raise RateLimitError(message, retry_after_seconds=30.0)
        if self.failure == "permanent":
            raise PermanentError(message)
        if self.failure == "auth":
            raise AuthError(message)
        raise TransientError(message)

    def publish(self, target_credentials: dict, account_id: str, spec: PostSpec) -> AdapterResult:
        self._maybe_fail()
        self._seq += 1
        self.published.append(spec)
        post_id = f"{self.name}-{account_id or 'acct'}-{self._seq}"
        return AdapterResult(
            external_post_id=post_id,
            url=f"https://example.invalid/{self.name}/{post_id}",
            pending=self.pending,
            detail={"mock": True, "caption": spec.caption[:80]},
        )

    def check_status(self, target_credentials: dict, external_post_id: str) -> PostStatus:
        self.status_checks.append(external_post_id)
        return PostStatus(state="published", detail="mock adapter reports success")

    def fetch_metrics(self, target_credentials: dict, external_post_id: str) -> dict:
        self.metrics_checks.append(external_post_id)
        self._maybe_fail()
        # Deterministic, non-zero so a caller that charts metrics sees a shape.
        seed = sum(ord(c) for c in external_post_id) or 1
        return {
            "views": 1000 + seed % 500,
            "likes": 50 + seed % 90,
            "comments": 5 + seed % 20,
            "shares": 2 + seed % 10,
            "average_watch_seconds": 6.0 + (seed % 40) / 10,
        }
