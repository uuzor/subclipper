"""Pydantic request/response schemas for the HTTP API."""

from __future__ import annotations

from datetime import datetime

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    field_validator,
    model_validator,
)


def _none_to_empty(value: object) -> object:
    """Rows written before a JSON column existed read back as NULL.

    ``default=dict`` only applies on insert, so a legacy row hands ``None`` to
    a ``dict`` field and FastAPI raises a ResponseValidationError. Coerce here
    so the API stays readable for old data.
    """
    return {} if value is None else value


def _none_to_blank(value: object) -> object:
    """String counterpart to :func:`_none_to_empty` for legacy NULL text columns."""
    return "" if value is None else value


class MediaAssetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    path: str
    size_bytes: int
    duration_seconds: float
    width: int
    height: int
    fps: float
    has_audio: bool


class TranscriptSegmentOut(BaseModel):
    start: float
    end: float
    text: str
    speaker: str | None = None


class TranscriptOut(BaseModel):
    language: str
    provider: str
    text: str
    segments: list[TranscriptSegmentOut] = Field(default_factory=list)


class ClipOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    project_id: str
    start_seconds: float
    end_seconds: float
    raw_start_seconds: float
    raw_end_seconds: float
    duration_seconds: float = 0.0
    title: str
    hook: str
    transcript_excerpt: str
    moment_type: str
    rationale: str
    score: float
    score_components: dict = Field(default_factory=dict)
    score_breakdown: dict = Field(default_factory=dict)
    rank: int
    status: str
    render_path: str | None = None
    render_meta: dict = Field(default_factory=dict)

    _coerce_components = field_validator("score_components", "score_breakdown", mode="before")(
        _none_to_empty
    )
    _coerce_render_meta = field_validator("render_meta", mode="before")(_none_to_empty)

    @model_validator(mode="after")
    def _fill_duration(self) -> ClipOut:
        """Clip has no duration column; derive it from the chosen boundaries.

        Without this the schema default (0.0) is reported for every clip.
        """
        if self.duration_seconds <= 0:
            self.duration_seconds = max(0.0, self.end_seconds - self.start_seconds)
        return self


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    source_kind: str
    source_url: str | None = None
    # Provenance, surfaced so a creator or agency can see the basis on which
    # this material may be republished.
    license_note: str = ""
    authorized_by: str = ""
    status: str
    error: str | None = None
    providers: dict = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime

    _coerce_providers = field_validator("providers", mode="before")(_none_to_empty)
    _coerce_provenance = field_validator("license_note", "authorized_by", mode="before")(
        _none_to_blank
    )


class ProjectDetailOut(ProjectOut):
    media: MediaAssetOut | None = None
    transcript: TranscriptOut | None = None
    clips: list[ClipOut] = Field(default_factory=list)


class ProjectCreateIn(BaseModel):
    title: str = Field(default="Untitled project", max_length=500)
    source_url: HttpUrl | None = None
    # Provenance for material the creator has rights to. Recording it here means
    # every clip inherits an answer to "on what basis may we republish this?".
    license_note: str = Field(default="", max_length=2000)
    authorized_by: str = Field(default="", max_length=320)


class UploadInitIn(BaseModel):
    """Start a chunked upload.

    Hosting proxies in front of the app cap the request body - Cloudflare, for
    one, rejects anything over 100MiB with a bare HTML 413. Large recordings
    therefore have to arrive in pieces, so the client declares the total size up
    front and the server can reject an oversized file before a single byte is
    transferred.
    """

    filename: str = Field(default="upload.mp4", max_length=255)
    total_bytes: int = Field(gt=0)


class UploadInitOut(BaseModel):
    upload_id: str
    chunk_size: int
    received_bytes: int = 0


class UploadStatusOut(BaseModel):
    upload_id: str
    received_bytes: int
    total_bytes: int
    complete: bool


class UrlIngestIn(BaseModel):
    """Attach a remote video URL to an existing project."""

    source_url: HttpUrl
    title: str | None = Field(default=None, max_length=500)


class RemoteProbeOut(BaseModel):
    """Metadata for a URL, fetched without downloading the media."""

    url: str
    title: str = ""
    uploader: str = ""
    duration_seconds: float = 0.0
    thumbnail: str = ""
    extractor: str = ""
    webpage_url: str = ""
    downloader: str = ""


class RenderRequest(BaseModel):
    clip_ids: list[str] | None = None
    max_clips: int = Field(default=3, ge=1, le=20)
    vertical: bool = True
    burn_captions: bool = True
    # Destination format. When omitted, ``vertical`` picks between the 9:16 and
    # 16:9 exports, which is the behaviour callers had before platforms existed.
    platform: str | None = None


class PublishTargetIn(BaseModel):
    """Connect an account to publish to.

    ``credentials`` is opaque to the API and passed straight to the adapter, so
    a new platform does not require a schema change here.
    """

    platform: str
    display_name: str = Field(default="", max_length=200)
    external_account_id: str = ""
    credentials: dict = Field(default_factory=dict)


class OAuthStartOut(BaseModel):
    """The consent URL for a connect flow, plus the state that must come back.

    ``state`` is returned so a test or a popup-based client can assert the
    round trip without reading server logs.
    """

    platform: str
    authorize_url: str
    state: str


class PublishTargetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    platform: str
    display_name: str
    external_account_id: str
    status: str
    status_detail: str = ""
    created_at: datetime
    updated_at: datetime

    _coerce_detail = field_validator("status_detail", mode="before")(_none_to_blank)


