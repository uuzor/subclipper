"""Application configuration.

All settings are environment-driven and prefixed with ``QUICKREEL_`` so they
never collide with provider SDK variables such as ``GEMINI_API_KEY``.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="QUICKREEL_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: str = "local"
    data_dir: Path = Path("./data")
    database_url: str = "sqlite+pysqlite:///./data/quickreel.db"
    cors_origins: str = "http://localhost:3000"

    # Built frontend to serve from this same process. Empty disables static
    # serving (pure API mode, e.g. the test suite). Set to the Vite output
    # directory to run the whole app on one origin, which is what the
    # deployment does so no proxy or CORS config is needed.
    frontend_dir: Path | None = None

    # Queue
    redis_url: str = ""
    task_always_eager: bool = True

    # Provider selection
    transcription_provider: str = "auto"
    vision_provider: str = "auto"
    moment_provider: str = "auto"

    # Whisper
    whisper_model: str = "base"
    whisper_device: str = "cpu"
    whisper_compute_type: str = "int8"

    # When a real provider is selected, a runtime failure normally degrades to
    # the deterministic fake so one flaky stage does not sink the whole run. Set
    # this to false to fail the project instead, which is what you want in
    # production where fake output is worse than no output.
    allow_provider_fallback: bool = True

    # Gemini
    gemini_api_key: str = ""
    gemini_model: str = "gemini-flash-lite-latest"

    # OpenAI-compatible LLM
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    # Long-form sources are processed in windows rather than one giant request.
    llm_timeout_seconds: int = 120
    llm_max_retries: int = 3
    # Characters of transcript sent per moment-detection call.
    llm_chunk_chars: int = 12000
    # Overlap between consecutive transcript windows, so a moment that straddles
    # a boundary is still seen whole by at least one call.
    llm_chunk_overlap_seconds: float = 30.0

    # Vision. The whole video is never uploaded; frames are sampled instead.
    vision_timeout_seconds: int = 300
    vision_max_frames: int = 24
    vision_frame_interval_seconds: float = 30.0
    vision_scene_threshold: float = 0.3

    # Rendering
    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"
    render_timeout_seconds: int = 900
    max_upload_mb: int = 2048

    # Remote URL ingestion
    url_ingestion_enabled: bool = True
    # Comma-separated host allowlist. Empty means "any public host" (still
    # subject to the SSRF checks in media/url_source.py).
    allowed_source_hosts: str = ""
    # Cap on downloaded resolution. Clips render to 1080x1920, so pulling 4K
    # source is wasted bandwidth and disk.
    download_max_height: int = 1080
    download_timeout_seconds: int = 1800
    # Netscape-format cookies file, for sites that require a logged-in session.
    downloader_cookies_file: str = ""
    downloader_proxy: str = ""

    # Publishing / scheduling. ``mock`` resolves every platform to the
    # deterministic mock adapter so the scheduling loop runs offline; ``live``
    # requires a registered real adapter and never falls back silently.
    #
    # Empty means "decide from the environment": mock outside production, live
    # inside it. That default matters, because the failure mode of getting it
    # wrong in production is publishing through a stub that reports success
    # while nothing reaches the platform.
    publishing_mode: str = ""
    publish_max_attempts: int = 5
    publish_retry_base_seconds: int = 60
    # Ceiling on backoff, so a throttled post cannot drift days out and
    # effectively disappear.
    publish_max_backoff_seconds: int = 3600

    # Performance feedback loop. Published posts are re-sampled on a cadence and
    # the platform's counters are stored, because the per-niche engagement
    # history is the one asset a competitor cannot copy quickly. Off only for a
    # deployment that must not make outbound calls.
    metrics_enabled: bool = True
    # How often a single post is re-sampled. Counters are cumulative on the
    # platform, so this is about how fast the curve is followed, not correctness.
    metrics_sample_interval_seconds: int = 3600
    # Stop sampling a clip this many days after it was published. The long tail
    # past this point is noise for the learning loop.
    metrics_max_age_days: int = 30

    # OAuth. ``public_base_url`` is the externally reachable origin, used to
    # build redirect URIs that the platform will call back to. It must be the
    # verified domain in production, because the platform rejects a mismatch.
    public_base_url: str = "http://localhost:12000"
    # Signs the OAuth ``state`` parameter. Without it an attacker can hand a
    # victim a callback URL and silently attach the attacker's account to the
    # victim's workspace (login CSRF), so it is required in production.
    oauth_state_secret: str = ""

    # Per-platform OAuth client credentials, issued when an app is created in
    # the platform's developer console. Empty means the connect flow reports
    # "not configured" rather than sending the user to an error page.
    tiktok_client_id: str = ""
    tiktok_client_secret: str = ""
    instagram_reels_client_id: str = ""
    instagram_reels_client_secret: str = ""
    youtube_shorts_client_id: str = ""
    youtube_shorts_client_secret: str = ""

    # YouTube upload events (WebSub / PubSubHubbub). The hub pushes a
    # notification when a subscribed channel publishes, which is the trigger
    # for back-catalogue ingestion.
    youtube_hub_url: str = "https://pubsubhubbub.appspot.com/subscribe"
    # Shared secret appended to the callback URL and echoed by the hub during
    # the verification handshake. WebSub has no HMAC of its own, so this plus a
    # channel allowlist is what authenticates a notification.
    youtube_callback_secret: str = ""
    # Optional: when set, notifications must carry a matching X-Hub-Signature.
    # Only used if the hub actually signs; PubSubHubbub does not by default.
    youtube_signing_secret: str = ""
    # Renew a subscription once it is within this many seconds of expiring.
    # Leases lapse silently otherwise, and the trigger just stops firing.
    youtube_renew_before_seconds: int = 86400
    # Ignore videos shorter than this: there is nothing to clip out of them.
    youtube_min_duration_seconds: int = 90
    # Shorts are already short-form, so clipping them is not the job.
    youtube_skip_shorts: bool = True
    # A new video creates a draft for approval by default. Auto-processing is
    # opt-in, because an unattended pipeline that spends money and publishes
    # without a human is not something to switch on by default.
    youtube_auto_schedule: bool = False

    # Scoring. Weights must sum to 1.0; calibrated against real engagement
    # data later (see docs/architecture.md, "Viral Intelligence").
    scoring_weights: dict[str, float] = Field(
        default_factory=lambda: {
            "emotional_intensity": 0.20,
            "hook_strength": 0.20,
            "storytelling": 0.20,
            "shareability": 0.15,
            "audio_visual_engagement": 0.10,
            "standalone_context": 0.15,
        }
    )

    # Recalibration. A fit needs enough observed clips to beat noise, and a
    # workspace that has not opted in keeps the configured weights.
    scoring_calibration_enabled: bool = True
    scoring_calibration_min_samples: int = 12

    @field_validator("data_dir")
    @classmethod
    def _resolve_data_dir(cls, value: Path) -> Path:
        return value.expanduser()

    @field_validator("publishing_mode", mode="before")
    @classmethod
    def _known_publishing_mode(cls, value: object) -> object:
        """Reject a typo rather than letting it fall through to live mode."""
        if value is None:
            return ""
        text = str(value).strip().lower()
        if text and text not in {"mock", "live"}:
            raise ValueError(f"publishing_mode must be 'mock' or 'live', got {value!r}")
        return text

    @field_validator("database_url", mode="after")
    @classmethod
    def _pin_the_installed_postgres_driver(cls, value: str) -> str:
        """Force the Postgres driver that is actually installed.

        Hosting providers hand out ``postgres://`` (Render, Heroku) or plain
        ``postgresql://``. SQLAlchemy resolves both to psycopg2, which this
        project does not depend on, so an unmodified provider URL fails at the
        first connection with a missing-module error. psycopg 3 is the declared
        dependency, so pin it here rather than asking operators to rewrite the
        string by hand.
        """
        for prefix in ("postgres://", "postgresql://"):
            if value.startswith(prefix):
                return "postgresql+psycopg://" + value[len(prefix) :]
        return value

    @field_validator("frontend_dir", mode="before")
    @classmethod
    def _empty_frontend_dir_is_none(cls, value: object) -> object:
        """An unset env var arrives as "", which is not a valid path."""
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return value

    @property
    def is_production(self) -> bool:
        return self.env.strip().lower() in {"production", "prod"}

    @property
    def auth_require_secure_cookies(self) -> bool:
        """Whether the public origin is HTTPS, so cookies can be marked Secure.

        Derived rather than a separate switch: a deployment that says it is at an
        HTTPS origin should send Secure cookies, and one that is not cannot
        afford to, because the browser would drop them and nobody could log in.
        """
        return self.public_base_url.strip().lower().startswith("https://")

    @property
    def effective_publishing_mode(self) -> str:
        """The mode actually used, resolving the empty default.

        An unset ``publishing_mode`` means mock locally and live in production,
        so forgetting to set it in production fails safe rather than silently
        publishing through a stub.
        """
        mode = self.publishing_mode.strip().lower()
        if mode:
            return mode
        return "live" if self.is_production else "mock"

    @model_validator(mode="after")
    def _refuse_unsafe_production_config(self) -> Settings:
        """Fail at startup rather than at the first bad publish.

        Both of these are silent-wrong-behaviour bugs in production: mock mode
        reports success while nothing reaches the platform, and a missing OAuth
        state secret makes account-linking forgeable. Neither should be
        discoverable by a user noticing their posts never appeared.
        """
        if not self.is_production:
            return self

        problems: list[str] = []
        if self.effective_publishing_mode == "mock":
            problems.append(
                "QUICKREEL_PUBLISHING_MODE=mock is not allowed when QUICKREEL_ENV=production; "
                "publishes would report success without reaching any platform"
            )
        if not self.oauth_state_secret.strip():
            problems.append(
                "QUICKREEL_OAUTH_STATE_SECRET must be set in production; "
                "without it OAuth account-linking is not forgery-resistant"
            )
        if not self.youtube_callback_secret.strip():
            problems.append(
                "QUICKREEL_YOUTUBE_CALLBACK_SECRET must be set in production; "
                "the WebSub callback is public and unsigned without it"
            )
        if not self.auth_require_secure_cookies:
            problems.append(
                "QUICKREEL_PUBLIC_BASE_URL must be an https:// origin in production; "
                "session cookies are marked Secure from it, and over plain HTTP the "
                "session cookie would travel in the clear"
            )
        if problems:
            raise ValueError("unsafe production configuration: " + "; ".join(problems))
        return self

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def renders_dir(self) -> Path:
        return self.data_dir / "renders"

    @property
    def artifacts_dir(self) -> Path:
        return self.data_dir / "artifacts"

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def allowed_source_host_list(self) -> tuple[str, ...]:
        return tuple(part.strip() for part in self.allowed_source_hosts.split(",") if part.strip())

    def ensure_directories(self) -> None:
        for directory in (
            self.data_dir,
            self.uploads_dir,
            self.renders_dir,
            self.artifacts_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_directories()
    return settings
