"""The post scheduler: claim due posts, publish them, retry what is retryable.

Two properties matter more than anything else here.

**A post is never published twice.** Claiming is an atomic conditional update
(``scheduled``/``failed`` -> ``publishing``) whose row count is checked, so two
workers racing for the same due post produce exactly one publish. A plain
"select then update" would let both win.

**A failure is never retried blindly.** The adapter's exception type decides:
transient and rate-limit failures are rescheduled with backoff, permanent
failures and auth failures are terminal. Retrying a rejected video forever is
how an app gets rate-limited or banned.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..config import get_settings
from ..logging_setup import get_logger
from ..models import (
    Clip,
    ClipStatus,
    PublishAttempt,
    PublishTarget,
    PublishTargetStatus,
    ScheduledPost,
    ScheduledPostStatus,
)
from .base import (
    AdapterError,
    AuthError,
    PermanentError,
    PlatformAdapter,
    PostSpec,
    RateLimitError,
)
from .registry import get_adapter

logger = get_logger(__name__)

# Statuses a post can be claimed from. ``publishing`` is deliberately excluded:
# a post already in flight must not be picked up by a second worker.
_CLAIMABLE = (ScheduledPostStatus.scheduled, ScheduledPostStatus.failed)


@dataclass(slots=True)
class PublishOutcome:
    """What happened to one post, for the caller to log or assert on."""

    post_id: str
    status: str
    detail: str = ""
    will_retry: bool = False
    next_attempt_at: datetime | None = None


def compute_backoff(attempt: int, *, retry_after_seconds: float | None = None) -> timedelta:
    """Delay before the next attempt.

    A platform-supplied ``retry_after`` always wins: it is the platform telling
    us exactly when it will accept a request again, and ignoring it is what turns
    a temporary throttle into a block. Otherwise exponential from 60s, capped so
    a post cannot silently drift days into the future.
    """
    settings = get_settings()
    if retry_after_seconds and retry_after_seconds > 0:
        return timedelta(seconds=min(retry_after_seconds, settings.publish_max_backoff_seconds))
    exponent = max(0, attempt - 1)
    delay = settings.publish_retry_base_seconds * (2**exponent)
    return timedelta(seconds=min(delay, settings.publish_max_backoff_seconds))


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _claim(session: Session, post_id: str) -> bool:
    """Atomically move a due post into ``publishing``. True if we won the race."""
    result = session.execute(
        update(ScheduledPost)
        .where(
            ScheduledPost.id == post_id,
            ScheduledPost.status.in_(_CLAIMABLE),
        )
        .values(status=ScheduledPostStatus.publishing)
    )
    session.commit()
    return result.rowcount == 1


def due_posts(
    session: Session,
    *,
    now: datetime | None = None,
    limit: int = 50,
    workspace_id: str | None = None,
) -> list[str]:
    """Ids of posts that are ready to publish, oldest first.

    A post is due when it is ``scheduled`` and its time has passed, or ``failed``
    with a backoff that has now elapsed. Posts whose target is not ``active`` are
    skipped: publishing to a disconnected account can only fail.

    ``workspace_id`` narrows the sweep to one tenant, which is what the HTTP
    "run now" button passes. The periodic worker leaves it None so one pass
    serves every workspace; that is why the target is scoped rather than the
    post, since the target carries the ``workspace_id``.
    """
    now = now or _utcnow()
    query = (
        select(ScheduledPost.id)
        .join(PublishTarget, PublishTarget.id == ScheduledPost.target_id)
        .where(
            PublishTarget.status == PublishTargetStatus.active,
            (
                (
                    (ScheduledPost.status == ScheduledPostStatus.scheduled)
                    & (ScheduledPost.scheduled_for <= now)
                )
                | (
                    (ScheduledPost.status == ScheduledPostStatus.failed)
                    & (ScheduledPost.next_attempt_at.is_not(None))
                    & (ScheduledPost.next_attempt_at <= now)
                )
            ),
        )
        .order_by(ScheduledPost.scheduled_for)
        .limit(limit)
    )
    if workspace_id is not None:
        query = query.where(PublishTarget.workspace_id == workspace_id)
    return list(session.scalars(query).all())


def _resolve_video_path(post: ScheduledPost, clip: Clip) -> Path | None:
    if not clip.render_path:
        return None
    path = Path(clip.render_path)
    return path if path.exists() else None


def _record_attempt(
    session: Session,
    post: ScheduledPost,
    *,
    ok: bool,
    error: str = "",
    detail: dict | None = None,
) -> None:
    session.add(
        PublishAttempt(
            post_id=post.id,
            attempt_number=post.attempts,
            ok=ok,
            error=error,
            detail=detail or {},
        )
    )


def publish_post(
    session: Session, post_id: str, *, adapter: PlatformAdapter | None = None
) -> PublishOutcome:
    """Claim and publish one post, applying retry policy. Never raises for a
    publish failure — the failure is recorded and returned, because one bad post
    must not abort a batch of otherwise fine ones.
    """
    settings = get_settings()

    if not _claim(session, post_id):
        # Another worker got it, or it is no longer due.
        return PublishOutcome(post_id=post_id, status="skipped", detail="not claimable")

    post = session.get(ScheduledPost, post_id)
    if post is None:  # deleted between claim and load
        return PublishOutcome(post_id=post_id, status="skipped", detail="post disappeared")

    clip = session.get(Clip, post.clip_id)
    target = session.get(PublishTarget, post.target_id)
    post.attempts += 1

    def fail(
        status: ScheduledPostStatus, message: str, retry_in: timedelta | None
    ) -> PublishOutcome:
        post.status = status
        post.error = message
        _record_attempt(session, post, ok=False, error=message)
        if retry_in is not None:
            post.next_attempt_at = _utcnow() + retry_in
        else:
            # Terminal: clear any backoff left from an earlier attempt, or the
            # post stays in the "due when next_attempt_at elapses" set forever.
            post.next_attempt_at = None
        session.commit()
        logger.warning(
            "publish.failed",
            post_id=post.id,
            attempt=post.attempts,
            platform=target.platform if target else "unknown",
            error=message,
            will_retry=retry_in is not None,
        )
        return PublishOutcome(
            post_id=post.id,
            status=str(status),
            detail=message,
            will_retry=retry_in is not None,
            next_attempt_at=post.next_attempt_at,
        )

    if clip is None or target is None:
        return fail(ScheduledPostStatus.failed, "clip or target no longer exists", None)

    video_path = _resolve_video_path(post, clip)
    if video_path is None:
        # A missing render is terminal until someone re-renders, so retrying on
        # a timer would just burn attempts.
        return fail(ScheduledPostStatus.failed, "clip has no rendered file on disk", None)

    if clip.status != ClipStatus.rendered:
        return fail(ScheduledPostStatus.failed, f"clip is {clip.status}, not rendered", None)

    if adapter is None:
        try:
            adapter = get_adapter(str(target.platform))
        except KeyError as exc:
            return fail(ScheduledPostStatus.failed, str(exc), None)

    spec = PostSpec(
        video_path=video_path,
        caption=post.caption,
        title=clip.title,
        options=dict(post.options or {}),
    )

    try:
        result = adapter.publish(dict(target.credentials or {}), target.external_account_id, spec)
    except AuthError as exc:
        # Terminal for the post, and the target is flagged so the UI can ask for
        # a reconnect instead of failing every future post the same way.
        target.status = PublishTargetStatus.needs_reauth
        target.status_detail = str(exc)
        session.commit()
        return fail(ScheduledPostStatus.failed, f"authentication failed: {exc}", None)
    except RateLimitError as exc:
        retry_in = compute_backoff(post.attempts, retry_after_seconds=exc.retry_after_seconds)
        if post.attempts >= settings.publish_max_attempts:
            return fail(ScheduledPostStatus.failed, f"rate limited, retries exhausted: {exc}", None)
        return fail(ScheduledPostStatus.failed, f"rate limited: {exc}", retry_in)
    except PermanentError as exc:
        return fail(ScheduledPostStatus.failed, f"rejected by platform: {exc}", None)
    except AdapterError as exc:
        # Unclassified adapter failure: treat as transient, but bounded by the
        # attempt cap so it cannot loop forever.
        if post.attempts >= settings.publish_max_attempts:
            return fail(ScheduledPostStatus.failed, f"retries exhausted: {exc}", None)
        return fail(ScheduledPostStatus.failed, str(exc), compute_backoff(post.attempts))
    except Exception as exc:  # noqa: BLE001 - a bug in an adapter must not kill the batch
        if post.attempts >= settings.publish_max_attempts:
            return fail(ScheduledPostStatus.failed, f"unexpected error: {exc}", None)
        return fail(
            ScheduledPostStatus.failed, f"unexpected error: {exc}", compute_backoff(post.attempts)
        )

    post.status = ScheduledPostStatus.published
    post.external_post_id = result.external_post_id
    post.error = None
    post.next_attempt_at = None
    post.published_at = _utcnow()
    _record_attempt(
        session,
        post,
        ok=True,
        detail={"url": result.url, "pending": result.pending, **result.detail},
    )
    session.commit()
    logger.info(
        "publish.succeeded",
        post_id=post.id,
        platform=str(target.platform),
        external_post_id=result.external_post_id,
        attempts=post.attempts,
    )
    return PublishOutcome(post_id=post.id, status="published", detail=result.url)


def run_due_posts(
    session: Session,
    *,
    now: datetime | None = None,
    limit: int = 50,
    workspace_id: str | None = None,
) -> list[PublishOutcome]:
    """Publish every post that is due. The entry point a worker calls."""
    outcomes: list[PublishOutcome] = []
    for post_id in due_posts(session, now=now, limit=limit, workspace_id=workspace_id):
        outcomes.append(publish_post(session, post_id))
    return outcomes
