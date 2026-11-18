"""Shared pytest fixtures.

The suite runs the deterministic fake providers against a real SQLite database
and real ffmpeg, so the full pipeline is exercised end to end without network
access or model weights.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

# Configure the app *before* importing it, since settings are cached.
#
# These are assigned, not defaulted with setdefault. A developer with
# QUICKREEL_MOMENT_PROVIDER=llm exported in their shell would otherwise run the
# suite against real providers, so the same commit could pass on CI and fail on
# a laptop — or worse, pass locally while a fake-only test hid a real
# regression. The suite owns its configuration; the shell does not get a vote.
_TMP_ROOT = Path(__file__).parent / ".tmp"
_TMP_ROOT.mkdir(exist_ok=True)

_TEST_ENV = {
    "QUICKREEL_ENV": "test",
    "QUICKREEL_DATA_DIR": str(_TMP_ROOT / "data"),
    "QUICKREEL_DATABASE_URL": f"sqlite+pysqlite:///{_TMP_ROOT}/test.db",
    "QUICKREEL_TRANSCRIPTION_PROVIDER": "fake",
    "QUICKREEL_VISION_PROVIDER": "fake",
    "QUICKREEL_MOMENT_PROVIDER": "fake",
    "QUICKREEL_TASK_ALWAYS_EAGER": "true",
    "QUICKREEL_PUBLISHING_MODE": "mock",
    "QUICKREEL_FRONTEND_DIR": "",
}
os.environ.update(_TEST_ENV)

# Every QUICKREEL_ variable the shell might be carrying, so a stray export
# cannot leak into a test. Anything set above is kept.
_SHELL_LEAKED_QUICKREEL_VARS = [
    name for name in os.environ if name.startswith("QUICKREEL_") and name not in _TEST_ENV
]


@pytest.fixture(scope="session", autouse=True)
def _isolate_settings_from_shell():
    """Strip QUICKREEL_* the developer's shell exported for the whole session.

    Done once, before any settings are built, because ``get_settings`` caches.
    """
    for name in _SHELL_LEAKED_QUICKREEL_VARS:
        os.environ.pop(name, None)
    yield


@pytest.fixture(scope="session")
def settings():
    from quickreel.config import get_settings

    return get_settings()


@pytest.fixture()
def db_session():
    from quickreel.db import get_sessionmaker, init_db, reset_engine

    reset_engine()
    init_db()
    session = get_sessionmaker()()
    try:
        yield session
    finally:
        session.close()
        reset_engine()


@pytest.fixture()
def sample_video(tmp_path: Path) -> Path:
    """Generate a small, real MP4 with a speech-like tone track using ffmpeg."""
    target = tmp_path / "sample.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=640x360:rate=25:duration=20",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=220:duration=20",
            "-shortest",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(target),
        ],
        check=True,
        capture_output=True,
    )
    return target


@pytest.fixture()
def client(settings):
    """An unauthenticated client. For auth itself and for 401 assertions."""
    from fastapi.testclient import TestClient

    from quickreel.db import init_db, reset_engine
    from quickreel.main import create_app

    reset_engine()
    init_db()
    # An https base URL so that a Secure session cookie is actually sent back.
    # httpx will not attach a Secure cookie over http, which would make every
    # authenticated test fail for a reason unrelated to what it is testing.
    with TestClient(create_app(), base_url="https://testserver") as test_client:
        yield test_client
    reset_engine()


# Credentials for the signed-in fixture. The email is unique per test so that a
# persistent test database does not turn the second run into a 409.
AUTH_PASSWORD = "correct-horse-battery"


@pytest.fixture()
def auth_client(client):
    """A client with a signed-up, logged-in user.

    Content endpoints require a session, so tests that exercise them need an
    account. Signing up through the real endpoint rather than inserting a user
    directly keeps the tests honest: if signup or login breaks, every test that
    uses this fixture fails, instead of the suite passing while the front door
    is broken.
    """
    import uuid

    from quickreel import auth

    auth.login_throttle.reset()
    email = f"creator-{uuid.uuid4().hex[:12]}@example.com"
    response = client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": AUTH_PASSWORD, "workspace_name": "Test workspace"},
    )
    assert response.status_code == 201, response.text
    client.auth_email = email
    return client


@pytest.fixture()
def second_auth_client(client):
    """A second, independent signed-in user in their own workspace.

    Used to prove tenant isolation: two accounts, two workspaces, and no way for
    one to see or touch the other's content. It reuses the engine the ``client``
    fixture set up rather than resetting it, so both clients see the same
    database while keeping separate cookie jars.
    """
    import uuid

    from fastapi.testclient import TestClient

    from quickreel import auth
    from quickreel.main import create_app

    auth.login_throttle.reset()
    with TestClient(create_app(), base_url="https://testserver") as other:
        response = other.post(
            "/api/v1/auth/signup",
            json={
                "email": f"other-{uuid.uuid4().hex[:12]}@example.com",
                "password": "another-correct-horse",
                "workspace_name": "Other workspace",
            },
        )
        assert response.status_code == 201, response.text
        yield other


@pytest.fixture()
def fake_transcript():
    from quickreel.ai.base import TranscriptResult, TranscriptSegment

    return TranscriptResult(
        language="en",
        text="",
        provider="unit",
        segments=[
            TranscriptSegment(0.0, 5.0, "So tell me about the beginning of the company.", "S1"),
            TranscriptSegment(5.0, 12.0, "The biggest mistake I made was trusting everyone.", "S2"),
            TranscriptSegment(12.0, 18.0, "I gave away equity because I wanted to be liked.", "S2"),
            TranscriptSegment(18.0, 25.0, "That nearly killed the business, honestly.", "S2"),
            TranscriptSegment(25.0, 31.0, "What did you learn from that experience?", "S1"),
        ],
    )
