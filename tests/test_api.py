"""End-to-end API tests: upload -> process -> clips -> render -> download.

These run the real pipeline with the deterministic fake AI providers and real
ffmpeg rendering, so they cover the wiring between every layer.
"""

from __future__ import annotations

from pathlib import Path


def _create_and_upload(auth_client, sample_video: Path) -> dict:
    created = auth_client.post("/api/v1/projects", json={"title": "Podcast episode"})
    assert created.status_code == 201, created.text
    project_id = created.json()["id"]

    with sample_video.open("rb") as handle:
        response = auth_client.post(
            f"/api/v1/projects/{project_id}/upload",
            files={"file": ("sample.mp4", handle, "video/mp4")},
        )
    assert response.status_code == 200, response.text
    return response.json()


def test_health_endpoints(auth_client):
    assert auth_client.get("/healthz").json() == {"status": "ok"}
    ready = auth_client.get("/readyz").json()
    assert ready["status"] == "ok"
    assert set(ready["providers"]) == {"transcription", "vision", "moment"}


def test_upload_rejects_unknown_project(auth_client, sample_video):
    with sample_video.open("rb") as handle:
        response = auth_client.post(
            "/api/v1/projects/does-not-exist/upload",
            files={"file": ("sample.mp4", handle, "video/mp4")},
        )
    assert response.status_code == 404


def test_full_pipeline_produces_ranked_clips(auth_client, sample_video):
    project = _create_and_upload(auth_client, sample_video)
    project_id = project["id"]

    processed = auth_client.post(f"/api/v1/projects/{project_id}/process")
    assert processed.status_code == 202, processed.text
    body = processed.json()
    assert body["status"] == "ready", body

    clips = auth_client.get(f"/api/v1/projects/{project_id}/clips").json()
    assert clips, "pipeline should produce candidate clips"

    ranks = [clip["rank"] for clip in clips]
    assert ranks == sorted(ranks)
    scores = [clip["score"] for clip in clips]
    assert scores == sorted(scores, reverse=True)
    for clip in clips:
        assert 0.0 <= clip["score"] <= 100.0
        assert clip["end_seconds"] > clip["start_seconds"]
        assert clip["score_breakdown"]["components"]

    transcript = auth_client.get(f"/api/v1/projects/{project_id}").json()["transcript"]
    assert transcript is not None
    assert transcript["segments"]

    # Which provider served each stage must be recorded on the project, so a
    # degraded run is auditable after the fact.
    detail = auth_client.get(f"/api/v1/projects/{project_id}").json()
    assert set(detail["providers"]) == {"transcription", "audio", "vision", "moment"}


def test_render_and_download_top_clip(auth_client, sample_video):
    project = _create_and_upload(auth_client, sample_video)
    project_id = project["id"]
    assert auth_client.post(f"/api/v1/projects/{project_id}/process").status_code == 202

    rendered = auth_client.post(
        f"/api/v1/projects/{project_id}/render",
        json={"max_clips": 1, "burn_captions": True},
    )
    assert rendered.status_code == 200, rendered.text
    clips = rendered.json()
    assert len(clips) == 1
    assert clips[0]["status"] == "rendered"

    download = auth_client.get(f"/api/v1/clips/{clips[0]['id']}/download")
    assert download.status_code == 200
    assert download.headers["content-type"] == "video/mp4"
    assert len(download.content) > 0


def test_download_before_render_returns_conflict(auth_client, sample_video):
    project = _create_and_upload(auth_client, sample_video)
    project_id = project["id"]
    assert auth_client.post(f"/api/v1/projects/{project_id}/process").status_code == 202
    clip_id = auth_client.get(f"/api/v1/projects/{project_id}/clips").json()[0]["id"]

    response = auth_client.get(f"/api/v1/clips/{clip_id}/download")
    assert response.status_code == 409


def test_clip_filters(auth_client, sample_video):
    project = _create_and_upload(auth_client, sample_video)
    project_id = project["id"]
    assert auth_client.post(f"/api/v1/projects/{project_id}/process").status_code == 202

    high_only = auth_client.get(f"/api/v1/projects/{project_id}/clips?min_score=90").json()
    assert all(clip["score"] >= 90 for clip in high_only)


