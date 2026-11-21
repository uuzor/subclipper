"""Application configuration.

All settings are environment-driven and prefixed with ``QUICKREEL_`` so they
never collide with provider SDK variables such as ``GEMINI_API_KEY``.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, field_validator, model_validator
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
    # Render off-request in a thread when no worker exists. Long renders outlive
    # hosting proxies' request timeouts, and the proxy answers 502 while ffmpeg
    # keeps running, so the caller sees failure for work that succeeds. Off by
    # default so a local run and the test suite keep the synchronous contract
    # where the response body is the finished clip.
    render_in_background: bool = False
    # Same reasoning for the analysis pipeline, which is slower still: a
    # transcript plus moment detection on a long video dwarfs any request
    # timeout. Off by default for the same reason as render_in_background.
    process_in_background: bool = False

    # Render the top moments as soon as analysis finishes. Without this a
    # finished project is a list of candidate rows with no playable video, which
    # reads as a broken product rather than an unrendered one.
    auto_render_after_process: bool = True
    auto_render_max_clips: int = 3
    # Empty means the default platform (9:16 vertical).
    auto_render_platform: str = ""

    # Object storage for source videos and rendered clips. ``local`` keeps
    # everything under ``data_dir``, which does not survive a container
    # restart on hosts with an ephemeral filesystem: the database row says
    # "rendered" while the MP4 is gone. ``s3`` keeps the bytes outside the
    # container so a restart costs nothing.
    storage_backend: str = "local"
    # A provisioner writes these as plain ``S3_*``, which is also the name every
    # S3 client expects, so both spellings are accepted and re-provisioning does
    # not quietly stop configuring the app.
    s3_bucket: str = Field("", validation_alias=AliasChoices("QUICKREEL_S3_BUCKET", "S3_BUCKET"))
    s3_endpoint_url: str = Field(
        "", validation_alias=AliasChoices("QUICKREEL_S3_ENDPOINT_URL", "S3_ENDPOINT")
    )
    s3_region: str = Field(
        "us-east-1", validation_alias=AliasChoices("QUICKREEL_S3_REGION", "S3_REGION")
    )
    s3_access_key_id: str = Field(
        "", validation_alias=AliasChoices("QUICKREEL_S3_ACCESS_KEY_ID", "S3_ACCESS_KEY_ID")
    )
    s3_secret_access_key: str = Field(
        "",
        validation_alias=AliasChoices("QUICKREEL_S3_SECRET_ACCESS_KEY", "S3_SECRET_ACCESS_KEY"),
    )
    # Presigned playback URLs are handed to a <video> tag, so they have to
    # outlive a viewing session without being long-lived credentials.
    s3_presign_seconds: int = 3600

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

    # NVIDIA NIM (build.nvidia.com). One OpenAI-compatible endpoint covers all
    # three AI stages, so a deployment can run without a local Whisper model.
    # The omni model takes audio directly, the VL model takes sampled frames,
    # and a text model does moment selection.
    nvidia_api_key: str = ""
    nvidia_base_url: str = "https://integrate.api.nvidia.com/v1"
    nvidia_asr_model: str = "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning"
    nvidia_vision_model: str = "nvidia/nemotron-3.5-super-vl-120b-a12b"
    nvidia_moment_model: str = "moonshotai/kimi-k3.1-pro"
    nvidia_timeout_seconds: int = 180
    # Audio is uploaded in chunks so a 2-hour source does not become one
    # multi-hundred-megabyte request. Chunk length bounds each upload.
    nvidia_asr_chunk_seconds: float = 120.0
    nvidia_asr_max_chunks: int = 240

    # Smallest AI Pulse speech-to-text. Purpose-built ASR with real word
    # timestamps, which is the one thing the general multimodal models cannot
    # give us: asked for timestamps they estimate them from word counts.
    smallest_api_key: str = ""
    smallest_base_url: str = "https://api.smallest.ai"
    # pulse-pro is English-tuned and pre-recorded only; pulse is multilingual.
    smallest_stt_model: str = "pulse-pro"
    smallest_stt_language: str = "en"
    smallest_timeout_seconds: int = 300

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
    # Cap on encoder threads. x264 sizes its frame pools from the reported core
    # count, and a container on a many-core host inherits that number even when
    # its memory limit is far smaller - 16 threads needs well over 512MB and the
    # OOM killer takes the whole service down mid-render.
    render_threads: int = 2
    # x264 preset for clip exports. ``ultrafast`` peaks around 273MB at 1080x1920
    # against 470MB for ``veryfast``; on a 512MB instance the difference is a
    # finished clip versus a restart that also destroys the uploaded source.
    # Hosts with real memory can raise this to ``veryfast`` for smaller files.
    render_preset: str = "ultrafast"
    # Frames of lookahead x264 buffers per thread. The default 40 is a large
    # slice of a small instance's memory for a negligible quality gain on a
    # short vertical clip.
    render_lookahead: int = 10
    # Longest side of a rendered clip. The default keeps platform-native sizes;
    # an instance short on memory can set 1280 to render 720x1280, which measured
    # 314MB against 470MB at 1080x1920. Scaling happens at encode time, so the
    # export keeps the platform's aspect ratio either way.
    render_max_dimension: int = 1920
    # How many clips may render at once across the whole process. Each ffmpeg
    # peaks around 240MB at 720x1280, so two on a 512MB instance (518MB measured)
    # trip the OOM killer and take the service down mid-render - which the edge
    # proxy reports as a 503. A batch of clips queues on this instead of racing;
    # a host with real memory can raise it. Renders that cannot start within
    # ``render_queue_timeout_seconds`` fail with a clear message rather than
    # sitting in ``rendering`` forever.
    render_max_concurrent: int = 1
    render_queue_timeout_seconds: int = 900
    max_upload_mb: int = 2048
    # Chunk size advertised to clients for resumable uploads. Cloudflare rejects
    # request bodies over 100MiB, so chunks must stay comfortably below that or
    # large uploads die at the proxy with an HTML 413 the app never sees.
    upload_chunk_mb: int = 8

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
    # The same jar as text, for hosts where you can only set env vars. Written
    # to an owner-only temp file when a download runs.
    downloader_cookies_text: str = ""
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

        Hosting providers hand out ``postgres://`` (Render, Heroku, Neon) or
        plain ``postgresql://``. SQLAlchemy resolves both to psycopg2, which this
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
        # A container's filesystem is wiped on every deploy, so local storage in
        # production means clips that render and then disappear. Caught here
        # rather than at the first render, where it looks like a rendering bug.
        if self.storage_backend.strip().lower() == "s3" and not self.s3_bucket.strip():
            problems.append(
                "QUICKREEL_STORAGE_BACKEND=s3 requires QUICKREEL_S3_BUCKET; without a "
                "bucket, renders would only exist on the container's ephemeral disk"
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
    def upload_chunk_bytes(self) -> int:
        return max(self.upload_chunk_mb, 1) * 1024 * 1024

    @property
    def upload_parts_dir(self) -> Path:
        return self.data_dir / "upload-parts"

    @property
    def allowed_source_host_list(self) -> tuple[str, ...]:
        return tuple(part.strip() for part in self.allowed_source_hosts.split(",") if part.strip())

    def ensure_directories(self) -> None:
        for directory in (
            self.data_dir,
            self.uploads_dir,
            self.renders_dir,
            self.artifacts_dir,
            self.upload_parts_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_directories()
    return settings
