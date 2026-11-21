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


def test_render_reports_missing_source_instead_of_hanging(auth_client, sample_video):
    """A render whose source video is gone must surface an error on the clip.

    On a host without a persistent volume the upload disappears on restart while
    the database row survives, so ffmpeg fails with "No such file or directory".
    That failure used to reach only the server log, leaving the UI polling a
    spinner forever with nothing to show the user.
    """
    project = _create_and_upload(auth_client, sample_video)
    project_id = project["id"]
    assert auth_client.post(f"/api/v1/projects/{project_id}/process").status_code == 202

    media_path = Path(project["media"]["path"])
    assert media_path.is_file()
    media_path.unlink()

    response = auth_client.post(
        f"/api/v1/projects/{project_id}/render",
        json={"max_clips": 1, "burn_captions": True},
    )
    # A synchronous render reports the failure in the response itself...
    assert response.status_code == 500
    assert "No such file or directory" in response.json()["detail"]

    # ...and the clip keeps the reason, so the page can show it after a reload
    # even when nobody was watching the request.
    clip = auth_client.get(f"/api/v1/projects/{project_id}/clips").json()[0]
    assert clip["status"] == "render_failed"
    assert "No such file or directory" in clip["render_error"]


def test_interrupted_render_becomes_retryable(auth_client, sample_video):
    """A clip stuck in ``rendering`` after a crash is reset at the next boot.

    Rendering runs in a thread inside the web process, so an OOM kill leaves the
    row mid-flight. Nothing can be rendering at startup, so the sweep marks it
    failed with a reason rather than leaving the page spinning.
    """
    from quickreel.db import session_scope
    from quickreel.models import Clip, ClipStatus
    from quickreel.rendering.renderer import recover_interrupted_renders

    project = _create_and_upload(auth_client, sample_video)
    project_id = project["id"]
    assert auth_client.post(f"/api/v1/projects/{project_id}/process").status_code == 202
    clip_id = auth_client.get(f"/api/v1/projects/{project_id}/clips").json()[0]["id"]

    with session_scope() as session:
        clip = session.get(Clip, clip_id)
        clip.status = ClipStatus.rendering

    assert recover_interrupted_renders() >= 1

    clip = auth_client.get(f"/api/v1/projects/{project_id}/clips").json()[0]
    assert clip["status"] == "render_failed"
    assert "interrupted" in (clip["render_error"] or "")


def test_download_before_render_returns_conflict(auth_client, sample_video, no_auto_render):
    project = _create_and_upload(auth_client, sample_video)
    project_id = project["id"]
    assert auth_client.post(f"/api/v1/projects/{project_id}/process").status_code == 202
    clip_id = auth_client.get(f"/api/v1/projects/{project_id}/clips").json()[0]["id"]

    response = auth_client.get(f"/api/v1/clips/{clip_id}/download")
    assert response.status_code == 409


def test_stream_clip_serves_inline_playable_video(auth_client, sample_video):
    """The UI plays clips with a <video> tag, which needs an inline response and
    range support. The download route sends Content-Disposition: attachment and
    would be saved rather than played.
    """
    project = _create_and_upload(auth_client, sample_video)
    project_id = project["id"]
    assert auth_client.post(f"/api/v1/projects/{project_id}/process").status_code == 202
    rendered = auth_client.post(
        f"/api/v1/projects/{project_id}/render",
        json={"max_clips": 1, "burn_captions": True},
    )
    assert rendered.status_code == 200, rendered.text
    clip_id = rendered.json()[0]["id"]

    video = auth_client.get(f"/api/v1/clips/{clip_id}/video")
    assert video.status_code == 200
    assert video.headers["content-type"] == "video/mp4"
    assert video.headers["content-disposition"].startswith("inline")
    assert len(video.content) > 0

    # Browsers, Safari especially, probe with a Range request before playing.
    partial = auth_client.get(
        f"/api/v1/clips/{clip_id}/video", headers={"Range": "bytes=0-99"}
    )
    assert partial.status_code == 206
    assert partial.headers["content-range"].startswith("bytes 0-99/")
    assert len(partial.content) == 100
    assert partial.content == video.content[:100]


def test_stream_clip_before_render_returns_conflict(auth_client, sample_video, no_auto_render):
    project = _create_and_upload(auth_client, sample_video)
    project_id = project["id"]
    assert auth_client.post(f"/api/v1/projects/{project_id}/process").status_code == 202
    clip_id = auth_client.get(f"/api/v1/projects/{project_id}/clips").json()[0]["id"]

    assert auth_client.get(f"/api/v1/clips/{clip_id}/video").status_code == 409


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


