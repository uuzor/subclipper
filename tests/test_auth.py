"""Authentication, sessions and tenant isolation.

Two things are being checked here and they fail in very different ways.

**Authentication** fails loudly: a wrong password is a 401 and someone notices.
So the tests go past the happy path to the properties that matter — that a
missing account and a wrong password are indistinguishable, that a password is
never stored in a form that can be read back, and that a cookie cannot be forged
or reused after logout.

**Tenancy** fails quietly, which is what makes it dangerous. A cross-tenant leak
returns 200 with someone else's data, and nothing in the logs looks wrong. So
every isolation test is written as "user B asks for user A's object by its real
id and must get a 404", covering each content type rather than one.
"""

from __future__ import annotations

import time
import uuid

import pytest

from quickreel import auth
from quickreel.models import User, UserSession, Workspace

# The minimum the signup endpoint accepts.
PASSWORD = "a-good-enough-password"


@pytest.fixture()
def mail():
    """A factory for unique addresses.

    The test database persists between runs, so a fixed address would be a 409
    on the second run. Every address is minted fresh instead.
    """

    def make(local: str = "user") -> str:
        return f"{local}-{uuid.uuid4().hex[:12]}@example.com"

    return make


def _signup(client, email: str, password: str = PASSWORD, **extra):
    return client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": password, **extra},
    )


def _login(client, email: str, password: str = PASSWORD):
    return client.post("/api/v1/auth/login", json={"email": email, "password": password})


def _user_id(email: str) -> str:
    """Look up a user's id. The test database persists across tests, so queries
    here are always scoped to one account rather than assuming a single row."""
    from quickreel.db import get_sessionmaker

    session = get_sessionmaker()()
    try:
        return session.query(User).filter_by(email=email).one().id
    finally:
        session.close()


# ---- Password hashing ----


def test_a_password_is_never_stored_in_the_clear():
    stored = auth.hash_password(PASSWORD)
    assert PASSWORD not in stored
    assert stored.startswith("scrypt$")


def test_the_same_password_hashes_differently_each_time():
    """A per-password salt, so equal passwords are not visibly equal."""
    assert auth.hash_password(PASSWORD) != auth.hash_password(PASSWORD)


def test_a_correct_password_verifies():
    assert auth.verify_password(PASSWORD, auth.hash_password(PASSWORD))


def test_a_wrong_password_does_not_verify():
    assert not auth.verify_password("not-the-password", auth.hash_password(PASSWORD))


def test_a_corrupt_stored_hash_fails_closed_rather_than_raising():
    """A damaged row is a failed login, not a 500 with a traceback in it."""
    for broken in ("", "not-a-hash", "scrypt$broken", "bcrypt$1$2$3$4$5"):
        assert auth.verify_password(PASSWORD, broken) is False


def test_weak_parameters_are_flagged_for_rehash():
    """A hash made with lower cost is upgraded on next login, not silently kept."""
    assert auth.needs_rehash(auth.hash_password(PASSWORD)) is False
    assert auth.needs_rehash("scrypt$2$8$1$c2FsdA==$aGFzaA==") is True
    assert auth.needs_rehash("something-else-entirely") is True


# ---- Email handling ----


def test_email_is_normalised_so_case_does_not_create_two_accounts(auth_client, mail):
    address = mail("mixed.case")
    created = _signup(auth_client, address.upper())
    assert created.status_code == 201, created.text
    assert created.json()["user"]["email"] == address.lower()

    duplicate = _signup(auth_client, address.lower())
    assert duplicate.status_code == 409


def test_an_obviously_invalid_email_is_rejected(auth_client, mail):
    for bad in ("no-at-sign", "no@tld", "@example.com", "a b@example.com"):
        assert _signup(auth_client, bad).status_code == 422, bad
    assert _signup(auth_client, mail("ok")).status_code == 201


def test_a_short_password_is_rejected(auth_client, mail):
    assert _signup(auth_client, mail("short"), password="abc").status_code == 422


# ---- Signup and login ----


def test_signup_returns_a_session_and_sets_an_httponly_cookie(auth_client, mail):
    address = mail("new")
    response = _signup(auth_client, address)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["user"]["email"] == address
    assert body["workspace"]["id"]
    assert body["role"] == "owner"
    assert len(body["workspaces"]) == 1

    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "samesite=lax" in cookie.lower()


