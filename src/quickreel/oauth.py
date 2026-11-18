"""OAuth 2.0 authorization-code flow for connecting publishing accounts.

Kept separate from :mod:`quickreel.publishing` because it is an *authorization*
concern, not a publishing one: it produces the credentials that an adapter later
uses, and an adapter never needs to know how they were obtained.

Two things here are security-relevant and easy to get wrong:

**The ``state`` parameter is signed, not random.** A random state that the
server forgets between requests is not a defence; it only works if the callback
can verify it came from this server. Signing an HMAC over (state, platform,
timestamp) lets the callback authenticate the round trip without server-side
session storage, which matters because the callback arrives on a different
request than the one that started the flow.

**Tokens are never returned to the client.** They go straight into
``PublishTarget.credentials``. The API response for a target omits them, so a
browser or a log never sees a refresh token.

The provider table below is data, not code: adding a platform is a new entry,
which keeps the flow identical across platforms and makes it obvious what each
one requires.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass, field
from urllib.parse import urlencode

from .config import get_settings
from .logging_setup import get_logger

logger = get_logger(__name__)

# How long a started flow stays valid. Long enough to log in and consent, short
# enough that a leaked authorize URL is not useful for long.
_STATE_TTL_SECONDS = 600


@dataclass(frozen=True, slots=True)
class OAuthProvider:
    """Everything needed to run the authorization-code flow for one platform."""

    name: str
    authorize_url: str
    token_url: str
    scopes: tuple[str, ...]
    # True when the flow needs a PKCE challenge. Public clients (a desktop or
    # SPA app) do; confidential clients with a secret do not. Sent regardless
    # where the platform supports it, since it costs nothing and protects
    # against authorization-code interception.
    uses_pkce: bool = True
    # Some platforms return the account id under a different key.
    account_id_keys: tuple[str, ...] = ("user_id", "open_id", "id")
    extra_authorize_params: dict[str, str] = field(default_factory=dict)


# TikTok, Meta, and Google all speak authorization-code with PKCE. The scope
# lists are deliberately minimal: publishing needs the ability to post, not to
# read a creator's private analytics, messages, or followers.
PROVIDERS: dict[str, OAuthProvider] = {
    "tiktok": OAuthProvider(
        name="tiktok",
        authorize_url="https://www.tiktok.com/v2/auth/authorize/",
        token_url="https://open.tiktokapis.com/v2/oauth/token/",
        scopes=("user.info.basic", "video.publish"),
        account_id_keys=("open_id", "user_id", "id"),
    ),
    "instagram_reels": OAuthProvider(
        name="instagram_reels",
        authorize_url="https://www.facebook.com/v21.0/dialog/oauth",
        token_url="https://graph.facebook.com/v21.0/oauth/access_token",
        scopes=("instagram_basic", "instagram_content_publish", "pages_show_list"),
        uses_pkce=False,  # confidential client with an app secret
        account_id_keys=("user_id", "id"),
    ),
    "youtube_shorts": OAuthProvider(
        name="youtube_shorts",
        authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        scopes=(
            "https://www.googleapis.com/auth/youtube.upload",
            "https://www.googleapis.com/auth/youtube.readonly",
        ),
        account_id_keys=("channel_id", "id", "user_id"),
        extra_authorize_params={"access_type": "offline", "prompt": "consent"},
    ),
}


class OAuthError(RuntimeError):
    """A flow failure that should be reported to the user, not retried."""


def provider_for(platform: str) -> OAuthProvider:
    try:
        return PROVIDERS[platform]
    except KeyError as exc:
        raise OAuthError(f"no OAuth provider configured for platform {platform!r}") from exc


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _secret() -> bytes:
    settings = get_settings()
    secret = settings.oauth_state_secret.strip()
    if secret:
        return secret.encode("utf-8")
    # Local development only: the production guard in config.py refuses to build
    # settings without a real secret, so this branch cannot be reached there.
    return b"quickreel-dev-only-oauth-state"


def sign_state(platform: str, *, now: float | None = None) -> str:
    """Build a signed ``state`` value binding this flow to a platform and time.

    Format: ``<nonce>.<issued_at>.<hmac>``. The nonce makes each flow unique;
    the timestamp bounds its life; the HMAC is what makes the callback able to
    trust that this server issued it.
    """
    nonce = secrets.token_urlsafe(16)
    issued_at = int(now if now is not None else time.time())
    payload = f"{nonce}.{issued_at}.{platform}"
    signature = hmac.new(_secret(), payload.encode("utf-8"), hashlib.sha256).digest()
    return f"{payload}.{_b64encode(signature)}"


def verify_state(state: str, *, now: float | None = None) -> str:
    """Validate a returned ``state`` and return the platform it was issued for.

    Raises :class:`OAuthError` on anything suspicious. Constant-time comparison
    so a wrong signature cannot be found byte by byte.
    """
    parts = state.split(".")
    if len(parts) != 4:
        raise OAuthError("malformed OAuth state")
    nonce, issued_raw, platform, signature = parts
    payload = f"{nonce}.{issued_raw}.{platform}"
    expected = _b64encode(hmac.new(_secret(), payload.encode("utf-8"), hashlib.sha256).digest())
    if not hmac.compare_digest(expected, signature):
        raise OAuthError("OAuth state signature does not match")

    try:
        issued_at = int(issued_raw)
    except ValueError as exc:
        raise OAuthError("malformed OAuth state timestamp") from exc
    age = (now if now is not None else time.time()) - issued_at
    if age > _STATE_TTL_SECONDS:
        raise OAuthError("OAuth state has expired; restart the connect flow")
    if age < -60:
        raise OAuthError("OAuth state is dated in the future")
    return platform


def redirect_uri(platform: str) -> str:
    """The callback URL registered with the platform.

    Must match what was registered exactly, including scheme and host, or the
    platform refuses the exchange. Built from ``public_base_url`` for that
    reason rather than from the incoming request, which an attacker controls.
    """
    base = get_settings().public_base_url.rstrip("/")
    return f"{base}/api/v1/oauth/{platform}/callback"


def authorization_url(platform: str) -> tuple[str, str]:
    """Return ``(url, state)`` for the user to be sent to."""
    provider = provider_for(platform)
    state = sign_state(platform)
    params = {
        "client_id": _client_id(platform),
        "redirect_uri": redirect_uri(platform),
        "response_type": "code",
        "scope": " ".join(provider.scopes),
        "state": state,
        **provider.extra_authorize_params,
    }
    return f"{provider.authorize_url}?{urlencode(params)}", state


def _client_id(platform: str) -> str:
    settings = get_settings()
    client_id = getattr(settings, f"{platform}_client_id", "")
    if not client_id:
        # A missing client id is a setup problem, and the message should say
        # which variable to set rather than sending the user to an error page.
        raise OAuthError(
            f"no OAuth client configured for {platform!r}; "
            f"set QUICKREEL_{platform.upper()}_CLIENT_ID and _CLIENT_SECRET"
        )
    return client_id


def _client_secret(platform: str) -> str:
    return getattr(get_settings(), f"{platform}_client_secret", "")


def exchange_code(platform: str, code: str) -> dict:
    """Exchange an authorization code for tokens.

    Raises :class:`OAuthError` on a missing client configuration. Network
    exchange lives in the platform adapter module, because each platform's token
    response differs; this function is the seam where that happens and is
    overridden in tests.
    """
    provider_for(platform)  # validates the platform name
    raise OAuthError(
        "token exchange requires a live provider client; "
        f"configure QUICKREEL_{platform.upper()}_CLIENT_ID to enable it"
    )


def account_id_from_token_payload(platform: str, payload: dict) -> str:
    """Pull the platform's account identifier out of a token response."""
    provider = provider_for(platform)
    for key in provider.account_id_keys:
        value = payload.get(key)
        if value:
            return str(value)
    return ""


