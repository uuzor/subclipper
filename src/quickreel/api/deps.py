"""Request dependencies for authentication and tenant resolution.

Every content route depends on :func:`current_workspace`, which both proves the
caller is logged in and pins every query to one tenant. Making the workspace a
dependency rather than something a handler looks up is the point: a handler that
forgets to scope its query is a cross-tenant leak, so the scope is handed to it
and the helper functions it uses take it as a required argument.

**Why cookies and not a bearer token.** The frontend is served from the same
origin as the API, so a cookie is the natural fit and is not reachable by
JavaScript (``HttpOnly``). The cookie is ``SameSite=Lax``, which means a browser
will not attach it to a cross-site POST, and the API requires a JSON body —
which a cross-site HTML form cannot produce. Those two together are what stands
in for a CSRF token here; a bearer token in ``localStorage`` would trade CSRF for
XSS exposure, which is the worse deal.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import auth, auth_service
from ..config import get_settings
from ..db import get_db
from ..logging_setup import get_logger
from ..models import Project, User, UserSession, Workspace, WorkspaceRole

logger = get_logger(__name__)


@dataclass(slots=True)
class Principal:
    """Who is calling, and in which tenant."""

    user: User
    workspace: Workspace
    session: UserSession
    role: str


def _unauthorized(detail: str = "authentication required") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Cookie"},
    )


def optional_principal(
    request: Request, db: Session = Depends(get_db)
) -> Principal | None:
    """Resolve the caller if a valid session cookie is present, else None.

    Used by endpoints that must work both ways — the WebSub callback is called by
    the hub with no cookie at all, and the health check is public.
    """
    token = request.cookies.get(auth.SESSION_COOKIE, "")
    if not token:
        return None
    record = auth_service.resolve_session(db, token)
    if record is None:
        return None

    user = db.get(User, record.user_id)
    workspace = db.get(Workspace, record.workspace_id)
    if user is None or not user.is_active or workspace is None:
        # The account or tenant was deleted underneath a live session. Treat it
        # as logged out rather than raising a 500 on every request.
        return None

    return Principal(user=user, workspace=workspace, session=record, role=_role_for(db, record))


def current_principal(
    principal: Principal | None = Depends(optional_principal),
) -> Principal:
    """Require a logged-in caller."""
    if principal is None:
        raise _unauthorized()
    return principal


def current_user(principal: Principal = Depends(current_principal)) -> User:
    return principal.user


def current_workspace(principal: Principal = Depends(current_principal)) -> Workspace:
    return principal.workspace


def require_owner(principal: Principal = Depends(current_principal)) -> Principal:
    """Require the caller to own the workspace they are acting in."""
    if principal.role != WorkspaceRole.owner:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="this action requires the workspace owner",
        )
    return principal


def _role_for(db: Session, record: UserSession) -> str:
    from ..models import Membership

    membership = db.scalars(
        select(Membership).where(
            Membership.user_id == record.user_id,
            Membership.workspace_id == record.workspace_id,
        )
    ).first()
    return str(membership.role) if membership else str(WorkspaceRole.member)


def set_session_cookie(response: Response, token: str) -> None:
    """Attach the session cookie.

    ``HttpOnly`` keeps it away from JavaScript, ``SameSite=Lax`` keeps it off
    cross-site POSTs, and ``Secure`` is set whenever the public origin is HTTPS —
    which it is in production, where the production guard requires a real base
    URL. Setting Secure on a plain-HTTP local setup would stop the cookie being
    stored at all, so it is conditional.
    """
    settings = get_settings()
    secure = settings.public_base_url.strip().lower().startswith("https://")
    response.set_cookie(
        key=auth.SESSION_COOKIE,
        value=token,
        httponly=True,
        samesite="lax",
        secure=secure,
        max_age=int(auth_service.SESSION_TTL.total_seconds()),
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(key=auth.SESSION_COOKIE, path="/")


# ---- Tenant-scoped lookups ----
#
# Each of these takes the workspace id as a required argument, so a handler
# cannot accidentally fetch another tenant's row: the query cannot be written
# without it. A missing row and a row belonging to someone else are the same
# 404, which also stops the API being used to probe for the existence of ids.


def get_scoped_project(db: Session, project_id: str, workspace_id: str) -> Project:
    project = db.scalars(
        select(Project).where(Project.id == project_id, Project.workspace_id == workspace_id)
    ).first()
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return project


def scoped_or_404(db: Session, model, row_id: str, workspace_id: str, label: str):
    """Fetch a workspace-owned row by id, or 404.

    ``model`` must have a ``workspace_id`` column.
    """
    row = db.scalars(
        select(model).where(model.id == row_id, model.workspace_id == workspace_id)
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"{label} not found")
    return row