def test_login_succeeds_with_the_right_password(auth_client, mail):
    address = mail("login")
    _signup(auth_client, address)
    auth_client.post("/api/v1/auth/logout")
    response = _login(auth_client, address)
    assert response.status_code == 200, response.text
    assert response.json()["user"]["email"] == address


def test_login_is_case_insensitive_on_the_email(auth_client, mail):
    address = mail("case")
    _signup(auth_client, address)
    auth_client.post("/api/v1/auth/logout")
    assert _login(auth_client, address.upper()).status_code == 200


def test_an_unknown_email_and_a_wrong_password_are_indistinguishable(auth_client, mail):
    """The core anti-enumeration property: one status, one message."""
    address = mail("real")
    _signup(auth_client, address)
    auth_client.post("/api/v1/auth/logout")

    unknown = _login(auth_client, mail("nobody"), "whatever-password")
    wrong = _login(auth_client, address, "whatever-password")

    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json()["detail"] == wrong.json()["detail"]


def test_repeated_failures_are_throttled(auth_client, mail):
    """Online guessing is slowed after a handful of failures."""
    email = mail("throttle")
    _signup(auth_client, email)
    auth_client.post("/api/v1/auth/logout")

    statuses = [_login(auth_client, email, "wrong-password").status_code for _ in range(10)]
    assert 429 in statuses, statuses
    assert statuses.index(429) >= 5  # not so eager that a typo locks you out


def test_a_disabled_account_cannot_log_in(auth_client, mail):
    address = mail("disabled")
    _signup(auth_client, address)
    auth_client.post("/api/v1/auth/logout")

    from quickreel.db import get_sessionmaker

    session = get_sessionmaker()()
    try:
        user = session.query(User).filter_by(email=address).one()
        user.is_active = False
        session.commit()
    finally:
        session.close()

    assert _login(auth_client, address).status_code == 401


# ---- Sessions ----


def test_me_requires_a_session(client):
    assert client.get("/api/v1/auth/me").status_code == 401


def test_me_returns_the_current_identity(auth_client, mail):
    address = mail("me")
    _signup(auth_client, address)
    body = auth_client.get("/api/v1/auth/me").json()
    assert body["user"]["email"] == address
    assert body["workspace"]["name"]


def test_a_forged_cookie_is_not_a_session(client):
    """A random token must not authenticate, and must not raise a 500."""
    client.cookies.set(auth.SESSION_COOKIE, "not-a-real-token")
    assert client.get("/api/v1/auth/me").status_code == 401


def test_logout_ends_the_session_immediately(auth_client, mail):
    _signup(auth_client, mail("logout"))
    assert auth_client.get("/api/v1/auth/me").status_code == 200

    assert auth_client.post("/api/v1/auth/logout").status_code == 204
    assert auth_client.get("/api/v1/auth/me").status_code == 401


def test_logout_is_idempotent(client):
    assert client.post("/api/v1/auth/logout").status_code == 204
    assert client.post("/api/v1/auth/logout").status_code == 204


def test_logging_out_invalidates_the_token_server_side(auth_client, mail):
    """Deleting the row is what makes logout real; a stale cookie must not work."""
    response = _signup(auth_client, mail("revoke"))
    token = auth_client.cookies.get(auth.SESSION_COOKIE)
    assert token

    auth_client.post("/api/v1/auth/logout")

    # Replay the exact cookie that was issued before logging out.
    auth_client.cookies.set(auth.SESSION_COOKIE, token)
    assert auth_client.get("/api/v1/auth/me").status_code == 401
    assert response.status_code == 201


def test_an_expired_session_is_refused_and_removed(auth_client, mail):
    email = mail("expire")
    _signup(auth_client, email)
    token = auth_client.cookies.get(auth.SESSION_COOKIE)
    user_id = _user_id(email)

    from datetime import UTC, datetime, timedelta

    from quickreel.db import get_sessionmaker

    session = get_sessionmaker()()
    try:
        record = session.query(UserSession).filter_by(user_id=user_id).one()
        record.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()
    finally:
        session.close()

    assert auth_client.get("/api/v1/auth/me").status_code == 401

    session = get_sessionmaker()()
    try:
        # The dead row is cleaned up rather than left to accumulate.
        assert session.query(UserSession).filter_by(user_id=user_id).count() == 0
    finally:
        session.close()
    assert token


