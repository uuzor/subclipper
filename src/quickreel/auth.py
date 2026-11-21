"""Email/password authentication and server-side sessions.

Three things here are security-relevant and easy to get wrong, so each is
isolated in its own function with the reasoning attached:

**Password storage.** Passwords are hashed with scrypt, which is memory-hard:
unlike a bare SHA-256 it cannot be attacked cheaply on a GPU. The parameters are
recorded in the stored string, so they can be raised later without invalidating
existing hashes — a hash written today still verifies after the cost is
increased, and is transparently re-hashed on the next successful login.

**Session tokens.** The token in the cookie is random, and only its SHA-256 is
stored. A leaked database therefore does not hand over live sessions, in the
same way that a leaked password table does not hand over passwords. Sessions are
rows rather than signed tokens so that logout and revocation actually work: a
JWT cannot be un-issued.

**User enumeration.** Login must not reveal whether an email is registered. A
missing account and a wrong password return the same error and take
approximately the same time, the latter by verifying against a dummy hash so the
response time does not give the answer away.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import threading
import time
from dataclasses import dataclass

from .logging_setup import get_logger

logger = get_logger(__name__)

# Cookie name carrying the session token. Kept here so the routes and the
# request dependency cannot drift apart.
SESSION_COOKIE = "quickreel_session"

# scrypt cost. n is the CPU/memory cost, r the block size, p the parallelism.
# n=2**14 with r=8 needs 128*n*r = 16 MB per hash, which is above the memory a
# GPU attacker wants to spend per guess and still fast enough for a login.
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_SALT_BYTES = 16

MIN_PASSWORD_LENGTH = 8
# Bounded so a huge body cannot be turned into a hashing denial of service.
MAX_PASSWORD_LENGTH = 1024
MAX_EMAIL_LENGTH = 254

# Deliberately permissive: the address is validated by sending to it, not by
# matching a grammar. This rejects what is obviously not an address without
# locking out the many valid addresses a stricter pattern would.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+(?:\.[^@\s.]+)+$")

# A real scrypt hash of a random value, used to spend the same time on a login
# for an unknown email as on one for a known email with the wrong password.
# Computed once at import rather than hardcoded so it cannot be mistaken for a
# credential.
_DUMMY_HASH = ""


class AuthError(RuntimeError):
    """A rejected credential. The message is safe to show a user."""


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


def normalize_email(email: str) -> str:
    """Fold an address to its stored form.

    Lowercased so ``Sam@Example.com`` and ``sam@example.com`` are one account
    rather than two, which is what a user expects and what stops a duplicate
    account being created by changing capitalisation.
    """
    return email.strip().lower()


def validate_email(email: str) -> str:
    normalized = normalize_email(email)
    if not normalized:
        raise AuthError("email is required")
    if len(normalized) > MAX_EMAIL_LENGTH:
        raise AuthError("email is too long")
    if not _EMAIL_RE.match(normalized):
        raise AuthError("that does not look like an email address")
    return normalized


def validate_password(password: str) -> str:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise AuthError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise AuthError("password is too long")
    if not password.strip():
        raise AuthError("password cannot be only whitespace")
    return password


def hash_password(password: str) -> str:
    """Hash a password into a self-describing string.

    Format: ``scrypt$<n>$<r>$<p>$<salt>$<hash>``. Storing the parameters means
    raising the cost later is a code change, not a migration: old hashes keep
    verifying and are upgraded on next login.
    """
    salt = secrets.token_bytes(_SALT_BYTES)
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64(salt)}${_b64(derived)}"


def verify_password(password: str, stored: str) -> bool:
    """Check a password against a stored hash, in constant time.

    Never raises: a malformed or truncated stored hash is a failed login, not a
    500. That matters because the alternative is a stored-value error leaking
    into an HTTP response.
    """
    try:
        scheme, n_raw, r_raw, p_raw, salt_raw, hash_raw = stored.split("$")
        if scheme != "scrypt":
            return False
        derived = hashlib.scrypt(
            password.encode("utf-8"),
            salt=_unb64(salt_raw),
            n=int(n_raw),
            r=int(r_raw),
            p=int(p_raw),
            dklen=len(_unb64(hash_raw)),
        )
        return hmac.compare_digest(derived, _unb64(hash_raw))
    except (ValueError, TypeError):
        return False


def needs_rehash(stored: str) -> bool:
    """Whether a stored hash was made with weaker parameters than we now use."""
    try:
        scheme, n_raw, r_raw, p_raw, _, _ = stored.split("$")
    except ValueError:
        return True
    if scheme != "scrypt":
        return True
    return (int(n_raw), int(r_raw), int(p_raw)) != (_SCRYPT_N, _SCRYPT_R, _SCRYPT_P)


def dummy_verify(password: str) -> None:
    """Spend the cost of a real verification without a real hash.

    Called when the email is unknown so that a login attempt cannot be timed to
    discover which addresses are registered.
    """
    global _DUMMY_HASH
    if not _DUMMY_HASH:
        _DUMMY_HASH = hash_password(secrets.token_urlsafe(32))
    verify_password(password, _DUMMY_HASH)


# ---- Session tokens ----


def new_session_token() -> str:
    """A fresh opaque session token. 32 bytes of entropy."""
    return secrets.token_urlsafe(32)


def hash_session_token(token: str) -> str:
    """The stored form of a session token.

    A plain SHA-256 is right here, unlike for passwords: the token is already
    high-entropy random, so there is nothing to brute-force and no reason to pay
    scrypt's cost on every authenticated request.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class Credentials:
    """A verified identity, as returned by :func:`authenticate`."""

    user_id: str
    workspace_id: str
    role: str


class LoginThrottle:
    """A per-key failed-login limiter.

    Deliberately in-process and in-memory: it is a brake on online guessing, not
    a distributed rate limiter. It resets on restart and is not shared between
    workers, so a deployment running several workers should also rate limit at
    the edge. It is still worth having, because the alternative for a
    single-process deployment is nothing at all.
    """

    def __init__(
        self,
        *,
        max_failures: int = 8,
        window_seconds: float = 300.0,
        lockout_seconds: float = 300.0,
    ) -> None:
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self.lockout_seconds = lockout_seconds
        self._failures: dict[str, list[float]] = {}
        self._locked_until: dict[str, float] = {}
        self._lock = threading.Lock()

    def retry_after(self, key: str, *, now: float | None = None) -> float:
        """Seconds the caller must wait, or 0 when the attempt may proceed."""
        now = now if now is not None else time.monotonic()
        with self._lock:
            until = self._locked_until.get(key)
            if until is None:
                return 0.0
            if until <= now:
                self._locked_until.pop(key, None)
                self._failures.pop(key, None)
                return 0.0
            return until - now

    def record_failure(self, key: str, *, now: float | None = None) -> None:
        now = now if now is not None else time.monotonic()
        with self._lock:
            stamps = [t for t in self._failures.get(key, []) if now - t < self.window_seconds]
            stamps.append(now)
            self._failures[key] = stamps
            if len(stamps) >= self.max_failures:
                self._locked_until[key] = now + self.lockout_seconds
                logger.warning("auth.throttled", key=key, failures=len(stamps))

    def record_success(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)
            self._locked_until.pop(key, None)

    def reset(self) -> None:
        """Drop all state. Used by tests so one case cannot lock out another."""
        with self._lock:
            self._failures.clear()
            self._locked_until.clear()


# One shared instance for the process. The routes import this rather than
# constructing their own, so the limit applies across requests.
login_throttle = LoginThrottle()