def test_process_requires_media(auth_client):
    from quickreel.db import get_sessionmaker
    from quickreel.models import Project

    # The project has to be in the caller's workspace, or the request is a 404
    # (not the caller's project) rather than the 400 this test is about.
    workspace_id = auth_client.get("/api/v1/auth/me").json()["workspace"]["id"]
    session = get_sessionmaker()()
    try:
        project = Project(title="Empty", workspace_id=workspace_id)
        session.add(project)
        session.commit()
        project_id = project.id
    finally:
        session.close()

    response = auth_client.post(f"/api/v1/projects/{project_id}/process")
    assert response.status_code == 400


def test_delete_project_removes_clips(auth_client, sample_video):
    project = _create_and_upload(auth_client, sample_video)
    project_id = project["id"]
    assert auth_client.post(f"/api/v1/projects/{project_id}/process").status_code == 202

    assert auth_client.delete(f"/api/v1/projects/{project_id}").status_code == 204
    assert auth_client.get(f"/api/v1/projects/{project_id}").status_code == 404


def test_reprocess_replaces_renders_without_orphaning_files(auth_client, sample_video):
    """Re-processing must delete the old rendered MP4s, not leave them on disk."""
    project = _create_and_upload(auth_client, sample_video)
    project_id = project["id"]
    assert auth_client.post(f"/api/v1/projects/{project_id}/process").status_code == 202
    rendered = auth_client.post(
        f"/api/v1/projects/{project_id}/render",
        json={"max_clips": 1}).json(
    )
    first_render = Path(rendered[0]["render_path"])
    assert first_render.exists()

    # Re-run the pipeline; the previous clip rows and their files are replaced.
    assert auth_client.post(f"/api/v1/projects/{project_id}/process").status_code == 202
    assert not first_render.exists(), "stale render should be removed on re-process"


def test_render_honours_vertical_flag(auth_client, sample_video):
    from quickreel.media import ffmpeg

    project = _create_and_upload(auth_client, sample_video)
    project_id = project["id"]
    assert auth_client.post(f"/api/v1/projects/{project_id}/process").status_code == 202

    # Render the same clip in each orientation. The output path is derived from
    # the clip id, so probe straight away before the next render overwrites it.
    vertical = auth_client.post(
        f"/api/v1/projects/{project_id}/render",
        json={"max_clips": 1, "vertical": True},
    ).json()[0]
    assert vertical["render_meta"]["orientation"] == "vertical"
    assert (vertical["render_meta"]["width"], vertical["render_meta"]["height"]) == (1080, 1920)
    v_info = ffmpeg.probe(vertical["render_path"])
    assert (v_info.width, v_info.height) == (1080, 1920)

    horizontal = auth_client.post(
        f"/api/v1/projects/{project_id}/render",
        json={"max_clips": 1, "vertical": False},
    ).json()[0]
    assert horizontal["render_meta"]["orientation"] == "horizontal"
    assert (horizontal["render_meta"]["width"], horizontal["render_meta"]["height"]) == (1920, 1080)
    h_info = ffmpeg.probe(horizontal["render_path"])
    assert (h_info.width, h_info.height) == (1920, 1080)


def test_render_hands_off_to_a_thread_when_no_worker_exists(
    auth_client, sample_video, settings
):
    """A render must not run inside the request when it can be avoided.

    An edge proxy kills a long request at its own timeout and answers 502 while
    ffmpeg keeps working, so the caller sees failure for a render that
    succeeds. With no broker there is no worker, so the render runs in a thread
    and the clip is polled instead. The test waits for the real thread to finish
    rather than asserting a mock was called.
    """
    import time

    project = _create_and_upload(auth_client, sample_video)
    project_id = project["id"]
    assert auth_client.post(f"/api/v1/projects/{project_id}/process").status_code == 202

    settings.render_in_background = True
    try:
        response = auth_client.post(
            f"/api/v1/projects/{project_id}/render", json={"max_clips": 1}
        )
    finally:
        settings.render_in_background = False
    assert response.status_code == 200, response.text
    clip_id = response.json()[0]["id"]
    # The response describes work that has been started, not work that is done.
    assert response.json()[0]["status"] == "rendering"

    deadline = time.time() + 120
    while time.time() < deadline:
        clips = auth_client.get(f"/api/v1/projects/{project_id}/clips").json()
        clip = next(c for c in clips if c["id"] == clip_id)
        if clip["status"] in {"rendered", "render_failed"}:
            break
        time.sleep(0.5)

    assert clip["status"] == "rendered", clip
    download = auth_client.get(f"/api/v1/clips/{clip_id}/download")
    assert download.status_code == 200
    assert len(download.content) > 0


