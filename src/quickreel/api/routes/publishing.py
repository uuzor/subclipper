"""Publishing endpoints: connect targets, schedule posts, run the scheduler.

The routes stay thin. All retry and claim logic lives in
:mod:`quickreel.publishing.scheduler`, so the same code path serves the HTTP
"run now" button and the periodic worker.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...db import get_db
from ...logging_setup import get_logger
from ...models import (
    Clip,
    ClipStatus,
    Platform,
    PublishAttempt,
    PublishTarget,
    ScheduledPost,
    ScheduledPostStatus,
)
from ...publishing.registry import available_adapters
from ...publishing.scheduler import publish_post, run_due_posts
from ...schemas import (
    PublishAttemptOut,
    PublishRunOut,
    PublishTargetIn,
    PublishTargetOut,
    ScheduledPostIn,
    ScheduledPostOut,
)

logger = get_logger(__name__)
router = APIRouter(prefix="/api/v1", tags=["publishing"])


def _platform_names() -> list[str]:
    return [str(p) for p in Platform]


@router.get("/platforms")
def list_platforms() -> dict:
    """Platforms the scheduler understands, and which have a real adapter.

    The UI uses this to render connect buttons and to label a platform as
    "mock" versus "ready", rather than discovering it on the first failed post.
    """
    registered = set(available_adapters())
    return {
        "platforms": [
            {"name": name, "live_adapter_registered": name in registered}
            for name in _platform_names()
        ]
    }


@router.post("/publish-targets", response_model=PublishTargetOut, status_code=201)
def create_target(payload: PublishTargetIn, db: Session = Depends(get_db)) -> PublishTarget:
    if payload.platform not in _platform_names():
        raise HTTPException(
            status_code=422,
            detail=f"unknown platform {payload.platform!r}; expected one of {_platform_names()}",
        )
    target = PublishTarget(
        platform=Platform(payload.platform),
        display_name=payload.display_name,
        external_account_id=payload.external_account_id,
        credentials=payload.credentials,
    )
    db.add(target)
    db.commit()
    db.refresh(target)
    logger.info("publish_target.created", target_id=target.id, platform=payload.platform)
    return target


@router.get("/publish-targets", response_model=list[PublishTargetOut])
def list_targets(db: Session = Depends(get_db)) -> list[PublishTarget]:
    return list(db.scalars(select(PublishTarget).order_by(PublishTarget.created_at)).all())


@router.delete("/publish-targets/{target_id}", status_code=204)
def delete_target(target_id: str, db: Session = Depends(get_db)) -> None:
    target = db.get(PublishTarget, target_id)
    if target is None:
        raise HTTPException(status_code=404, detail="publish target not found")
    db.delete(target)
    db.commit()


@router.post("/scheduled-posts", response_model=ScheduledPostOut, status_code=201)
def create_scheduled_post(payload: ScheduledPostIn, db: Session = Depends(get_db)) -> ScheduledPost:
    clip = db.get(Clip, payload.clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail="clip not found")
    # Scheduling an unrendered clip would create a post that can only fail, so
    # reject it here where the message is useful.
    if clip.status != ClipStatus.rendered:
        raise HTTPException(
            status_code=409,
            detail=f"clip is {clip.status}; render it before scheduling",
        )
    target = db.get(PublishTarget, payload.target_id)
    if target is None:
        raise HTTPException(status_code=404, detail="publish target not found")

    post = ScheduledPost(
        clip_id=payload.clip_id,
        target_id=payload.target_id,
        scheduled_for=payload.scheduled_for,
        caption=payload.caption,
        options=payload.options,
    )
    db.add(post)
    db.commit()
    db.refresh(post)
    logger.info(
        "scheduled_post.created",
        post_id=post.id,
        clip_id=post.clip_id,
        target_id=post.target_id,
        scheduled_for=post.scheduled_for.isoformat(),
    )
    return post


@router.get("/scheduled-posts", response_model=list[ScheduledPostOut])
def list_scheduled_posts(
    status: str | None = Query(default=None),
    clip_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
) -> list[ScheduledPost]:
    query = select(ScheduledPost)
    if status:
        try:
            query = query.where(ScheduledPost.status == ScheduledPostStatus(status))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"unknown status {status!r}") from exc
    if clip_id:
        query = query.where(ScheduledPost.clip_id == clip_id)
    query = query.order_by(ScheduledPost.scheduled_for).limit(limit)
    return list(db.scalars(query).all())


@router.get("/scheduled-posts/{post_id}", response_model=ScheduledPostOut)
def get_scheduled_post(post_id: str, db: Session = Depends(get_db)) -> ScheduledPost:
    post = db.get(ScheduledPost, post_id)
    if post is None:
        raise HTTPException(status_code=404, detail="scheduled post not found")
    return post


@router.post("/scheduled-posts/{post_id}/cancel", response_model=ScheduledPostOut)
def cancel_scheduled_post(post_id: str, db: Session = Depends(get_db)) -> ScheduledPost:
    post = db.get(ScheduledPost, post_id)
    if post is None:
        raise HTTPException(status_code=404, detail="scheduled post not found")
    if post.status == ScheduledPostStatus.published:
        raise HTTPException(status_code=409, detail="post has already been published")
    post.status = ScheduledPostStatus.canceled
    post.next_attempt_at = None
    db.commit()
    db.refresh(post)
    return post


@router.get("/scheduled-posts/{post_id}/attempts", response_model=list[PublishAttemptOut])
def list_attempts(post_id: str, db: Session = Depends(get_db)) -> list[PublishAttempt]:
    post = db.get(ScheduledPost, post_id)
    if post is None:
        raise HTTPException(status_code=404, detail="scheduled post not found")
    query = (
        select(PublishAttempt)
        .where(PublishAttempt.post_id == post_id)
        .order_by(PublishAttempt.created_at)
    )
    return list(db.scalars(query).all())


@router.post("/scheduled-posts/{post_id}/publish-now", response_model=ScheduledPostOut)
def publish_now(post_id: str, db: Session = Depends(get_db)) -> ScheduledPost:
    """Attempt one post immediately, regardless of its scheduled time.

    The post must still be claimable (not published, not in flight); this is the
    same claim the worker uses, so pressing the button twice cannot double-post.
    """
    post = db.get(ScheduledPost, post_id)
    if post is None:
        raise HTTPException(status_code=404, detail="scheduled post not found")
    outcome = publish_post(db, post_id)
    if outcome.status == "skipped":
        raise HTTPException(status_code=409, detail=outcome.detail or "post is not claimable")
    db.refresh(post)
    return post


@router.post("/publishing/run-due", response_model=PublishRunOut)
def run_due(
    limit: int = Query(default=50, ge=1, le=500),
    db: Session = Depends(get_db),
) -> PublishRunOut:
    """Publish every due post now. The same call the periodic worker makes."""
    outcomes = run_due_posts(db, now=datetime.now(UTC), limit=limit)
    counts = {"published": 0, "failed": 0, "skipped": 0}
    for outcome in outcomes:
        counts[outcome.status] = counts.get(outcome.status, 0) + 1
    return PublishRunOut(
        examined=len(outcomes),
        published=counts.get("published", 0),
        failed=counts.get("failed", 0),
        skipped=counts.get("skipped", 0),
        outcomes=[
            {
                "post_id": o.post_id,
                "status": o.status,
                "detail": o.detail,
                "will_retry": o.will_retry,
                "next_attempt_at": o.next_attempt_at.isoformat() if o.next_attempt_at else None,
            }
            for o in outcomes
        ],
    )
