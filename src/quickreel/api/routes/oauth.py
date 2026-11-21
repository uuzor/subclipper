"""OAuth connect endpoints.

Three routes, matching the three steps a reviewer will click through:

1. ``GET  /oauth/{platform}/start``    — where the browser is sent to consent.
2. ``GET  /oauth/{platform}/callback`` — where the platform returns the user.
3. ``GET  /publish-targets``           — where the connected account appears.

The callback is the interesting one. It is reached by a redirect from a
third-party site, so it must not trust anything in the request: the ``state`` is
verified before the code is used, and the code is only exchanged server-side.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ... import oauth
from ...config import get_settings
from ...db import get_db
from ...logging_setup import get_logger
from ...models import Platform, PublishTarget, Workspace
from ...schemas import OAuthStartOut
from ..deps import current_workspace

logger = get_logger(__name__)
router = APIRouter(prefix="/api/v1/oauth", tags=["oauth"])


def _known_platforms() -> list[str]:
    return [str(p) for p in Platform]


@router.get("/providers")
def list_providers() -> dict:
    """Which platforms can be connected, and whether a client is configured."""
    return {"providers": oauth.describe_providers()}


@router.get("/{platform}/start", response_model=OAuthStartOut)
def start(
    platform: str, workspace: Workspace = Depends(current_workspace)
) -> OAuthStartOut:
    """Return the consent URL for the browser to navigate to.

    Returned as JSON rather than a 302 so the frontend can open it in a popup
    and keep the user's place in the app. The caller's workspace is signed into
    the state, so the account that comes back is filed under this tenant.
    """
    if platform not in _known_platforms():
        raise HTTPException(status_code=404, detail=f"unknown platform {platform!r}")
    try:
        url, state = oauth.authorization_url(platform, workspace_id=workspace.id)
    except oauth.OAuthError as exc:
        # 409 rather than 500: the platform is fine, the app is not configured.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    logger.info("oauth.flow_started", platform=platform, state=oauth.state_fingerprint(state))
    return OAuthStartOut(authorize_url=url, state=state, platform=platform)


@router.get("/{platform}/callback")
def callback(
    platform: str,
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
    error_description: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Handle the platform's redirect and store the connected account.

    Always ends in a redirect back to the app, including on failure, so the user
    is never left staring at a JSON error from a flow they started by clicking a
    button. The reason is passed as a query parameter for the UI to display.
    """
    settings = get_settings()
    app_url = settings.public_base_url.rstrip("/")

    def back(**params: str) -> RedirectResponse:
        query = "&".join(f"{key}={value}" for key, value in params.items())
        return RedirectResponse(url=f"{app_url}/accounts?{query}", status_code=303)

    if error:
        # The user declined, or the platform refused. Not our bug.
        logger.warning("oauth.denied", platform=platform, error=error)
        return back(connected="error", reason=error_description or error)

    if platform not in _known_platforms():
        return back(connected="error", reason="unknown platform")

    if not code or not state:
        return back(connected="error", reason="missing code or state")

    try:
        # Verifies the signature and the age, and returns the platform and the
        # workspace the state was issued for. Comparing the platform to the path
        # segment closes the door on using a state issued for one platform on
        # another's callback.
        state_platform, state_workspace = oauth.verify_state(state)
    except oauth.OAuthError as exc:
        logger.warning("oauth.state_rejected", platform=platform, reason=str(exc))
        return back(connected="error", reason=str(exc))

    if state_platform != platform:
        logger.warning(
            "oauth.state_platform_mismatch", path_platform=platform, state_platform=state_platform
        )
        return back(connected="error", reason="OAuth state was issued for a different platform")

    if db.get(Workspace, state_workspace) is None:
        # The workspace was deleted while the user was on the consent screen.
        # Refusing is the only safe answer: filing the account anywhere else
        # would attach it to a tenant that did not ask for it.
        logger.warning("oauth.workspace_missing", workspace_id=state_workspace)
        return back(connected="error", reason="the workspace this flow started in no longer exists")

    try:
        token_payload = oauth.exchange_code(platform, code)
    except oauth.OAuthError as exc:
        logger.warning("oauth.exchange_failed", platform=platform, reason=str(exc))
        return back(connected="error", reason=str(exc))

    account_id = oauth.account_id_from_token_payload(platform, token_payload)
    credentials = oauth.dumps_credentials(platform, token_payload)

    # Reconnecting an account updates the existing target rather than adding a
    # duplicate row, so a user who re-authorises does not end up with two.
    target = db.scalars(
        select(PublishTarget).where(
            PublishTarget.platform == Platform(platform),
            PublishTarget.external_account_id == account_id,
            PublishTarget.workspace_id == state_workspace,
        )
    ).first()

    if target is None:
        target = PublishTarget(
            workspace_id=state_workspace,
            platform=Platform(platform),
            external_account_id=account_id,
            display_name=token_payload.get("display_name", "") or f"{platform} account",
            credentials=credentials,
        )
        db.add(target)
    else:
        target.credentials = credentials
        target.status_detail = ""
    # Reconnecting clears a previous needs_reauth, which is the whole point of
    # showing the reconnect prompt.
    target.status = "active"

    db.commit()
    logger.info("oauth.connected", platform=platform, target_id=target.id, account_id=account_id)
    return back(connected=platform, account=account_id)