def test_process_hands_off_to_a_thread_when_no_worker_exists(
    auth_client, sample_video, settings
):
    """Analysis must not run inside the request when it can be avoided.

    Transcribing and analysing a long video takes far longer than a hosting
    proxy's request timeout, and the proxy answers 502 while the pipeline keeps
    working. Off-request, the project reports ``processing`` and the client
    polls, so the test waits for the real pipeline rather than mocking it.
    """
    import time

    project = _create_and_upload(auth_client, sample_video)
    project_id = project["id"]

    settings.process_in_background = True
    try:
        response = auth_client.post(f"/api/v1/projects/{project_id}/process")
    finally:
        settings.process_in_background = False

    assert response.status_code == 202, response.text
    assert response.json()["status"] in {"ingesting", "transcribing", "analyzing"}

    deadline = time.time() + 120
    while time.time() < deadline:
        detail = auth_client.get(f"/api/v1/projects/{project_id}").json()
        if detail["status"] in {"ready", "analyze_failed", "transcribe_failed"}:
            break
        time.sleep(0.5)

    assert detail["status"] == "ready", detail
    assert detail["clips"]


def test_chunked_upload_assembles_file_in_order(auth_client, sample_video):
    """Chunks must reassemble byte-for-byte into the original file.

    A proxy in front of the app caps the request body (Cloudflare rejects
    anything over 100MiB with an HTML 413 that never reaches the app), so long
    recordings are sent in pieces. If the pieces are assembled wrong the pipeline
    later fails on a corrupt container, which is much harder to diagnose.
    """
    payload = sample_video.read_bytes()
    created = auth_client.post("/api/v1/projects", json={"title": "Chunked"})
    project_id = created.json()["id"]

    init = auth_client.post(
        f"/api/v1/projects/{project_id}/upload/init",
        json={"filename": "sample.mp4", "total_bytes": len(payload)},
    )
    assert init.status_code == 200, init.text
    upload_id = init.json()["upload_id"]
    chunk_size = init.json()["chunk_size"]
    assert chunk_size > 0

    offset = 0
    while offset < len(payload):
        piece = payload[offset : offset + chunk_size]
        response = auth_client.post(
            f"/api/v1/projects/{project_id}/upload/{upload_id}/chunk?offset={offset}",
            files={"file": ("chunk", piece, "application/octet-stream")},
        )
        assert response.status_code == 200, response.text
        assert response.json()["received_bytes"] == offset + len(piece)
        offset += len(piece)

    done = auth_client.post(
        f"/api/v1/projects/{project_id}/upload/{upload_id}/complete",
        json={"filename": "sample.mp4", "total_bytes": len(payload)},
    )
    assert done.status_code == 200, done.text
    stored = Path(done.json()["media"]["path"])
    assert stored.read_bytes() == payload
    assert done.json()["media"]["size_bytes"] == len(payload)


def test_chunk_retry_at_same_offset_does_not_duplicate(auth_client, sample_video):
    """A retried chunk must overwrite, not append.

    Clients cannot tell a dropped response from a dropped request, so they retry
    the chunk they think failed. Appending twice would silently corrupt the
    container while every request still returns 200.
    """
    created = auth_client.post("/api/v1/projects", json={"title": "Retry"})
    project_id = created.json()["id"]
    init = auth_client.post(
        f"/api/v1/projects/{project_id}/upload/init",
        json={"filename": "sample.mp4", "total_bytes": 12},
    )
    upload_id = init.json()["upload_id"]

    first = auth_client.post(
        f"/api/v1/projects/{project_id}/upload/{upload_id}/chunk?offset=0",
        files={"file": ("chunk", b"hello", "application/octet-stream")},
    )
    assert first.json()["received_bytes"] == 5

    retry = auth_client.post(
        f"/api/v1/projects/{project_id}/upload/{upload_id}/chunk?offset=0",
        files={"file": ("chunk", b"hello", "application/octet-stream")},
    )
    assert retry.status_code == 200, retry.text
    assert retry.json()["received_bytes"] == 5, "a retried chunk must not append"


