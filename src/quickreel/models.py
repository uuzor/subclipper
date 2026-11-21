"""SQLAlchemy ORM models for projects, transcripts, moments and clips."""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from .db import Base


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator):
    """A timestamp that is always UTC and always timezone-aware.

    SQLite discards tzinfo on write and returns naive datetimes, so comparing a
    loaded value against ``datetime.now(UTC)`` raises TypeError. Postgres with
    ``timezone=True`` returns aware values, so the two backends disagree. This
    normalises both directions: aware UTC in, aware UTC out. Naive values are
    assumed to be UTC, which matches how every writer in this codebase stores
    them.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class ProjectStatus(enum.StrEnum):
    created = "created"
    ingesting = "ingesting"
    ingesting_failed = "ingesting_failed"
    transcribing = "transcribing"
    transcribe_failed = "transcribe_failed"
    analyzing = "analyzing"
    analyze_failed = "analyze_failed"
    ready = "ready"


class ClipStatus(enum.StrEnum):
    candidate = "candidate"
    rendering = "rendering"
    rendered = "rendered"
    render_failed = "render_failed"


class Platform(enum.StrEnum):
    tiktok = "tiktok"
    youtube_shorts = "youtube_shorts"
    instagram_reels = "instagram_reels"


class PublishTargetStatus(enum.StrEnum):
    active = "active"
    needs_reauth = "needs_reauth"
    disabled = "disabled"


class ScheduledPostStatus(enum.StrEnum):
    scheduled = "scheduled"
    publishing = "publishing"
    published = "published"
    failed = "failed"
    canceled = "canceled"


class SourceKind(enum.StrEnum):
    """How a project's media was obtained. Recorded so provenance is auditable.

    Only ``upload`` and ``authorized_url`` are ingest paths the product stands
    behind; the enum exists so the value is never a free-form string that drifts.
    """

    upload = "upload"
    authorized_url = "authorized_url"
    licensed_source = "licensed_source"
    channel_subscription = "channel_subscription"


class SubscriptionStatus(enum.StrEnum):
    pending = "pending"
    active = "active"
    expired = "expired"
    revoked = "revoked"


class YouTubeEventStatus(enum.StrEnum):
    """What we did with one channel notification.

    Recorded so a video that was skipped is auditable: "why was this not
    clipped?" is answered by a row, not by guessing from logs.
    """

    received = "received"
    ignored = "ignored"
    queued = "queued"
    processed = "processed"
    failed = "failed"


class WorkspaceRole(enum.StrEnum):
    """A member's rights inside one workspace.

    ``owner`` may manage members and delete the workspace; ``member`` may work
    with the workspace's projects, clips and connected accounts but not change
    who else has access.
    """

    owner = "owner"
    member = "member"


class User(Base):
    """An authenticated person.

    The account is deliberately separate from the workspace. Signing up creates
    both, but a user can belong to more than one workspace, which is what makes
    an agency able to work inside a client's tenancy without sharing a login.
    """

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    # Stored already normalised (lowercased, trimmed) by quickreel.auth, and
    # unique so two accounts cannot share an address.
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(Text)
    display_name: Mapped[str] = mapped_column(String(200), default="")
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now, onupdate=_now)

    memberships: Mapped[list[Membership]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    sessions: Mapped[list[UserSession]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class Workspace(Base):
    """A tenant. Every piece of content belongs to exactly one.

    This is the isolation boundary: projects, clips, connected publishing
    accounts and watched channels are all scoped by ``workspace_id``, and every
    query filters on the caller's workspace. Nothing is shared implicitly.
    """

    __tablename__ = "workspaces"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200), default="My workspace")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now, onupdate=_now)

    memberships: Mapped[list[Membership]] = relationship(
        back_populates="workspace", cascade="all, delete-orphan"
    )


class Membership(Base):
    """Which users belong to which workspace, and with what role."""

    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("user_id", "workspace_id", name="uq_membership"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[WorkspaceRole] = mapped_column(
        Enum(WorkspaceRole, native_enum=False), default=WorkspaceRole.owner
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)

    user: Mapped[User] = relationship(back_populates="memberships")
    workspace: Mapped[Workspace] = relationship(back_populates="memberships")


class UserSession(Base):
    """A logged-in browser session.

    Sessions are rows, not signed tokens, so that logging out and revoking access
    actually take effect. ``token_hash`` stores a SHA-256 of the cookie value, so
    a leaked database does not yield usable session tokens.
    """

    __tablename__ = "user_sessions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # The workspace the session is currently acting in. A user in several
    # workspaces switches this rather than holding several sessions.
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    user_agent: Mapped[str] = mapped_column(String(400), default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)

    user: Mapped[User] = relationship(back_populates="sessions")


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    # The tenant that owns this project. Every read and write filters on it.
    # Nullable only so the additive-column migration can backfill rows that
    # predate tenancy; application code always sets it.
    workspace_id: Mapped[str | None] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=True, index=True
    )
    title: Mapped[str] = mapped_column(String(500), default="Untitled project")
    source_kind: Mapped[str] = mapped_column(String(32), default="upload")
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Platform-native id of the source media, when the source is remote. Set for
    # channel-subscription ingestion so a repeated notification maps to the same
    # project instead of creating a duplicate.
    external_video_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    # Provenance. "Who uploaded this and on what basis may we republish it?"
    # Recorded on every project so a clip can always answer that question.
    license_note: Mapped[str] = mapped_column(Text, default="")
    authorized_by: Mapped[str] = mapped_column(String(320), default="")
    status: Mapped[ProjectStatus] = mapped_column(
        Enum(ProjectStatus, native_enum=False), default=ProjectStatus.created
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Which provider actually served each stage. Empty until the pipeline runs.
    # Recorded so a silently degraded run is auditable after the fact.
    # Nullable because the column was added to existing databases by the
    # additive migration, which can only create it without a default, so legacy
    # rows hold NULL. The API's ``_none_to_empty`` validators read that as ``{}``.
    providers: Mapped[dict | None] = mapped_column(JSON, default=dict, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now, onupdate=_now)

    media: Mapped[MediaAsset | None] = relationship(
        back_populates="project", uselist=False, cascade="all, delete-orphan"
    )
    transcript: Mapped[Transcript | None] = relationship(
        back_populates="project", uselist=False, cascade="all, delete-orphan"
    )
    clips: Mapped[list[Clip]] = relationship(back_populates="project", cascade="all, delete-orphan")


class MediaAsset(Base):
    __tablename__ = "media_assets"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), unique=True
    )
    path: Mapped[str] = mapped_column(Text)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    duration_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    width: Mapped[int] = mapped_column(Integer, default=0)
    height: Mapped[int] = mapped_column(Integer, default=0)
    fps: Mapped[float] = mapped_column(Float, default=0.0)
    has_audio: Mapped[bool] = mapped_column(default=True)
    probe: Mapped[dict] = mapped_column(JSON, default=dict)
    # Object-storage key for the source video, set when the upload was backed up.
    # Null on a local backend, and on rows written before backups existed.
    source_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)

    project: Mapped[Project] = relationship(back_populates="media")


class Transcript(Base):
    __tablename__ = "transcripts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), unique=True
    )
    language: Mapped[str] = mapped_column(String(16), default="en")
    text: Mapped[str] = mapped_column(Text, default="")
    provider: Mapped[str] = mapped_column(String(64), default="unknown")
    segments: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)

    project: Mapped[Project] = relationship(back_populates="transcript")


class Clip(Base):
    """A candidate or rendered viral moment."""

    __tablename__ = "clips"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )

    # Optimized boundaries (already snapped to sentence / silence edges).
    start_seconds: Mapped[float] = mapped_column(Float)
    end_seconds: Mapped[float] = mapped_column(Float)
    # Boundaries as proposed by the moment detector, kept for traceability.
    raw_start_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    raw_end_seconds: Mapped[float] = mapped_column(Float, default=0.0)

    title: Mapped[str] = mapped_column(String(300), default="")
    hook: Mapped[str] = mapped_column(Text, default="")
    transcript_excerpt: Mapped[str] = mapped_column(Text, default="")
    moment_type: Mapped[str] = mapped_column(String(64), default="insight")
    rationale: Mapped[str] = mapped_column(Text, default="")

    score: Mapped[float] = mapped_column(Float, default=0.0)
    score_components: Mapped[dict] = mapped_column(JSON, default=dict)
    score_breakdown: Mapped[dict] = mapped_column(JSON, default=dict)

    rank: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[ClipStatus] = mapped_column(
        Enum(ClipStatus, native_enum=False), default=ClipStatus.candidate
    )
    render_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    render_meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)

    project: Mapped[Project] = relationship(back_populates="clips")


class PublishTarget(Base):
    """A connected account on a platform, with the credentials to post to it.

    ``credentials`` holds the OAuth material the adapter needs. It is stored
    as-is in the database today; a production deployment should encrypt it at
    rest (see the note in docs/architecture.md). ``external_account_id`` is the
    platform's own identifier for the account, which is what adapters address.
    """

    __tablename__ = "publish_targets"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    # Connected accounts belong to a tenant, so one workspace's TikTok account is
    # never visible to, or usable by, another.
    workspace_id: Mapped[str | None] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=True, index=True
    )
    platform: Mapped[Platform] = mapped_column(Enum(Platform, native_enum=False))
    display_name: Mapped[str] = mapped_column(String(200), default="")
    external_account_id: Mapped[str] = mapped_column(String(200), default="")
    credentials: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[PublishTargetStatus] = mapped_column(
        Enum(PublishTargetStatus, native_enum=False), default=PublishTargetStatus.active
    )
    # Set when the platform rejects the credentials, so the UI can prompt a
    # reconnect instead of silently failing every future post.
    status_detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now, onupdate=_now)

    posts: Mapped[list[ScheduledPost]] = relationship(
        back_populates="target", cascade="all, delete-orphan"
    )


class ScheduledPost(Base):
    """A rendered clip queued for publication to one target at one time."""

    __tablename__ = "scheduled_posts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    clip_id: Mapped[str] = mapped_column(ForeignKey("clips.id", ondelete="CASCADE"), index=True)
    target_id: Mapped[str] = mapped_column(
        ForeignKey("publish_targets.id", ondelete="CASCADE"), index=True
    )

    scheduled_for: Mapped[datetime] = mapped_column(UTCDateTime, index=True, default=_now)
    caption: Mapped[str] = mapped_column(Text, default="")
    # Per-platform overrides (privacy level, hashtags, cover frame, ...).
    options: Mapped[dict] = mapped_column(JSON, default=dict)

    status: Mapped[ScheduledPostStatus] = mapped_column(
        Enum(ScheduledPostStatus, native_enum=False),
        default=ScheduledPostStatus.scheduled,
        index=True,
    )
    # Platform-side id of the published post, used for status polling and for
    # later pulling performance metrics.
    external_post_id: Mapped[str] = mapped_column(String(200), default="")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Retry bookkeeping. ``attempts`` counts every adapter call; ``next_attempt_at``
    # is when the scheduler may try again, which is how backoff is expressed
    # without a separate queue.
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True, index=True)

    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now, onupdate=_now)

    clip: Mapped[Clip] = relationship()
    target: Mapped[PublishTarget] = relationship(back_populates="posts")
    attempts_log: Mapped[list[PublishAttempt]] = relationship(
        back_populates="post", cascade="all, delete-orphan"
    )


class PublishAttempt(Base):
    """One adapter invocation for a scheduled post.

    Kept separately from the post so the full retry history survives: "it
    worked on the fourth try" is exactly the information a support question
    needs, and overwriting a single error string loses it.
    """

    __tablename__ = "publish_attempts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    post_id: Mapped[str] = mapped_column(
        ForeignKey("scheduled_posts.id", ondelete="CASCADE"), index=True
    )
    attempt_number: Mapped[int] = mapped_column(Integer, default=1)
    ok: Mapped[bool] = mapped_column(default=False)
    error: Mapped[str] = mapped_column(Text, default="")
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)

    post: Mapped[ScheduledPost] = relationship(back_populates="attempts_log")


class ScoringCalibration(Base):
    """One proposed recalibration of the viral scoring weights.

    The model is a prediction, and the point of the feedback loop is to replace
    the initial guesses with weights fitted to what actually happened. A fit is
    stored rather than applied so the change is reviewable: the proposal records
    the weights it would install, the sample it was fitted on and the
    diagnostics that justify it, and ``applied_at`` is set only when a human (or
    an explicit API call) promotes it. Nothing about scoring changes silently.
    """

    __tablename__ = "scoring_calibrations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    # Calibration is per tenant: one workspace's audience is not another's, and
    # a shared weight vector would blend two different signals.
    workspace_id: Mapped[str | None] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=True, index=True
    )
    # Weights the fit proposes, and the weights that were live when it ran.
    proposed_weights: Mapped[dict] = mapped_column(JSON, default=dict)
    baseline_weights: Mapped[dict] = mapped_column(JSON, default=dict)
    # How many clips with both components and outcomes the fit used, plus the
    # per-platform counts so a lopsided sample is visible.
    sample_size: Mapped[int] = mapped_column(Integer, default=0)
    platform_counts: Mapped[dict] = mapped_column(JSON, default=dict)
    diagnostics: Mapped[dict] = mapped_column(JSON, default=dict)
    # A fit that cannot beat the prior is still recorded, with this set, so the
    # UI can say "not enough signal yet" instead of showing a bogus proposal.
    applied: Mapped[bool] = mapped_column(default=False)
    applied_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)


class ClipMetric(Base):
    """A performance sample for a published clip.

    The scoring model is meant to be recalibrated against real engagement, so
    samples are recorded from the first publish rather than added later when
    there is already no history to learn from.
    """

    __tablename__ = "clip_metrics"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    post_id: Mapped[str] = mapped_column(
        ForeignKey("scheduled_posts.id", ondelete="CASCADE"), index=True
    )
    platform: Mapped[Platform] = mapped_column(Enum(Platform, native_enum=False))
    views: Mapped[int] = mapped_column(Integer, default=0)
    likes: Mapped[int] = mapped_column(Integer, default=0)
    comments: Mapped[int] = mapped_column(Integer, default=0)
    shares: Mapped[int] = mapped_column(Integer, default=0)
    # Retention is the signal that matters most for short-form and is not
    # derivable from the counts above.
    average_watch_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    captured_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)

    post: Mapped[ScheduledPost] = relationship()


class ChannelSubscription(Base):
    """A channel whose new uploads should be ingested automatically.

    Backed by a WebSub (PubSubHubbub) subscription at the hub. The hub is what
    actually watches the channel; we only receive and verify its notifications.

    ``lease_expires_at`` exists because WebSub leases lapse silently. Without a
    renewal sweep the trigger simply stops firing one day, with no error, so the
    expiry is stored and a renewal task acts on it.
    """

    __tablename__ = "channel_subscriptions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    # The tenant that watches this channel, and therefore owns every project the
    # subscription produces.
    workspace_id: Mapped[str | None] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=True, index=True
    )
    platform: Mapped[Platform] = mapped_column(
        Enum(Platform, native_enum=False), default=Platform.youtube_shorts
    )
    # The platform's channel id (YouTube's UC... id), which is what the hub's
    # feed URL is built from and what a notification identifies.
    channel_id: Mapped[str] = mapped_column(String(64), index=True)
    channel_title: Mapped[str] = mapped_column(String(300), default="")
    # The owner of the channel, recorded so ingestion has an auditable basis for
    # processing the channel's uploads rather than relying on the subscription
    # existing.
    authorized_by: Mapped[str] = mapped_column(String(320), default="")
    license_note: Mapped[str] = mapped_column(Text, default="")

    status: Mapped[SubscriptionStatus] = mapped_column(
        Enum(SubscriptionStatus, native_enum=False), default=SubscriptionStatus.pending
    )
    status_detail: Mapped[str] = mapped_column(Text, default="")
    # Per-subscription override of the global auto-process setting, so a noisy
    # channel can be parked without disabling the feature for everything.
    auto_process: Mapped[bool] = mapped_column(default=False)

    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now, onupdate=_now)

    events: Mapped[list[YouTubeEvent]] = relationship(
        back_populates="subscription", cascade="all, delete-orphan"
    )


class YouTubeEvent(Base):
    """One notification from the hub, and what we did with it.

    This table is both an audit trail and the idempotency guard. WebSub
    explicitly permits duplicate delivery, and a hub may retry a notification it
    thinks failed, so the same video can arrive several times. A unique
    constraint on ``(channel_id, video_id)`` means the second delivery is
    recognised and ignored rather than starting a second pipeline run.
    """

    __tablename__ = "youtube_events"
    __table_args__ = (UniqueConstraint("channel_id", "video_id", name="uq_youtube_event_video"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    subscription_id: Mapped[str | None] = mapped_column(
        ForeignKey("channel_subscriptions.id", ondelete="CASCADE"), nullable=True, index=True
    )
    channel_id: Mapped[str] = mapped_column(String(64), index=True)
    video_id: Mapped[str] = mapped_column(String(64))
    video_title: Mapped[str] = mapped_column(String(500), default="")
    video_url: Mapped[str] = mapped_column(Text, default="")

    status: Mapped[YouTubeEventStatus] = mapped_column(
        Enum(YouTubeEventStatus, native_enum=False), default=YouTubeEventStatus.received
    )
    reason: Mapped[str] = mapped_column(Text, default="")
    project_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # The raw hub payload, kept so a notification can be replayed when the
    # parsing rules change rather than requiring the hub to redeliver.
    payload: Mapped[dict] = mapped_column(JSON, default=dict)

    received_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    processed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    subscription: Mapped[ChannelSubscription | None] = relationship(back_populates="events")
