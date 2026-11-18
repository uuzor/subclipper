"""End-to-end tests for URL ingestion.

A fake downloader stands in for yt-dlp and the OS resolver is stubbed, so these
tests are hermetic: no network access, no real platform. They exercise the real
route handlers, the real pipeline, and real ffmpeg.

The fake downloader is stubbed at the registry boundary
(``quickreel.media.downloader.set_downloader``), which is the same seam the
application uses to choose yt-dlp. The resolver is stubbed because DNS is
outside our process; every SSRF decision above it is real code.
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from quickreel.media.downloader import FakeDownloader, set_downloader

PUBLIC_IP = "93.184.216.34"  # a real, globally routable address (example.com)


@pytest.fixture()
def stub_resolver(monkeypatch):
    """Resolve every hostname to a public address."""

    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_IP, 0))]

    monkeypatch.setattr("quickreel.media.url_source.socket.getaddrinfo", fake_getaddrinfo)


@pytest.fixture()
def fake_downloader(sample_video: Path):
    """Install a FakeDownloader that copies ``sample_video`` into place."""
    downloader = FakeDownloader(source=sample_video)
    set_downloader(downloader)
    try:
        yield downloader
    finally:
        set_downloader(None)


YOUTUBE_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def _create_url_project(client, url: str = YOUTUBE_URL, title: str = "Remote clip") -> dict:
    response = client.post("/api/v1/projects", json={"title": title, "source_url": url})
    assert response.status_code == 201, response.text
    return response.json()


# --- project creation ---------------------------------------------------------


def test_create_project_with_source_url(client, stub_resolver):
    project = _create_url_project(client)
    assert project["source_kind"] == "url"
    assert project["source_url"] == YOUTUBE_URL
    assert project["status"] == "created"


def test_create_project_rejects_localhost_url(client, stub_resolver):
    response = client.post(
        "/api/v1/projects",
        json={"title": "bad", "source_url": "http://localhost:6379/"},
    )
    assert response.status_code == 403
    assert "not permitted" in response.json()["detail"]


def test_create_project_rejects_metadata_endpoint(client, stub_resolver):
    response = client.post(
        "/api/v1/projects",
        json={"title": "bad", "source_url": "http://169.254.169.254/latest/meta-data/"},
    )
    assert response.status_code == 403


def test_create_project_rejects_ftp_scheme(client, stub_resolver):
    # Pydantic's HttpUrl rejects non-http(s) schemes at request validation,
    # before our own validator runs. Either way the request must not succeed.
    response = client.post(
        "/api/v1/projects", json={"title": "bad", "source_url": "ftp://example.com/x"}
    )
    assert response.status_code == 422
    assert "ftp" in response.text.lower() or "scheme" in response.text.lower()


def test_url_ingestion_can_be_disabled(client, stub_resolver, monkeypatch):
    from quickreel.config import get_settings

    monkeypatch.setattr(get_settings(), "url_ingestion_enabled", False)
    response = client.post("/api/v1/projects", json={"title": "off", "source_url": YOUTUBE_URL})
    assert response.status_code == 403
    assert "disabled" in response.json()["detail"]


# --- attaching a URL to an existing project -----------------------------------


def test_set_source_url_on_existing_project(client, stub_resolver):
    created = client.post("/api/v1/projects", json={"title": "Later"}).json()
    project_id = created["id"]

    response = client.post(
        f"/api/v1/projects/{project_id}/source-url",
        json={"source_url": YOUTUBE_URL, "title": "Renamed"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source_kind"] == "url"
    assert body["source_url"] == YOUTUBE_URL
    assert body["title"] == "Renamed"
    assert body["media"] is not None  # placeholder asset created


def test_replacing_source_url_clears_previously_downloaded_file(
    client, stub_resolver, fake_downloader
):
    """Re-pointing a project at a new URL must re-download, not reuse old media."""
    project = _create_url_project(client)
    project_id = project["id"]
    assert client.post(f"/api/v1/projects/{project_id}/process").status_code == 202

    first_path = client.get(f"/api/v1/projects/{project_id}").json()["media"]["path"]
    assert first_path and Path(first_path).exists()

    other = "https://vimeo.com/12345"
    response = client.post(f"/api/v1/projects/{project_id}/source-url", json={"source_url": other})
    assert response.status_code == 200
    assert response.json()["media"]["path"] == ""
    assert not Path(first_path).exists(), "stale download should be removed"

    # Processing again downloads the new URL from scratch.
    fake_downloader.calls.clear()
    assert client.post(f"/api/v1/projects/{project_id}/process").status_code == 202
    assert other in fake_downloader.calls


def test_process_without_source_is_rejected(client):
    created = client.post("/api/v1/projects", json={"title": "Empty"}).json()
    response = client.post(f"/api/v1/projects/{created['id']}/process")
    assert response.status_code == 400
    assert "source" in response.json()["detail"]


# --- probe --------------------------------------------------------------------


def test_probe_returns_metadata_without_downloading(client, stub_resolver, fake_downloader):
    response = client.post("/api/v1/projects/source-url/probe", json={"source_url": YOUTUBE_URL})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["title"] == "Fake remote video"
    assert body["downloader"] == "fake-downloader"
    assert body["duration_seconds"] == 20.0


def test_probe_rejects_private_host(client, stub_resolver):
    response = client.post(
        "/api/v1/projects/source-url/probe", json={"source_url": "http://127.0.0.1/x"}
    )
    assert response.status_code == 403


# --- full pipeline ------------------------------------------------------------


def test_url_project_runs_full_pipeline(client, stub_resolver, fake_downloader):
    project = _create_url_project(client)
    project_id = project["id"]

    processed = client.post(f"/api/v1/projects/{project_id}/process")
    assert processed.status_code == 202, processed.text
    body = processed.json()
    assert body["status"] == "ready", body
    assert body["error"] is None

    # The downloader was actually invoked with the validated URL.
    assert YOUTUBE_URL in fake_downloader.calls

    detail = client.get(f"/api/v1/projects/{project_id}").json()
    # Ingest probed the downloaded file, so we have real media metadata.
    assert detail["media"]["duration_seconds"] > 0
    assert detail["media"]["width"] > 0

    # Download provenance is persisted on the asset (not exposed by the API).
    from quickreel.db import get_sessionmaker
    from quickreel.models import MediaAsset

    with get_sessionmaker()() as session:
        asset = session.query(MediaAsset).filter_by(project_id=project_id).one()
        assert asset.probe["download"]["extractor"] == "fake"

    clips = client.get(f"/api/v1/projects/{project_id}/clips").json()
    assert clips, "URL-sourced project should produce clips"
    assert all(clip["score"] >= 0 for clip in clips)


def test_url_project_renders_clip(client, stub_resolver, fake_downloader):
    project = _create_url_project(client)
    project_id = project["id"]
    assert client.post(f"/api/v1/projects/{project_id}/process").status_code == 202

    rendered = client.post(f"/api/v1/projects/{project_id}/render", json={"max_clips": 1})
    assert rendered.status_code == 200, rendered.text
    clip = rendered.json()[0]
    assert clip["status"] == "rendered"
    assert clip["render_meta"]["height"] == 1920

    download = client.get(f"/api/v1/clips/{clip['id']}/download")
    assert download.status_code == 200
    assert len(download.content) > 0


def test_download_failure_surfaces_as_pipeline_error(client, stub_resolver):
    """A downloader error must fail the project, not crash the request."""
    set_downloader(FakeDownloader(error="video is private"))
    try:
        project = _create_url_project(client)
        response = client.post(f"/api/v1/projects/{project['id']}/process")
        assert response.status_code == 422
        assert "could not download" in response.json()["detail"]

        detail = client.get(f"/api/v1/projects/{project['id']}").json()
        assert detail["status"] == "ingesting_failed"
        assert "private" in detail["error"]
    finally:
        set_downloader(None)


def test_delete_url_project_before_download(client, stub_resolver):
    """Deleting a URL project that has no file yet must not raise."""
    project = _create_url_project(client)
    response = client.delete(f"/api/v1/projects/{project['id']}")
    assert response.status_code == 204
    assert client.get(f"/api/v1/projects/{project['id']}").status_code == 404
