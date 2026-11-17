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
_TMP_ROOT = Path(__file__).parent / ".tmp"
_TMP_ROOT.mkdir(exist_ok=True)

os.environ.setdefault("QUICKREEL_ENV", "test")
os.environ.setdefault("QUICKREEL_DATA_DIR", str(_TMP_ROOT / "data"))
os.environ.setdefault("QUICKREEL_DATABASE_URL", f"sqlite+pysqlite:///{_TMP_ROOT}/test.db")
os.environ.setdefault("QUICKREEL_TRANSCRIPTION_PROVIDER", "fake")
os.environ.setdefault("QUICKREEL_VISION_PROVIDER", "fake")
os.environ.setdefault("QUICKREEL_MOMENT_PROVIDER", "fake")
os.environ.setdefault("QUICKREEL_TASK_ALWAYS_EAGER", "true")


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
    from fastapi.testclient import TestClient

    from quickreel.db import init_db, reset_engine
    from quickreel.main import create_app

    reset_engine()
    init_db()
    with TestClient(create_app()) as test_client:
        yield test_client
    reset_engine()


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
