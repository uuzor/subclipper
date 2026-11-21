"""Publishing: adapter contracts, the registry, and the post scheduler.

The design is deliberately split so that adding a platform is additive:

- :mod:`quickreel.publishing.base` defines the ``PlatformAdapter`` contract and
  the result types. Nothing in the scheduler knows a platform's name.
- :mod:`quickreel.publishing.registry` selects an adapter for a target, the same
  way :mod:`quickreel.ai.registry` selects an AI provider.
- :mod:`quickreel.publishing.mocks` provides a deterministic adapter so the
  whole scheduling loop is testable without network access or credentials.
- :mod:`quickreel.publishing.scheduler` claims due posts and drives retries.
- :mod:`quickreel.publishing.metrics` samples engagement counters after
  publication. That history is the feedback loop the scoring model is
  recalibrated against, and the one asset a competitor cannot copy quickly.

A real platform integration is a new module implementing ``PlatformAdapter``
plus one registry entry. It does not touch the scheduler, the models, or the
HTTP API.
"""

from __future__ import annotations

from .base import (
    AdapterError,
    AdapterResult,
    AuthError,
    PermanentError,
    PlatformAdapter,
    PostSpec,
    RateLimitError,
    TransientError,
)
from .registry import (
    available_adapters,
    get_adapter,
    register_adapter,
    reset_adapters,
)

__all__ = [
    "AdapterError",
    "AdapterResult",
    "AuthError",
    "PermanentError",
    "PlatformAdapter",
    "PostSpec",
    "RateLimitError",
    "TransientError",
    "available_adapters",
    "get_adapter",
    "register_adapter",
    "reset_adapters",
]