def test_only_the_token_hash_is_stored(auth_client, mail):
    """A database dump must not yield usable session tokens."""
    email = mail("hashed")
    _signup(auth_client, email)
    token = auth_client.cookies.get(auth.SESSION_COOKIE)

    from quickreel.db import get_sessionmaker

    session = get_sessionmaker()()
    try:
        record = session.query(UserSession).filter_by(user_id=_user_id(email)).one()
        assert record.token_hash != token
        assert record.token_hash == auth.hash_session_token(token)
    finally:
        session.close()


def test_changing_the_password_signs_every_session_out(auth_client, mail):
    """A password change is the remedy for a suspected compromise, so it must
    not leave the old sessions live."""
    address = mail("rotate")
    _signup(auth_client, address)

    response = auth_client.post(
        "/api/v1/auth/password",
        json={"current_password": PASSWORD, "new_password": "a-brand-new-password"},
    )
    assert response.status_code == 204, response.text

    assert auth_client.get("/api/v1/auth/me").status_code == 401
    assert _login(auth_client, address, "a-brand-new-password").status_code == 200
    assert _login(auth_client, address, PASSWORD).status_code == 401


def test_changing_the_password_requires_the_current_one(auth_client, mail):
    _signup(auth_client, mail("nocurrent"))
    response = auth_client.post(
        "/api/v1/auth/password",
        json={"current_password": "not-it", "new_password": "a-brand-new-password"},
    )
    assert response.status_code == 422
    assert auth_client.get("/api/v1/auth/me").status_code == 200


# ---- Workspaces ----


def test_signup_creates_a_workspace_owned_by_the_new_user(auth_client, mail):
    body = _signup(auth_client, mail("owner")).json()
    from quickreel.db import get_sessionmaker

    session = get_sessionmaker()()
    try:
        workspace = session.get(Workspace, body["workspace"]["id"])
        assert workspace is not None
    finally:
        session.close()
    assert body["role"] == "owner"


def test_a_user_can_switch_to_a_workspace_they_belong_to(auth_client, mail):
    from quickreel.auth_service import create_workspace
    from quickreel.db import get_sessionmaker

    address = mail("multi")
    _signup(auth_client, address)
    session = get_sessionmaker()()
    try:
        user = session.query(User).filter_by(email=address).one()
        second = create_workspace(session, user, name="Second workspace")
        second_id = second.id
    finally:
        session.close()

    response = auth_client.post("/api/v1/auth/workspace", json={"workspace_id": second_id})
    assert response.status_code == 200, response.text
    assert response.json()["workspace"]["id"] == second_id
    assert auth_client.get("/api/v1/auth/me").json()["workspace"]["id"] == second_id


def test_a_user_cannot_switch_into_a_workspace_they_do_not_belong_to(auth_client, mail):
    """Otherwise switching tenants is a matter of guessing an id."""
    _signup(auth_client, mail("outsider"))

    from quickreel.db import get_sessionmaker

    session = get_sessionmaker()()
    try:
        stranger = Workspace(name="Someone else's workspace")
        session.add(stranger)
        session.commit()
        stranger_id = stranger.id
    finally:
        session.close()

    response = auth_client.post("/api/v1/auth/workspace", json={"workspace_id": stranger_id})
    assert response.status_code == 404
    # The session is unchanged.
    assert auth_client.get("/api/v1/auth/me").json()["workspace"]["id"] != stranger_id


# ---- Tenant isolation ----


def test_one_workspace_cannot_list_anothers_projects(auth_client, second_auth_client):
    created = auth_client.post("/api/v1/projects", json={"title": "A's project"})
    assert created.status_code == 201, created.text

    theirs = second_auth_client.get("/api/v1/projects").json()
    assert all(p["id"] != created.json()["id"] for p in theirs)


def test_reading_another_workspaces_project_is_a_404(auth_client, second_auth_client):
    project_id = auth_client.post("/api/v1/projects", json={"title": "Private"}).json()["id"]
    response = second_auth_client.get(f"/api/v1/projects/{project_id}")
    assert response.status_code == 404


