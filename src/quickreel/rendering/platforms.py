"""Per-platform export targets.

One render cannot serve every destination. The aspect ratio differs, and so does
the area of the frame each platform covers with its own UI: a caption drawn at
the very bottom of a 9:16 frame is hidden behind TikTok's description and
Instagram's action rail, which is why captions here sit above a per-platform
safe margin instead of at the default baseline.

The numbers below are starting points, not guarantees. Platforms revise limits
without notice, so they are data rather than logic: update this table and every
export follows.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class PlatformSpec:
    """Everything that differs between one destination and another."""

    key: str
    label: str
    width: int
    height: int
    # Longest clip the platform accepts. Clips longer than this are refused
    # rather than silently truncated, because a clip cut mid-sentence is worse
    # than an error the caller can act on.
    max_seconds: float
    # Fraction of the frame height covered by the platform's own chrome at the
    # bottom (caption text, action rail, progress bar). Captions are lifted above
    # it, as a fraction of the frame height.
    bottom_safe_ratio: float = 0.0
    # Extra bits per second for video. Vertical feeds are watched on phones and
    # re-encoded by the platform, so a high bitrate buys little.
    video_bitrate: str = "6M"
    audio_bitrate: str = "192k"
    # Some platforms cap file size; 0 means no local cap.
    max_bytes: int = 0
    notes: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def aspect(self) -> str:
        return f"{self.width}x{self.height}"

    @property
    def bottom_safe_pixels(self) -> int:
        return int(self.height * self.bottom_safe_ratio)


# Bottom safe ratios reflect each app's overlay on a 1920px-tall frame.
SPECS: dict[str, PlatformSpec] = {
    "tiktok": PlatformSpec(
        key="tiktok",
        label="TikTok",
        width=1080,
        height=1920,
        max_seconds=600.0,
        # TikTok's caption block, action rail and progress bar occupy roughly the
        # bottom fifth of the frame.
        bottom_safe_ratio=0.20,
        notes="9:16; captions lifted above the description and action rail.",
    ),
    "youtube_shorts": PlatformSpec(
        key="youtube_shorts",
        label="YouTube Shorts",
        width=1080,
        height=1920,
        max_seconds=180.0,
        # Shorts shows title and channel plus a progress bar at the bottom.
        bottom_safe_ratio=0.15,
        notes="9:16; three-minute ceiling for Shorts eligibility.",
    ),
    "instagram_reels": PlatformSpec(
        key="instagram_reels",
        label="Instagram Reels",
        width=1080,
        height=1920,
        max_seconds=180.0,
        # Reels overlays caption, audio attribution and the action rail.
        bottom_safe_ratio=0.22,
        notes="9:16; heaviest bottom overlay of the vertical feeds.",
    ),
    "youtube": PlatformSpec(
        key="youtube",
        label="YouTube (landscape)",
        width=1920,
        height=1080,
        max_seconds=43200.0,
        video_bitrate="8M",
        notes="16:9 long-form upload.",
    ),
    "square": PlatformSpec(
        key="square",
        label="Square (LinkedIn, X)",
        width=1080,
        height=1080,
        max_seconds=600.0,
        bottom_safe_ratio=0.10,
        notes="1:1 feed video.",
    ),
    "portrait": PlatformSpec(
        key="portrait",
        label="Portrait 4:5 (LinkedIn, Instagram feed)",
        width=1080,
        height=1350,
        max_seconds=600.0,
        bottom_safe_ratio=0.12,
        notes="4:5 feed video; takes more vertical space than 1:1.",
    ),
}

# The format the app has always produced, kept as the default so existing
# callers keep working unchanged.
DEFAULT_PLATFORM = "tiktok"


class UnknownPlatform(ValueError):
    """Raised for a platform key that has no spec."""


def get_spec(key: str) -> PlatformSpec:
    try:
        return SPECS[key]
    except KeyError:
        raise UnknownPlatform(
            f"unknown platform {key!r}; known: {', '.join(sorted(SPECS))}"
        ) from None


def all_specs() -> list[PlatformSpec]:
    return [SPECS[key] for key in sorted(SPECS)]
