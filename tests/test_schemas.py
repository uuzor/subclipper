"""Schema-level regression tests for legacy and derived response fields."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from quickreel.models import Project
from quickreel.schemas import ClipOut, ProjectOut

_NOW = datetime.now(UTC)


def _project(**overrides) -> dict:
    base = dict(
        id="p1",
        title="Legacy",
        source_kind="upload",
        source_url=None,
        status="ready",
        error=None,
        providers={},
        created_at=_NOW,
        updated_at=_NOW,
    )
    base.update(overrides)
    return base


def _clip(**overrides) -> dict:
    base = dict(
        id="c1",
        project_id="p1",
        start_seconds=10.0,
        end_seconds=25.5,
        raw_start_seconds=9.0,
        raw_end_seconds=27.0,
        title="Title",
        hook="Hook",
        transcript_excerpt="Excerpt",
        moment_type="revelation",
        rationale="Why",
        score=61.2,
        rank=1,
        status="candidate",
    )
    base.update(overrides)
    return base


def test_project_null_providers_becomes_empty_dict():
    """Legacy rows read back providers = NULL; the response must not 500."""
    assert ProjectOut.model_validate(_project(providers=None)).providers == {}


def test_clip_duration_derived_when_not_stored():
    """Clip has no duration column, so duration comes from end - start."""
    assert ClipOut.model_validate(_clip()).duration_seconds == 15.5


def test_clip_duration_prefers_supplied_value():
    out = ClipOut.model_validate(_clip(duration_seconds=12.0))
    assert out.duration_seconds == 12.0


def test_clip_null_json_columns_become_empty_dicts():
    out = ClipOut.model_validate(
        _clip(score_components=None, score_breakdown=None, render_meta=None)
    )
    assert out.score_components == {}
    assert out.score_breakdown == {}
    assert out.render_meta == {}


def test_project_list_endpoint_survives_null_providers(auth_client):
    """End-to-end guard: a NULL-providers row must not break the list route."""
    from sqlalchemy import text

    from quickreel.db import get_sessionmaker

    # The row must belong to the caller's workspace to appear in the listing;
    # what is under test here is the NULL-providers coercion, not tenancy.
    workspace_id = auth_client.get("/api/v1/auth/me").json()["workspace"]["id"]
    project_id = f"legacy{uuid.uuid4().hex}"
    session = get_sessionmaker()()
    try:
        session.add(Project(id=project_id, title="Legacy", workspace_id=workspace_id))
        session.commit()
        session.execute(
            text("UPDATE projects SET providers = NULL WHERE id = :id"),
            {"id": project_id},
        )
        session.commit()
    finally:
        session.close()

    response = auth_client.get("/api/v1/projects")
    assert response.status_code == 200, response.text
    legacy = next(p for p in response.json() if p["id"] == project_id)
    assert legacy["providers"] == {}