def test_processing_another_workspaces_project_is_a_404(auth_client, second_auth_client):
    project_id = auth_client.post("/api/v1/projects", json={"title": "Private"}).json()["id"]
    assert second_auth_client.post(f"/api/v1/projects/{project_id}/process").status_code == 404


def test_deleting_another_workspaces_project_is_a_404(auth_client, second_auth_client):
    project_id = auth_client.post("/api/v1/projects", json={"title": "Private"}).json()["id"]
    assert second_auth_client.delete(f"/api/v1/projects/{project_id}").status_code == 404
    # And it is still there for its owner.
    assert auth_client.get(f"/api/v1/projects/{project_id}").status_code == 200


def test_uploading_to_another_workspaces_project_is_a_404(
    auth_client, second_auth_client, sample_video
):
    project_id = auth_client.post("/api/v1/projects", json={"title": "Private"}).json()["id"]
    with sample_video.open("rb") as handle:
        response = second_auth_client.post(
            f"/api/v1/projects/{project_id}/upload",
            files={"file": ("sample.mp4", handle, "video/mp4")},
        )
    assert response.status_code == 404


def test_a_clip_is_not_visible_across_workspaces(auth_client, second_auth_client, sample_video):
    """Clips inherit their project's tenant, so the clip id alone is not enough."""
    project = auth_client.post("/api/v1/projects", json={"title": "Mine"}).json()
    with sample_video.open("rb") as handle:
        auth_client.post(
            f"/api/v1/projects/{project['id']}/upload",
            files={"file": ("sample.mp4", handle, "video/mp4")},
        )
    assert auth_client.post(f"/api/v1/projects/{project['id']}/process").status_code == 202
    clips = auth_client.get(f"/api/v1/projects/{project['id']}/clips").json()
    assert clips, "the pipeline should produce clips"

    clip_id = clips[0]["id"]
    assert second_auth_client.get(f"/api/v1/clips/{clip_id}").status_code == 404
    assert second_auth_client.get(f"/api/v1/clips/{clip_id}/download").status_code == 404
    # Listing another workspace's clips through its project id is also refused.
    assert second_auth_client.get(f"/api/v1/projects/{project['id']}/clips").status_code == 404


def test_publish_targets_are_per_workspace(auth_client, second_auth_client):
    created = auth_client.post(
        "/api/v1/publish-targets",
        json={"platform": "tiktok", "display_name": "Mine", "external_account_id": "acct-1"},
    )
    assert created.status_code == 201, created.text
    target_id = created.json()["id"]

    assert second_auth_client.get("/api/v1/publish-targets").json() == []
    assert second_auth_client.delete(f"/api/v1/publish-targets/{target_id}").status_code == 404


def test_channel_subscriptions_are_per_workspace(auth_client, second_auth_client):
    channel = "UCuAXFkgsw1L7xaCfnd5JJOw"
    created = auth_client.post(
        "/api/v1/triggers/youtube/subscriptions",
        json={"channel_id": channel, "authorized_by": "me"},
    )
    assert created.status_code == 201, created.text
    subscription_id = created.json()["id"]

    assert second_auth_client.get("/api/v1/triggers/youtube/subscriptions").json() == []
    assert (
        second_auth_client.delete(
            f"/api/v1/triggers/youtube/subscriptions/{subscription_id}"
        ).status_code
        == 404
    )


def test_scheduled_posts_are_per_workspace(auth_client, second_auth_client, sample_video):
    """A post is reached through its clip's project, which carries the tenant."""
    project = auth_client.post("/api/v1/projects", json={"title": "Mine"}).json()
    with sample_video.open("rb") as handle:
        auth_client.post(
            f"/api/v1/projects/{project['id']}/upload",
            files={"file": ("sample.mp4", handle, "video/mp4")},
        )
    auth_client.post(f"/api/v1/projects/{project['id']}/process")
    clip_id = auth_client.get(f"/api/v1/projects/{project['id']}/clips").json()[0]["id"]
    auth_client.post(
        f"/api/v1/projects/{project['id']}/render", json={"max_clips": 1, "burn_captions": False}
    )
    target_id = auth_client.post(
        "/api/v1/publish-targets",
        json={"platform": "tiktok", "external_account_id": "acct-1"},
    ).json()["id"]
    post = auth_client.post(
        "/api/v1/scheduled-posts",
        json={
            "clip_id": clip_id,
            "target_id": target_id,
            "scheduled_for": "2030-01-01T00:00:00Z",
        },
    )
    assert post.status_code == 201, post.text
    post_id = post.json()["id"]

    assert second_auth_client.get("/api/v1/scheduled-posts").json() == []
    assert second_auth_client.get(f"/api/v1/scheduled-posts/{post_id}").status_code == 404
    assert (
        second_auth_client.post(f"/api/v1/scheduled-posts/{post_id}/publish-now").status_code == 404
    )
    assert (
        second_auth_client.get(f"/api/v1/scheduled-posts/{post_id}/attempts").status_code == 404
    )