class ScheduledPostIn(BaseModel):
    clip_id: str
    target_id: str
    scheduled_for: datetime
    caption: str = ""
    options: dict = Field(default_factory=dict)


class ScheduledPostOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    clip_id: str
    target_id: str
    scheduled_for: datetime
    caption: str
    options: dict = Field(default_factory=dict)
    status: str
    external_post_id: str
    error: str | None = None
    attempts: int
    next_attempt_at: datetime | None = None
    published_at: datetime | None = None
    created_at: datetime

    _coerce_options = field_validator("options", mode="before")(_none_to_empty)


class PublishAttemptOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    attempt_number: int
    ok: bool
    error: str
    detail: dict = Field(default_factory=dict)
    created_at: datetime

    _coerce_detail = field_validator("detail", mode="before")(_none_to_empty)


class PublishRunOut(BaseModel):
    """Summary of one scheduler pass."""

    examined: int
    published: int
    failed: int
    skipped: int
    outcomes: list[dict] = Field(default_factory=list)


class ClipMetricOut(BaseModel):
    """One performance sample for a published clip."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    post_id: str
    platform: str
    views: int
    likes: int
    comments: int
    shares: int
    average_watch_seconds: float
    captured_at: datetime


class ClipPerformanceOut(BaseModel):
    """Aggregated engagement for a clip, the shape the learning loop reads."""

    clip_id: str
    sample_count: int
    platforms: dict = Field(default_factory=dict)
    totals: dict = Field(default_factory=dict)


class MetricsRunOut(BaseModel):
    """Summary of one metrics-collection pass."""

    examined: int
    sampled: int
    failed: int
    skipped: int
    outcomes: list[dict] = Field(default_factory=list)


class CalibrationOut(BaseModel):
    """One proposed recalibration of the scoring weights.

    ``applied`` and the ``diagnostics.applicable`` flag are distinct on purpose:
    applicable means the fit beat the current weights, applied means someone
    installed it. A proposal can be applicable and unapplied.
    """

    model_config = ConfigDict(from_attributes=True)

    id: str
    proposed_weights: dict = Field(default_factory=dict)
    baseline_weights: dict = Field(default_factory=dict)
    sample_size: int
    platform_counts: dict = Field(default_factory=dict)
    diagnostics: dict = Field(default_factory=dict)
    applied: bool
    applied_at: datetime | None = None
    created_at: datetime


class CalibrationApplyOut(BaseModel):
    """Result of installing a proposal."""

    calibration: CalibrationOut
    active_weights: dict = Field(default_factory=dict)


class ChannelSubscriptionIn(BaseModel):
    """Subscribe to a channel's uploads.

    ``authorized_by`` and ``license_note`` are required in spirit: the whole
    point of the subscription is to record who authorised processing this
    channel's videos. They default to empty so a test or a quick local setup is
    not blocked, but the UI prompts for them.
    """

    channel_id: str = Field(min_length=1, max_length=64)
    channel_title: str = Field(default="", max_length=300)
    authorized_by: str = Field(default="", max_length=320)
    license_note: str = ""
    auto_process: bool | None = None

    @field_validator("channel_id")
    @classmethod
    def _valid_channel_id(cls, value: str) -> str:
        import re

        text = value.strip()
        if not re.match(r"^UC[A-Za-z0-9_-]{22}$", text):
            raise ValueError(
                "channel_id must be a YouTube channel id (starts with 'UC' followed by "
                "22 characters)"
            )
        return text


class ChannelSubscriptionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    platform: str
    channel_id: str
    channel_title: str
    authorized_by: str
    license_note: str
    status: str
    status_detail: str
    auto_process: bool
    lease_expires_at: datetime | None = None
    last_verified_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    _coerce_text = field_validator(
        "channel_title", "authorized_by", "license_note", "status_detail", mode="before"
    )(_none_to_blank)


class YouTubeEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    channel_id: str
    video_id: str
    video_title: str
    video_url: str
    status: str
    reason: str
    project_id: str | None = None
    received_at: datetime
    processed_at: datetime | None = None

    _coerce_text = field_validator("video_title", "video_url", "reason", mode="before")(
        _none_to_blank
    )


class SubscriptionRenewOut(BaseModel):
    """Result of one renewal sweep."""

    renewed: int
    channel_ids: list[str] = Field(default_factory=list)


# ---- Authentication and tenancy ----


class SignupIn(BaseModel):
    """Create an account. An empty ``workspace_name`` gets a sensible default."""

    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=8, max_length=1024)
    display_name: str = Field(default="", max_length=200)
    workspace_name: str = Field(default="", max_length=200)


class LoginIn(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=1024)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    email: str
    display_name: str = ""
    created_at: datetime

    _coerce_name = field_validator("display_name", mode="before")(_none_to_blank)


class WorkspaceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    created_at: datetime

    _coerce_name = field_validator("name", mode="before")(_none_to_blank)


class WorkspaceSwitchIn(BaseModel):
    workspace_id: str = Field(min_length=1, max_length=32)


class PasswordChangeIn(BaseModel):
    """Change the caller's password. The current one is required even though the
    caller is already authenticated, so a stolen session cannot be escalated into
    a stolen account."""

    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=8, max_length=1024)


class SessionOut(BaseModel):
    """The caller's identity, their active workspace, and the ones they can
    switch to. Returned by signup, login and ``/me`` so the frontend has one
    shape to handle."""

    user: UserOut
    workspace: WorkspaceOut
    role: str
    workspaces: list[WorkspaceOut] = Field(default_factory=list)