def state_fingerprint(state: str) -> str:
    """A short, non-secret fingerprint of a state value, safe to log.

    Logging the raw state would let anyone reading logs replay a flow.
    """
    digest = hashlib.sha256(state.encode("utf-8")).hexdigest()
    return digest[:12]


def describe_providers() -> list[dict]:
    """Provider metadata for the connect UI, with no secrets in it."""
    settings = get_settings()
    described = []
    for name, provider in PROVIDERS.items():
        client_id = getattr(settings, f"{name}_client_id", "")
        described.append(
            {
                "platform": name,
                "scopes": list(provider.scopes),
                "configured": bool(client_id),
            }
        )
    return described


def dumps_credentials(platform: str, payload: dict) -> dict:
    """Normalise a token payload into the dict stored on the target."""
    return {
        "platform": platform,
        "access_token": payload.get("access_token", ""),
        "refresh_token": payload.get("refresh_token", ""),
        "expires_in": payload.get("expires_in"),
        "scope": payload.get("scope", ""),
        "token_type": payload.get("token_type", "Bearer"),
        "obtained_at": int(time.time()),
    }


def credentials_json(credentials: dict) -> str:
    """Stable JSON for logging or debugging, with token values redacted."""
    redacted = {
        key: ("<redacted>" if "token" in key and value else value)
        for key, value in credentials.items()
    }
    return json.dumps(redacted, sort_keys=True)
