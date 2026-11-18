"""User, workspace and session operations.

The policy layer between :mod:`quickreel.auth` (which knows how to hash and
compare) and the HTTP routes (which know about cookies and status codes). Keeping
it separate means the same signup and login logic is reachable from a test or a
management command without going through HTTP.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import auth
from .logging_setup import get_logger
from .models import Membership, User, UserSession, Workspace, WorkspaceRole

logger = get_logger(__name__)

# How long a login lasts before it must be repeated. Long enough not to be
# annoying for a tool someone uses weekly, short enough that a stolen cookie
# stops working.
SESSION_TTL = timedelta(days=30)


class SignupError(RuntimeError):
    """Signup could not proceed. Safe to show the user."""


@dataclass(slots=True)
class SignupResult:
    user: User
    workspace: Workspace
    session: UserSession
    token: str


def create_user_with_workspace(
    session: Session,
    *,
    email: str,
    password: str,
    display_name: str = "",
    workspace_name: str = "",
    user_agent: str = "",
) -> SignupResult:
    """Create an account, its first workspace, and a logged-in session.

    The workspace is created here rather than lazily on first use so that every
    account has a tenant from the moment it exists. That removes a whole class of
    bug where content is written before a workspace is known and ends up
    unscoped.
    """
    normalized = auth.validate_email(email)
    auth.validate_password(password)

    existing = session.scalars(select(User).where(User.email == normalized)).first()
    if existing is not None:
        # The one place enumeration is unavoidable: signup has to say the
        # address is taken, or it cannot function. The wording is deliberately
        # neutral, and the login endpoint does not confirm it.
        raise SignupError("an account with that email already exists")

    user = User(
        email=normalized,
        password_hash=auth.hash_password(password),
        display_name=display_name.strip(),
    )
    workspace = Workspace(name=workspace_name.strip() or _default_workspace_name(normalized))
    session.add_all([user, workspace])
    session.flush()

    session.add(
        Membership(user_id=user.id, workspace_id=workspace.id, role=WorkspaceRole.owner)
    )
    session.flush()

    token, record = _issue_session(session, user, workspace, user_agent=user_agent)
    try:
        session.commit()
    except IntegrityError as exc:
        # Two signups for the same address racing. The unique constraint is the
        # real guard; the lookup above is the fast path.
        session.rollback()
        raise SignupError("an account with that email already exists") from exc

    logger.info("auth.signed_up", user_id=user.id, workspace_id=workspace.id)
    return SignupResult(user=user, workspace=workspace, session=record, token=token)


def authenticate(
    session: Session, *, email: str, password: str, user_agent: str = ""
) -> SignupResult:
    """Verify a credential and start a session.

    Raises :class:`auth.AuthError` with one message for both "no such account"
    and "wrong password", so the response cannot be used to discover which
    addresses are registered.
    """
    normalized = auth.normalize_email(email)
    user = session.scalars(select(User).where(User.email == normalized)).first()

    if user is None:
        # Spend the same time as a real verification, then fail identically.
        auth.dummy_verify(password)
        raise auth.AuthError("email or password is incorrect")

    if not auth.verify_password(password, user.password_hash):
        raise auth.AuthError("email or password is incorrect")

    if not user.is_active:
        raise auth.AuthError("this account has been disabled")

    # Upgrade a hash made with weaker parameters while the plaintext is in hand.
    if auth.needs_rehash(user.password_hash):
        user.password_hash = auth.hash_password(password)
        logger.info("auth.rehash", user_id=user.id)

    membership = _first_membership(session, user.id)
    if membership is None:
        # Should not happen: signup always creates one. Repair rather than 500,
        # because a user with no workspace cannot use the product at all.
        workspace = Workspace(name=_default_workspace_name(normalized))
        session.add(workspace)
        session.flush()
        membership = Membership(
            user_id=user.id, workspace_id=workspace.id, role=WorkspaceRole.owner
        )
        session.add(membership)
        session.flush()
        logger.warning("auth.repaired_missing_workspace", user_id=user.id)

    workspace = session.get(Workspace, membership.workspace_id)
    token, record = _issue_session(session, user, workspace, user_agent=user_agent)
    session.commit()
    logger.info("auth.logged_in", user_id=user.id, workspace_id=workspace.id)
    return SignupResult(user=user, workspace=workspace, session=record, token=token)


def resolve_session(session: Session, token: str) -> UserSession | None:
    """Look up a live session by its cookie value.

    An expired session is deleted on sight rather than left to accumulate, and
    returns None so the caller treats it as unauthenticated.
    """
    if not token:
        return None
    token_hash = auth.hash_session_token(token)
    record = session.scalars(
        select(UserSession).where(UserSession.token_hash == token_hash)
    ).first()
    if record is None:
        return None
    if record.expires_at <= datetime.now(UTC):
        session.delete(record)
        session.commit()
        return None
    return record


def touch_session(session: Session, record: UserSession) -> None:
    """Record that a session was used. Cheap, and makes "last active" honest."""
    record.last_seen_at = datetime.now(UTC)
    session.commit()


def end_session(session: Session, token: str) -> None:
    """Log out. Deleting the row is what makes the cookie immediately useless."""
    if not token:
        return
    token_hash = auth.hash_session_token(token)
    record = session.scalars(
        select(UserSession).where(UserSession.token_hash == token_hash)
    ).first()
    if record is not None:
        session.delete(record)
        session.commit()
        logger.info("auth.logged_out", user_id=record.user_id)


def end_all_sessions(session: Session, user_id: str) -> int:
    """Revoke every session for a user. Used after a password change."""
    records = list(
        session.scalars(select(UserSession).where(UserSession.user_id == user_id)).all()
    )
    for record in records:
        session.delete(record)
    session.commit()
    return len(records)


def switch_workspace(session: Session, record: UserSession, workspace_id: str) -> bool:
    """Point a session at another workspace the user is a member of.

    Returns False when the user is not a member, so a caller cannot switch into
    a tenant by guessing its id.
    """
    membership = session.scalars(
        select(Membership).where(
            Membership.user_id == record.user_id,
            Membership.workspace_id == workspace_id,
        )
    ).first()
    if membership is None:
        return False
    record.workspace_id = workspace_id
    session.commit()
    return True


def change_password(session: Session, user: User, *, current: str, new: str) -> None:
    """Change a password, revoking other sessions.

    Other sessions are dropped because a password change is the remedy for a
    suspected compromise; leaving the attacker's session live would defeat it.
    """
    if not auth.verify_password(current, user.password_hash):
        raise auth.AuthError("current password is incorrect")
    auth.validate_password(new)
    user.password_hash = auth.hash_password(new)
    session.commit()
    end_all_sessions(session, user.id)
    logger.info("auth.password_changed", user_id=user.id)


def _issue_session(
    session: Session, user: User, workspace: Workspace, *, user_agent: str = ""
) -> tuple[str, UserSession]:
    token = auth.new_session_token()
    record = UserSession(
        user_id=user.id,
        workspace_id=workspace.id,
        token_hash=auth.hash_session_token(token),
        user_agent=(user_agent or "")[:400],
        expires_at=datetime.now(UTC) + SESSION_TTL,
    )
    session.add(record)
    session.flush()
    return token, record


def _first_membership(session: Session, user_id: str) -> Membership | None:
    return session.scalars(
        select(Membership)
        .where(Membership.user_id == user_id)
        .order_by(Membership.created_at)
    ).first()


def _default_workspace_name(email: str) -> str:
    local = email.split("@", 1)[0]
    return f"{local}'s workspace"


def list_workspaces_for(session: Session, user_id: str) -> list[Workspace]:
    return list(
        session.scalars(
            select(Workspace)
            .join(Membership, Membership.workspace_id == Workspace.id)
            .where(Membership.user_id == user_id)
            .order_by(Workspace.created_at)
        ).all()
    )


def create_workspace(
    session: Session, user: User, *, name: str, role: WorkspaceRole = WorkspaceRole.owner
) -> Workspace:
    workspace = Workspace(name=name.strip() or "New workspace")
    session.add(workspace)
    session.flush()
    session.add(Membership(user_id=user.id, workspace_id=workspace.id, role=role))
    session.commit()
    logger.info("workspace.created", workspace_id=workspace.id, user_id=user.id)
    return workspace


def add_member(
    session: Session, *, workspace_id: str, email: str, role: WorkspaceRole = WorkspaceRole.member
) -> Membership:
    """Add an existing account to a workspace.

    The account must already exist. Invitation-by-email would need a token and a
    mail path, which is deliberately out of scope here.
    """
    normalized = auth.validate_email(email)
    user = session.scalars(select(User).where(User.email == normalized)).first()
    if user is None:
        raise SignupError("no account with that email")
    existing = session.scalars(
        select(Membership).where(
            Membership.user_id == user.id, Membership.workspace_id == workspace_id
        )
    ).first()
    if existing is not None:
        existing.role = role
        session.commit()
        return existing
    membership = Membership(user_id=user.id, workspace_id=workspace_id, role=role)
    session.add(membership)
    session.commit()
    logger.info("workspace.member_added", workspace_id=workspace_id, user_id=user.id)
    return membership


def ensure_legacy_workspace(session: Session, *, name: str = "Legacy workspace") -> Workspace:
    """The workspace that pre-tenancy rows are adopted into.

    Deterministic, so running the backfill twice adopts the same rows rather than
    creating a second home for them.
    """
    marker = uuid.uuid5(uuid.NAMESPACE_URL, "quickreel:legacy-workspace").hex
    workspace = session.get(Workspace, marker[:32])
    if workspace is None:
        workspace = Workspace(id=marker[:32], name=name)
        session.add(workspace)
        session.commit()
        logger.info("workspace.legacy_created", workspace_id=workspace.id)
    return workspace