def test_scheduling_another_workspaces_clip_is_a_404(auth_client, second_auth_client, sample_video):
    """The clip is validated against the caller's tenant before the post exists."""
    project = auth_client.post("/api/v1/projects", json={"title": "Mine"}).json()
    with sample_video.open("rb") as handle:
        auth_client.post(
            f"/api/v1/projects/{project['id']}/upload",
            files={"file": ("sample.mp4", handle, "video/mp4")},
        )
    auth_client.post(f"/api/v1/projects/{project['id']}/process")
    clip_id = auth_client.get(f"/api/v1/projects/{project['id']}/clips").json()[0]["id"]

    target_id = second_auth_client.post(
        "/api/v1/publish-targets",
        json={"platform": "tiktok", "external_account_id": "acct-b"},
    ).json()["id"]
    response = second_auth_client.post(
        "/api/v1/scheduled-posts",
        json={
            "clip_id": clip_id,
            "target_id": target_id,
            "scheduled_for": "2030-01-01T00:00:00Z",
        },
    )
    assert response.status_code == 404


def test_rendering_another_workspaces_clip_by_id_is_refused(
    auth_client, second_auth_client, sample_video
):
    """Passing a foreign clip id into the render call must not render it."""
    project = auth_client.post("/api/v1/projects", json={"title": "Mine"}).json()
    with sample_video.open("rb") as handle:
        auth_client.post(
            f"/api/v1/projects/{project['id']}/upload",
            files={"file": ("sample.mp4", handle, "video/mp4")},
        )
    auth_client.post(f"/api/v1/projects/{project['id']}/process")
    clip_id = auth_client.get(f"/api/v1/projects/{project['id']}/clips").json()[0]["id"]

    own_project = second_auth_client.post("/api/v1/projects", json={"title": "Theirs"}).json()
    response = second_auth_client.post(
        f"/api/v1/projects/{own_project['id']}/render",
        json={"clip_ids": [clip_id], "burn_captions": False},
    )
    # The foreign clip is not in this project, so there is nothing to render.
    assert response.status_code == 400


def test_an_oauth_flow_started_in_one_workspace_files_the_account_there(
    auth_client, second_auth_client, monkeypatch
):
    """The tenant travels in the signed state, not from the request.

    The callback is a cross-site redirect, so the workspace it files the account
    under has to be one the server itself signed, not one the caller supplied.
    """
    from quickreel.config import get_settings

    monkeypatch.setenv("QUICKREEL_TIKTOK_CLIENT_ID", "client-123")
    get_settings.cache_clear()

    workspace_id = auth_client.get("/api/v1/auth/me").json()["workspace"]["id"]
    started = auth_client.get("/api/v1/oauth/tiktok/start").json()
    assert workspace_id in started["state"]

    import quickreel.api.routes.oauth as oauth_routes

    original = oauth_routes.oauth.exchange_code
    oauth_routes.oauth.exchange_code = lambda platform, code: {
        "access_token": "t",
        "open_id": "acct-42",
        "expires_in": 100,
    }
    try:
        auth_client.get(
            f"/api/v1/oauth/tiktok/callback?code=c&state={started['state']}",
            follow_redirects=False,
        )
    finally:
        oauth_routes.oauth.exchange_code = original

    assert len(auth_client.get("/api/v1/publish-targets").json()) == 1
    assert second_auth_client.get("/api/v1/publish-targets").json() == []


# ---- Legacy data ----


