"""Signup, login, logout, and "who am I" endpoints.

These are the only routes that are reachable without a session, apart from the
health check and the WebSub callback. Each one is written to answer identically
whether an account exists or not, so the API cannot be used to enumerate
registered addresses.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session

from ... import auth, auth_service
from ...db import get_db
from ...logging_setup import get_logger
from ...models import WorkspaceRole
from ...schemas import (
    LoginIn,
    PasswordChangeIn,
    SessionOut,
    SignupIn,
    UserOut,
    WorkspaceOut,
    WorkspaceSwitchIn,
)
from ..deps import (
    Principal,
    clear_session_cookie,
    current_principal,
    set_session_cookie,
)

logger = get_logger(__name__)
router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


def _client_key(request: Request, email: str) -> str:
    """Throttle key for a login attempt.

    Keyed on both the address and the client, so one attacker cannot lock out a
    legitimate user by hammering their email from elsewhere, and one client
    cannot spray many addresses without also tripping the per-client limit.
    """
    client = request.client.host if request.client else "unknown"
    return f"{auth.normalize_email(email)}|{client}"


def _session_payload(principal: Principal, db: Session) -> SessionOut:
    workspaces = auth_service.list_workspaces_for(db, principal.user.id)
    return SessionOut(
        user=UserOut.model_validate(principal.user),
        workspace=WorkspaceOut.model_validate(principal.workspace),
        role=principal.role,
        workspaces=[WorkspaceOut.model_validate(w) for w in workspaces],
    )


@router.post("/signup", response_model=SessionOut, status_code=status.HTTP_201_CREATED)
def signup(payload: SignupIn, request: Request, response: Response, db: Session = Depends(get_db)):
    """Create an account, its workspace, and a logged-in session."""
    try:
        result = auth_service.create_user_with_workspace(
            db,
            email=payload.email,
            password=payload.password,
            display_name=payload.display_name,
            workspace_name=payload.workspace_name,
            user_agent=request.headers.get("user-agent", ""),
        )
    except auth.AuthError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except auth_service.SignupError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    set_session_cookie(response, result.token)
    principal = Principal(
        user=result.user,
        workspace=result.workspace,
        session=result.session,
        role=str(WorkspaceRole.owner),
    )
    return _session_payload(principal, db)


@router.post("/login", response_model=SessionOut)
def login(payload: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)):
    key = _client_key(request, payload.email)
    retry_after = auth.login_throttle.retry_after(key)
    if retry_after > 0:
        # 429 with a concrete wait, so a legitimate user who mistyped a few times
        # is told how long rather than left guessing.
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="too many failed attempts; try again shortly",
            headers={"Retry-After": str(int(retry_after) + 1)},
        )

    try:
        result = auth_service.authenticate(
            db,
            email=payload.email,
            password=payload.password,
            user_agent=request.headers.get("user-agent", ""),
        )
    except auth.AuthError as exc:
        auth.login_throttle.record_failure(key)
        # Same message and same status for a missing account and a wrong
        # password, which is what stops this being an enumeration oracle.
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc

    auth.login_throttle.record_success(key)
    set_session_cookie(response, result.token)
    principal = Principal(
        user=result.user,
        workspace=result.workspace,
        session=result.session,
        role=str(WorkspaceRole.owner),
    )
    return _session_payload(principal, db)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, response: Response, db: Session = Depends(get_db)) -> None:
    """End the session and clear the cookie.

    Idempotent: logging out twice, or with a dead cookie, is a success, because
    the caller's intent — "I am no longer logged in" — is satisfied either way.
    """
    token = request.cookies.get(auth.SESSION_COOKIE, "")
    auth_service.end_session(db, token)
    clear_session_cookie(response)


@router.get("/me", response_model=SessionOut)
def me(principal: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    """The current session, or 401. The frontend calls this on load to decide
    whether to show the app or the login screen."""
    return _session_payload(principal, db)


@router.post("/workspace", response_model=SessionOut)
def switch_workspace(
    payload: WorkspaceSwitchIn,
    principal: Principal = Depends(current_principal),
    db: Session = Depends(get_db),
):
    """Move this session to another workspace the caller belongs to."""
    if not auth_service.switch_workspace(db, principal.session, payload.workspace_id):
        # Not a member: 404 rather than 403, so the response does not confirm
        # that the workspace exists.
        raise HTTPException(status_code=404, detail="workspace not found")
    db.refresh(principal.session)
    principal.workspace = db.get(type(principal.workspace), payload.workspace_id)
    return _session_payload(principal, db)


@router.post("/password", status_code=status.HTTP_204_NO_CONTENT)
def change_password(
    payload: PasswordChangeIn,
    response: Response,
    principal: Principal = Depends(current_principal),
    db: Session = Depends(get_db),
) -> None:
    """Change the password and sign every session out, including this one.

    The caller must log in again with the new password. That is intentional: if
    the password was changed because it leaked, leaving any session live would
    leave the attacker inside.
    """
    try:
        auth_service.change_password(
            db,
            principal.user,
            current=payload.current_password,
            new=payload.new_password,
        )
    except auth.AuthError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    clear_session_cookie(response)
