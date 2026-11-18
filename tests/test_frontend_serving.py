"""Static frontend serving: one origin for the API and the built SPA.

The deployment sets QUICKREEL_FRONTEND_DIR to the Vite output so a single
uvicorn process serves both. These tests build a fake dist directory and check
the routing rules that keep the API and the SPA from shadowing each other.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture()
def dist(tmp_path: Path) -> Path:
    """A minimal stand-in for the Vite build output."""
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><div id=root>shell</div>")
    (root / "assets" / "app.js").write_text("console.log('app')")
    return root


def _client(monkeypatch, frontend_dir: Path | None):
    """Build an app with a specific frontend_dir, bypassing the cached settings."""
    from fastapi.testclient import TestClient

    from quickreel import config
    from quickreel.db import init_db, reset_engine

    monkeypatch.setenv("QUICKREEL_FRONTEND_DIR", str(frontend_dir) if frontend_dir else "")
    config.get_settings.cache_clear()
    reset_engine()
    init_db()
    from quickreel.main import create_app

    return TestClient(create_app())


def test_root_serves_spa_shell(monkeypatch, dist):
    with _client(monkeypatch, dist) as client:
        response = client.get("/")
    assert response.status_code == 200
    assert "id=root" in response.text


def test_client_route_falls_back_to_shell(monkeypatch, dist):
    """A deep link the SPA owns must not 404 on a hard refresh."""
    with _client(monkeypatch, dist) as client:
        response = client.get("/projects/abc123")
    assert response.status_code == 200
    assert "id=root" in response.text


def test_assets_are_served(monkeypatch, dist):
    with _client(monkeypatch, dist) as client:
        response = client.get("/assets/app.js")
    assert response.status_code == 200
    assert "console.log" in response.text


def test_api_still_reachable(monkeypatch, dist):
    with _client(monkeypatch, dist) as client:
        assert client.get("/healthz").json() == {"status": "ok"}


def test_api_404_stays_json_not_shell(monkeypatch, dist):
    """An unknown API path must return the API's JSON 404, not index.html."""
    with _client(monkeypatch, dist) as client:
        response = client.get("/api/v1/projects/does-not-exist")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert "id=root" not in response.text


def test_unknown_api_prefix_is_not_swallowed_by_shell(monkeypatch, dist):
    """A path under /api/ that no router matches must not become the SPA shell.

    This is the case the mount order actually gets wrong: when no router matches
    at all, the request reaches the catch-all mount, which would otherwise answer
    with index.html and a 200, hiding a broken client request.
    """
    with _client(monkeypatch, dist) as client:
        response = client.get("/api/v1/definitely-not-a-route")
    assert response.status_code == 404
    assert "id=root" not in response.text


def test_serving_disabled_by_default(monkeypatch):
    """With no frontend dir the app is API-only; / has no route."""
    with _client(monkeypatch, None) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/").status_code == 404


def test_missing_build_warns_and_keeps_api(monkeypatch, tmp_path):
    """A configured-but-unbuilt directory must not crash the server."""
    empty = tmp_path / "not-built"
    empty.mkdir()
    with _client(monkeypatch, empty) as client:
        assert client.get("/healthz").status_code == 200