def test_clip_reports_when_its_render_file_is_gone(auth_client, sample_video):
    """A rendered clip whose MP4 vanished must not look playable.

    Ephemeral storage keeps the row across a restart and loses the file, which
    left the page pointing <video> at a 409 and painting an empty box.
    """
    project = _create_and_upload(auth_client, sample_video)
    project_id = project["id"]
    assert auth_client.post(f"/api/v1/projects/{project_id}/process").status_code == 202
    clip = auth_client.post(
        f"/api/v1/projects/{project_id}/render", json={"max_clips": 1}
    ).json()[0]
    assert clip["status"] == "rendered"
    assert clip["render_available"] is True

    Path(clip["render_path"]).unlink()

    detail = auth_client.get(f"/api/v1/projects/{project_id}").json()
    assert detail["clips"][0]["status"] == "rendered"
    assert detail["clips"][0]["render_available"] is False
    assert auth_client.get(f"/api/v1/clips/{clip['id']}/video").status_code == 409


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
        settled = all(c["status"] != "rendering" for c in detail["clips"])
        if detail["status"] in {"ready", "analyze_failed", "transcribe_failed"} and settled:
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


def test_render_accepts_a_platform_and_exports_that_canvas(auth_client, sample_video):
    """Naming a platform must change the real output, not just the metadata."""
    from quickreel.media import ffmpeg

    project = _create_and_upload(auth_client, sample_video)
    project_id = project["id"]
    assert auth_client.post(f"/api/v1/projects/{project_id}/process").status_code == 202

    square = auth_client.post(
        f"/api/v1/projects/{project_id}/render",
        json={"max_clips": 1, "platform": "square"},
    ).json()[0]

    assert square["render_meta"]["platform"] == "square"
    assert (square["render_meta"]["width"], square["render_meta"]["height"]) == (1080, 1080)
    info = ffmpeg.probe(square["render_path"])
    assert (info.width, info.height) == (1080, 1080)

    # Switching platform must not leave the previous render in place: the file
    # name is platform-specific so both exports can coexist.
    reels = auth_client.post(
        f"/api/v1/projects/{project_id}/render",
        json={"max_clips": 1, "platform": "instagram_reels"},
    ).json()[0]
    assert reels["render_meta"]["platform"] == "instagram_reels"
    reels_info = ffmpeg.probe(reels["render_path"])
    assert (reels_info.width, reels_info.height) == (1080, 1920)


def test_render_rejects_an_unknown_platform_before_touching_clips(
    auth_client, sample_video
):
    """A bad platform must fail the request outright.

    Validating after the clips are claimed would leave them marked ``rendering``
    with nothing rendering them, so they would never settle.
    """
    project = _create_and_upload(auth_client, sample_video)
    project_id = project["id"]
    assert auth_client.post(f"/api/v1/projects/{project_id}/process").status_code == 202

    response = auth_client.post(
        f"/api/v1/projects/{project_id}/render",
        json={"max_clips": 1, "platform": "myspace"},
    )
    assert response.status_code == 400
    assert "myspace" in response.json()["detail"]

    clips = auth_client.get(f"/api/v1/projects/{project_id}/clips").json()
    assert all(clip["status"] != "rendering" for clip in clips)


def test_export_platforms_lists_the_available_targets(auth_client):
    response = auth_client.get("/api/v1/export-platforms")
    assert response.status_code == 200
    platforms = {item["key"]: item for item in response.json()}
    assert {"tiktok", "youtube_shorts", "instagram_reels", "youtube"} <= set(platforms)
    assert platforms["tiktok"]["aspect"] == "1080x1920"
    assert platforms["youtube"]["aspect"] == "1920x1080"


def test_process_renders_the_top_moments_without_being_asked(auth_client, sample_video):
    """Analysis alone must not leave a screen of "not rendered" cards.

    Moments start life as candidate rows, and a candidate renders as an empty
    player. The pipeline is expected to turn the best few into playable clips so
    a finished project has something to watch.
    """
    project = _create_and_upload(auth_client, sample_video)
    project_id = project["id"]
    assert auth_client.post(f"/api/v1/projects/{project_id}/process").status_code == 202

    clips = auth_client.get(f"/api/v1/projects/{project_id}/clips").json()
    assert clips, "pipeline should produce clips"
    rendered = [c for c in clips if c["status"] == "rendered"]
    assert rendered, "the top moments should be rendered automatically"
    for clip in rendered:
        assert clip["render_available"] is True
        assert Path(clip["render_path"]).is_file()


