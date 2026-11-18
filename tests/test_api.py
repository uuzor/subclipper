"""End-to-end API tests: upload -> process -> clips -> render -> download.

These run the real pipeline with the deterministic fake AI providers and real
ffmpeg rendering, so they cover the wiring between every layer.
"""

from __future__ import annotations

from pathlib import Path


def _create_and_upload(client, sample_video: Path) -> dict:
    created = client.post("/api/v1/projects", json={"title": "Podcast episode"})
    assert created.status_code == 201, created.text
    project_id = created.json()["id"]

    with sample_video.open("rb") as handle:
        response = client.post(
            f"/api/v1/projects/{project_id}/upload",
            files={"file": ("sample.mp4", handle, "video/mp4")},
        )
    assert response.status_code == 200, response.text
    return response.json()


def test_health_endpoints(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    ready = client.get("/readyz").json()
    assert ready["status"] == "ok"
    assert set(ready["providers"]) == {"transcription", "vision", "moment"}


def test_upload_rejects_unknown_project(client, sample_video):
    with sample_video.open("rb") as handle:
        response = client.post(
            "/api/v1/projects/does-not-exist/upload",
            files={"file": ("sample.mp4", handle, "video/mp4")},
        )
    assert response.status_code == 404


def test_full_pipeline_produces_ranked_clips(client, sample_video):
    project = _create_and_upload(client, sample_video)
    project_id = project["id"]

    processed = client.post(f"/api/v1/projects/{project_id}/process")
    assert processed.status_code == 202, processed.text
    body = processed.json()
    assert body["status"] == "ready", body

    clips = client.get(f"/api/v1/projects/{project_id}/clips").json()
    assert clips, "pipeline should produce candidate clips"

    ranks = [clip["rank"] for clip in clips]
    assert ranks == sorted(ranks)
    scores = [clip["score"] for clip in clips]
    assert scores == sorted(scores, reverse=True)
    for clip in clips:
        assert 0.0 <= clip["score"] <= 100.0
        assert clip["end_seconds"] > clip["start_seconds"]
        assert clip["score_breakdown"]["components"]

    transcript = client.get(f"/api/v1/projects/{project_id}").json()["transcript"]
    assert transcript is not None
    assert transcript["segments"]


def test_render_and_download_top_clip(client, sample_video):
    project = _create_and_upload(client, sample_video)
    project_id = project["id"]
    assert client.post(f"/api/v1/projects/{project_id}/process").status_code == 202

    rendered = client.post(
        f"/api/v1/projects/{project_id}/render",
        json={"max_clips": 1, "burn_captions": True},
    )
    assert rendered.status_code == 200, rendered.text
    clips = rendered.json()
    assert len(clips) == 1
    assert clips[0]["status"] == "rendered"

    download = client.get(f"/api/v1/clips/{clips[0]['id']}/download")
    assert download.status_code == 200
    assert download.headers["content-type"] == "video/mp4"
    assert len(download.content) > 0


def test_download_before_render_returns_conflict(client, sample_video):
    project = _create_and_upload(client, sample_video)
    project_id = project["id"]
    assert client.post(f"/api/v1/projects/{project_id}/process").status_code == 202
    clip_id = client.get(f"/api/v1/projects/{project_id}/clips").json()[0]["id"]

    response = client.get(f"/api/v1/clips/{clip_id}/download")
    assert response.status_code == 409


def test_clip_filters(client, sample_video):
    project = _create_and_upload(client, sample_video)
    project_id = project["id"]
    assert client.post(f"/api/v1/projects/{project_id}/process").status_code == 202

    high_only = client.get(f"/api/v1/projects/{project_id}/clips?min_score=90").json()
    assert all(clip["score"] >= 90 for clip in high_only)


def test_process_requires_media(client):
    from quickreel.db import get_sessionmaker
    from quickreel.models import Project

    session = get_sessionmaker()()
    try:
        project = Project(title="Empty")
        session.add(project)
        session.commit()
        project_id = project.id
    finally:
        session.close()

    response = client.post(f"/api/v1/projects/{project_id}/process")
    assert response.status_code == 400


def test_delete_project_removes_clips(client, sample_video):
    project = _create_and_upload(client, sample_video)
    project_id = project["id"]
    assert client.post(f"/api/v1/projects/{project_id}/process").status_code == 202

    assert client.delete(f"/api/v1/projects/{project_id}").status_code == 204
    assert client.get(f"/api/v1/projects/{project_id}").status_code == 404


def test_reprocess_replaces_renders_without_orphaning_files(client, sample_video):
    """Re-processing must delete the old rendered MP4s, not leave them on disk."""
    project = _create_and_upload(client, sample_video)
    project_id = project["id"]
    assert client.post(f"/api/v1/projects/{project_id}/process").status_code == 202
    rendered = client.post(f"/api/v1/projects/{project_id}/render", json={"max_clips": 1}).json()
    first_render = Path(rendered[0]["render_path"])
    assert first_render.exists()

    # Re-run the pipeline; the previous clip rows and their files are replaced.
    assert client.post(f"/api/v1/projects/{project_id}/process").status_code == 202
    assert not first_render.exists(), "stale render should be removed on re-process"


def test_render_honours_vertical_flag(client, sample_video):
    from quickreel.media import ffmpeg

    project = _create_and_upload(client, sample_video)
    project_id = project["id"]
    assert client.post(f"/api/v1/projects/{project_id}/process").status_code == 202

    # Render the same clip in each orientation. The output path is derived from
    # the clip id, so probe straight away before the next render overwrites it.
    vertical = client.post(
        f"/api/v1/projects/{project_id}/render",
        json={"max_clips": 1, "vertical": True},
    ).json()[0]
    assert vertical["render_meta"]["orientation"] == "vertical"
    assert (vertical["render_meta"]["width"], vertical["render_meta"]["height"]) == (1080, 1920)
    v_info = ffmpeg.probe(vertical["render_path"])
    assert (v_info.width, v_info.height) == (1080, 1920)

    horizontal = client.post(
        f"/api/v1/projects/{project_id}/render",
        json={"max_clips": 1, "vertical": False},
    ).json()[0]
    assert horizontal["render_meta"]["orientation"] == "horizontal"
    assert (horizontal["render_meta"]["width"], horizontal["render_meta"]["height"]) == (1920, 1080)
    h_info = ffmpeg.probe(horizontal["render_path"])
    assert (h_info.width, h_info.height) == (1920, 1080)
