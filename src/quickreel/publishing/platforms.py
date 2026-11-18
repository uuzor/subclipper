"""Real platform adapters register themselves here.

This module is the single place the scheduler's registry is populated from, so
adding a platform is: write a ``PlatformAdapter`` implementation, import it
here, and call ``register_adapter``. Nothing else in the codebase changes.

No live adapter ships yet — TikTok, YouTube Shorts, and Instagram Reels all
require an approved developer app before their publish endpoints work, so the
adapters land as those approvals do. Until then the registry is empty and
``QUICKREEL_PUBLISHING_MODE=mock`` resolves every platform to the deterministic
mock, which keeps the whole scheduling loop testable.
"""

from __future__ import annotations


def register_all() -> None:
    """Register every real adapter that is importable.

    Each adapter is registered under its ``Platform`` enum value. A missing
    optional dependency must not break the others, so each registration is
    guarded.
    """
    # Example of the shape a real integration takes; uncomment as approvals land.
    #
    # try:
    #     from .tiktok import TikTokAdapter
    #     register_adapter("tiktok", TikTokAdapter)
    # except ImportError:  # optional dependency not installed
    #     pass
    return
