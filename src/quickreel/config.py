"""Application configuration.

All settings are environment-driven and prefixed with ``QUICKREEL_`` so they
never collide with provider SDK variables such as ``GEMINI_API_KEY``.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
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

    # Gemini
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.0-flash"

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

    @field_validator("data_dir")
    @classmethod
    def _resolve_data_dir(cls, value: Path) -> Path:
        return value.expanduser()

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
