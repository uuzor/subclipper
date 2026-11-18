"""Adapter registry.

Resolves a platform name to a concrete adapter. Registration is explicit so a
real platform integration is one call in :mod:`quickreel.publishing.platforms`
rather than a change to the scheduler.

The registry is also the seam that keeps the test suite offline: with
``QUICKREEL_PUBLISHING_MODE=mock`` every platform resolves to the deterministic
mock adapter, so the scheduler can be exercised end to end without credentials
or network access. ``live`` mode resolves the real adapter and never falls back
silently — a publish that quietly went to a mock in production would be worse
than a loud failure.
"""

from __future__ import annotations

from collections.abc import Callable

from ..config import get_settings
from ..logging_setup import get_logger
from .base import PlatformAdapter
from .mocks import MockAdapter

logger = get_logger(__name__)

_AdapterFactory = Callable[[], PlatformAdapter]

# Populated by register_adapter. Keys must match the Platform enum values.
_FACTORIES: dict[str, _AdapterFactory] = {}


def register_adapter(name: str, factory: _AdapterFactory) -> None:
    """Register the factory that builds the adapter for ``name``."""
    _FACTORIES[name] = factory


def reset_adapters() -> None:
    """Clear registered adapters. Used by tests that register their own."""
    _FACTORIES.clear()


def available_adapters() -> list[str]:
    """Names with a real adapter registered, sorted for stable output."""
    return sorted(_FACTORIES)


def _mock_for(platform: str) -> PlatformAdapter:
    return MockAdapter(name=platform)


def get_adapter(platform: str) -> PlatformAdapter:
    """Return the adapter to use for ``platform``.

    Raises ``KeyError`` when live mode is requested for a platform with no real
    adapter, because that is a configuration error the operator must see.
    """
    settings = get_settings()

    if settings.effective_publishing_mode == "mock":
        return _mock_for(platform)

    factory = _FACTORIES.get(platform)
    if factory is None:
        raise KeyError(
            f"no live adapter registered for platform {platform!r}; "
            f"registered: {available_adapters() or 'none'}"
        )
    return factory()