def test_a_remote_render_stays_playable_when_the_local_file_is_gone(
    auth_client, sample_video, monkeypatch
):
    """A clip uploaded to object storage must survive the local file vanishing.

    This is the restart case: the row says rendered, the MP4 on the container is
    gone, and the copy in the bucket is the only thing left. The clip has to
    still read as playable, and the video endpoint has to hand back a URL for it
    instead of a 409.
    """
    from quickreel import storage as storage_module

    calls: list[tuple[str, str]] = []

    class _FakeRemoteStorage(storage_module.Storage):
        is_remote = True

        def put(self, local_path, key):
            calls.append(("put", key))
            return f"s3://bucket/{key}"

        def get(self, key, local_path):
            raise AssertionError("playback should not download through the app")

        def url_for(self, key):
            calls.append(("url", key))
            return f"https://bucket.example/{key}?sig=abc"

        def exists(self, key):
            return True

        def delete(self, key):
            pass

    fake = _FakeRemoteStorage()
    # ``renderer`` and the clips route import ``get_storage`` by value, so the
    # module attribute alone would not reach them.
    from quickreel.api.routes import clips as clips_route
    from quickreel.rendering import renderer as renderer_module

    for target in (storage_module, renderer_module, clips_route):
        monkeypatch.setattr(target, "get_storage", lambda: fake)

    project = _create_and_upload(auth_client, sample_video)
    project_id = project["id"]
    assert auth_client.post(f"/api/v1/projects/{project_id}/process").status_code == 202

    clip = auth_client.get(f"/api/v1/projects/{project_id}/clips").json()[0]
    assert clip["status"] == "rendered"
    assert clip["render_meta"]["storage_key"] == f"renders/{Path(clip['render_path']).name}"
    assert clip["render_available"] is True

    Path(clip["render_path"]).unlink()

    detail = auth_client.get(f"/api/v1/projects/{project_id}").json()
    after = detail["clips"][0]
    assert after["render_available"] is True, "the bucket copy is still playable"

    video = auth_client.get(f"/api/v1/clips/{clip['id']}/video", follow_redirects=False)
    assert video.status_code == 307
    assert video.headers["location"].startswith("https://bucket.example/")


def test_concurrent_renders_of_one_clip_cannot_corrupt_the_output(
    auth_client, sample_video, monkeypatch
):
    """Two renders of the same clip must not interleave writes to one file.

    The pipeline auto-renders the top moments and flips the project to ``ready``
    *before* rendering, so a client that reacts to ``ready`` by calling
    ``/render`` starts a second ffmpeg on the same deterministic output path.
    Both write a ``moov`` atom, and the loser's bytes land mid-file: the result
    probes as a valid MP4 but decodes zero frames.
    """
    import subprocess
    import threading

    from quickreel.db import get_sessionmaker
    from quickreel.models import Clip
    from quickreel.rendering import renderer

    project = _create_and_upload(auth_client, sample_video)
    project_id = project["id"]
    assert auth_client.post(f"/api/v1/projects/{project_id}/process").status_code == 202

    clip_id = auth_client.get(f"/api/v1/projects/{project_id}/clips").json()[0]["id"]

    # Hold the first render inside ffmpeg so the second arrives while the file is
    # open, which is the interleaving that produced the corrupt production clip.
    started = threading.Event()
    release = threading.Event()
    original = renderer.ffmpeg.render_clip

    def slow_render(*args, **kwargs):
        started.set()
        release.wait(timeout=30)
        return original(*args, **kwargs)

    monkeypatch.setattr(renderer.ffmpeg, "render_clip", slow_render)

    def run() -> None:
        session = get_sessionmaker()()
        try:
            clip = session.get(Clip, clip_id)
            renderer.render_clip(session, clip)
        finally:
            session.close()

    first = threading.Thread(target=run)
    first.start()
    assert started.wait(timeout=30), "first render never reached ffmpeg"

    # Second caller, while the first is mid-render: it must not run concurrently.
    second_session = get_sessionmaker()()
    try:
        second_clip = second_session.get(Clip, clip_id)
        renderer.render_clip(second_session, second_clip)
    finally:
        second_session.close()

    release.set()
    first.join(timeout=120)
    assert not first.is_alive()

    monkeypatch.setattr(renderer.ffmpeg, "render_clip", original)

    session = get_sessionmaker()()
    try:
        clip = session.get(Clip, clip_id)
        output = Path(clip.render_path)
        assert clip.status == "rendered"
    finally:
        session.close()

    # Exactly one moov, and the container is not truncated.
    import struct

    data = output.read_bytes()
    offset = 0
    boxes = []
    while offset + 8 <= len(data):
        size = struct.unpack(">I", data[offset : offset + 4])[0]
        boxes.append((data[offset + 4 : offset + 8], offset + size <= len(data)))
        if size < 8:
            break
        offset += size
    assert sum(1 for name, _ in boxes if name == b"moov") == 1, boxes
    assert all(ok for _, ok in boxes), boxes

    decode = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(output), "-f", "null", "-"],
        capture_output=True,
        text=True,
    )
    assert decode.returncode == 0, decode.stderr[:500]
    assert "Invalid NAL unit size" not in decode.stderr

    # The .partial file must not survive a successful render.
    assert not list(output.parent.glob(f"{output.stem}.partial*"))