def test_chunk_offset_past_end_is_rejected(auth_client):
    """An offset beyond what the server holds means the client lost track.

    Accepting it would leave a hole in the file, so the client has to re-check
    the status and resend from a known offset.
    """
    created = auth_client.post("/api/v1/projects", json={"title": "Gap"})
    project_id = created.json()["id"]
    init = auth_client.post(
        f"/api/v1/projects/{project_id}/upload/init",
        json={"filename": "sample.mp4", "total_bytes": 100},
    )
    upload_id = init.json()["upload_id"]

    response = auth_client.post(
        f"/api/v1/projects/{project_id}/upload/{upload_id}/chunk?offset=50",
        files={"file": ("chunk", b"data", "application/octet-stream")},
    )
    assert response.status_code == 409, response.text


def test_chunked_upload_rejects_oversized_file_up_front(auth_client, monkeypatch):
    """An oversized file must be refused before any bytes are transferred."""
    from quickreel.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "max_upload_mb", 1)
    created = auth_client.post("/api/v1/projects", json={"title": "Too big"})
    project_id = created.json()["id"]

    response = auth_client.post(
        f"/api/v1/projects/{project_id}/upload/init",
        json={"filename": "huge.mp4", "total_bytes": 5 * 1024 * 1024},
    )
    assert response.status_code == 413, response.text


def test_chunked_upload_can_resume_after_interruption(auth_client, sample_video):
    """A client that drops mid-upload must be able to learn where it stopped.

    Without a truthful total, a resumed client cannot tell a half-sent file from
    a complete one, and re-sends everything or declares success too early.
    """
    payload = sample_video.read_bytes()
    created = auth_client.post("/api/v1/projects", json={"title": "Resume"})
    project_id = created.json()["id"]
    init = auth_client.post(
        f"/api/v1/projects/{project_id}/upload/init",
        json={"filename": "sample.mp4", "total_bytes": len(payload)},
    )
    upload_id = init.json()["upload_id"]
    chunk_size = init.json()["chunk_size"]

    # The fixture is smaller than one chunk, so send a deliberately short first
    # piece to simulate a connection that dropped mid-file.
    first = payload[: max(1, len(payload) // 3)]
    auth_client.post(
        f"/api/v1/projects/{project_id}/upload/{upload_id}/chunk?offset=0",
        files={"file": ("chunk", first, "application/octet-stream")},
    )

    status = auth_client.get(f"/api/v1/projects/{project_id}/upload/{upload_id}")
    assert status.status_code == 200, status.text
    body = status.json()
    assert body["received_bytes"] == len(first)
    assert body["total_bytes"] == len(payload), "the declared total must survive a resume"
    assert body["complete"] is False

    offset = body["received_bytes"]
    while offset < len(payload):
        piece = payload[offset : offset + chunk_size]
        response = auth_client.post(
            f"/api/v1/projects/{project_id}/upload/{upload_id}/chunk?offset={offset}",
            files={"file": ("chunk", piece, "application/octet-stream")},
        )
        offset = response.json()["received_bytes"]

    done = auth_client.post(
        f"/api/v1/projects/{project_id}/upload/{upload_id}/complete",
        json={"filename": "sample.mp4", "total_bytes": len(payload)},
    )
    assert done.status_code == 200, done.text
    assert Path(done.json()["media"]["path"]).read_bytes() == payload


def test_chunked_upload_feeds_the_pipeline(auth_client, sample_video):
    """A chunk-uploaded file must be indistinguishable from a single-shot upload.

    The point of the chunk path is to get large files past the proxy, not to
    introduce a second, subtly different kind of media asset.
    """
    payload = sample_video.read_bytes()
    created = auth_client.post("/api/v1/projects", json={"title": "Chunked pipeline"})
    project_id = created.json()["id"]
    init = auth_client.post(
        f"/api/v1/projects/{project_id}/upload/init",
        json={"filename": "sample.mp4", "total_bytes": len(payload)},
    )
    upload_id = init.json()["upload_id"]
    chunk_size = init.json()["chunk_size"]

    offset = 0
    while offset < len(payload):
        piece = payload[offset : offset + chunk_size]
        response = auth_client.post(
            f"/api/v1/projects/{project_id}/upload/{upload_id}/chunk?offset={offset}",
            files={"file": ("chunk", piece, "application/octet-stream")},
        )
        offset = response.json()["received_bytes"]
    auth_client.post(
        f"/api/v1/projects/{project_id}/upload/{upload_id}/complete",
        json={"filename": "sample.mp4", "total_bytes": len(payload)},
    )

    processed = auth_client.post(f"/api/v1/projects/{project_id}/process")
    assert processed.status_code == 202, processed.text
    assert processed.json()["status"] == "ready", processed.json()
    assert auth_client.get(f"/api/v1/projects/{project_id}/clips").json()