def test_rows_predating_tenancy_are_adopted_rather_than_orphaned():
    """A row with no workspace would be invisible to every scoped query.

    That is worse than a delete because it looks like data loss, so the backfill
    adopts such rows into one deterministic legacy workspace.
    """
    from sqlalchemy import text

    from quickreel.db import get_sessionmaker, init_db, reset_engine
    from quickreel.models import Project

    project_id = "legacy" + uuid.uuid4().hex[:26]
    reset_engine()
    init_db()
    session = get_sessionmaker()()
    try:
        session.add(Project(id=project_id, title="Old row"))
        session.commit()
        session.execute(
            text("UPDATE projects SET workspace_id = NULL WHERE id = :id"),
            {"id": project_id},
        )
        session.commit()
    finally:
        session.close()

    # The next startup runs the backfill.
    reset_engine()
    init_db()

    session = get_sessionmaker()()
    try:
        project = session.get(Project, project_id)
        assert project.workspace_id is not None
        workspace = session.get(Workspace, project.workspace_id)
        assert workspace.name == "Legacy workspace"
    finally:
        session.close()

    # Running it again must be a no-op, not a second legacy workspace.
    reset_engine()
    init_db()
    session = get_sessionmaker()()
    try:
        assert session.query(Workspace).filter_by(name="Legacy workspace").count() == 1
    finally:
        session.close()

    # The adopted row is now visible to a scoped query for its workspace.
    session = get_sessionmaker()()
    try:
        project = session.get(Project, project_id)
        from sqlalchemy import select

        found = session.scalars(
            select(Project).where(Project.workspace_id == project.workspace_id)
        ).all()
        assert project_id in {p.id for p in found}
    finally:
        session.close()


# ---- Throttle unit behaviour ----


def test_the_throttle_unlocks_after_its_window():
    throttle = auth.LoginThrottle(max_failures=2, window_seconds=10, lockout_seconds=30)
    now = 1000.0
    throttle.record_failure("k", now=now)
    assert throttle.retry_after("k", now=now) == 0
    throttle.record_failure("k", now=now)
    assert throttle.retry_after("k", now=now) > 0
    assert throttle.retry_after("k", now=now + 31) == 0


def test_a_success_clears_accumulated_failures():
    throttle = auth.LoginThrottle(max_failures=3)
    now = 1000.0
    for _ in range(2):
        throttle.record_failure("k", now=now)
    throttle.record_success("k")
    assert throttle.retry_after("k", now=now) == 0


def test_the_throttle_does_not_let_a_stale_window_lock_a_user_out():
    """Failures outside the window must not accumulate toward a lockout."""
    throttle = auth.LoginThrottle(max_failures=3, window_seconds=10, lockout_seconds=30)
    now = 1000.0
    for i in range(5):
        throttle.record_failure("k", now=now + i * 20)
    assert throttle.retry_after("k", now=now + 100) == 0


def test_the_dummy_verify_costs_roughly_a_real_verification():
    """The anti-timing defence only works if it is not obviously cheaper.

    A generous bound: this is asserting the dummy is a real scrypt call, not
    that the two are within microseconds of each other.
    """
    stored = auth.hash_password(PASSWORD)

    start = time.monotonic()
    auth.verify_password(PASSWORD, stored)
    real = time.monotonic() - start

    start = time.monotonic()
    auth.dummy_verify(PASSWORD)
    dummy = time.monotonic() - start

    assert dummy > real / 10


@pytest.mark.parametrize(
    "endpoint",
    [
        ("get", "/api/v1/projects"),
        ("post", "/api/v1/projects"),
        ("get", "/api/v1/publish-targets"),
        ("get", "/api/v1/scheduled-posts"),
        ("post", "/api/v1/metrics/run-due"),
        ("get", "/api/v1/clips/anything/metrics"),
        ("get", "/api/v1/clips/anything/performance"),
        ("post", "/api/v1/clips/anything/metrics/collect"),
        ("get", "/api/v1/triggers/youtube/subscriptions"),
        ("get", "/api/v1/triggers/youtube/events"),
        ("get", "/api/v1/triggers/youtube/hub"),
        ("get", "/api/v1/auth/me"),
    ],
)
def test_content_endpoints_reject_an_anonymous_caller(client, endpoint):
    """Every content route is closed to a caller with no session."""
    method, path = endpoint
    response = getattr(client, method)(path)
    assert response.status_code == 401, f"{method.upper()} {path} -> {response.status_code}"
