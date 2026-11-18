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


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
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
    providers: Mapped[dict] = mapped_column(JSON, default=dict)
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
