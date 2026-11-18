"""Publishing HTTP API tests.

Exercises the routes against the real app with the deterministic mock adapter,
so the full path from "schedule a post" to "published" is covered over HTTP.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path


def _rendered_clip(client, sample_video: Path) -> str:
    """Run a project all the way to a rendered clip and return its id."""
    created = client.post("/api/v1/projects", json={"title": "Podcast"})
    project_id = created.json()["id"]
    with sample_video.open("rb") as handle:
        client.post(
            f"/api/v1/projects/{project_id}/upload",
            files={"file": ("sample.mp4", handle, "video/mp4")},
        )
    client.post(f"/api/v1/projects/{project_id}/process")
    clips = client.get(f"/api/v1/projects/{project_id}/clips").json()
    assert clips, "pipeline should produce candidate clips"
    clip_id = clips[0]["id"]
    rendered = client.post(
        f"/api/v1/projects/{project_id}/render",
        json={"clip_ids": [clip_id]},
    )
    assert rendered.status_code == 200, rendered.text
    return clip_id


def _target(client, platform: str = "tiktok") -> str:
    response = client.post(
        "/api/v1/publish-targets",
        json={"platform": platform, "display_name": "Test account"},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_list_platforms_reports_adapter_availability(client):
    body = client.get("/api/v1/platforms").json()
    names = {p["name"] for p in body["platforms"]}
    assert names == {"tiktok", "youtube_shorts", "instagram_reels"}
    # No live adapter ships yet, so every platform reports false.
    assert all(p["live_adapter_registered"] is False for p in body["platforms"])


def test_create_target_rejects_unknown_platform(client):
    response = client.post("/api/v1/publish-targets", json={"platform": "myspace"})
    assert response.status_code == 422
    assert "unknown platform" in response.json()["detail"]


def test_create_target_does_not_echo_credentials(client):
    response = client.post(
        "/api/v1/publish-targets",
        json={
            "platform": "tiktok",
            "display_name": "Acct",
            "credentials": {"access_token": "super-secret-token"},
        },
    )
    assert response.status_code == 201
    # A token must never be serialised back out through the API.
    assert "super-secret-token" not in response.text
    assert "credentials" not in response.json()


def test_schedule_rejects_unrendered_clip(client, sample_video):
    created = client.post("/api/v1/projects", json={"title": "P"})
    project_id = created.json()["id"]
    with sample_video.open("rb") as handle:
        client.post(
            f"/api/v1/projects/{project_id}/upload",
            files={"file": ("sample.mp4", handle, "video/mp4")},
        )
    client.post(f"/api/v1/projects/{project_id}/process")
    clip_id = client.get(f"/api/v1/projects/{project_id}/clips").json()[0]["id"]
    target_id = _target(client)

    response = client.post(
        "/api/v1/scheduled-posts",
        json={
            "clip_id": clip_id,
            "target_id": target_id,
            "scheduled_for": datetime.now(UTC).isoformat(),
        },
    )
    assert response.status_code == 409
    assert "render it before scheduling" in response.json()["detail"]


def test_schedule_and_publish_end_to_end(client, sample_video):
    clip_id = _rendered_clip(client, sample_video)
    target_id = _target(client)

    created = client.post(
        "/api/v1/scheduled-posts",
        json={
            "clip_id": clip_id,
            "target_id": target_id,
            "scheduled_for": (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
            "caption": "Watch this",
        },
    )
    assert created.status_code == 201, created.text
    post_id = created.json()["id"]
    assert created.json()["status"] == "scheduled"

    run = client.post("/api/v1/publishing/run-due")
    assert run.status_code == 200, run.text
    body = run.json()
    assert body["published"] == 1
    assert body["failed"] == 0

    after = client.get(f"/api/v1/scheduled-posts/{post_id}").json()
    assert after["status"] == "published"
    assert after["external_post_id"]
    assert after["published_at"] is not None

    attempts = client.get(f"/api/v1/scheduled-posts/{post_id}/attempts").json()
    assert len(attempts) == 1
    assert attempts[0]["ok"] is True


def test_future_post_is_not_published(client, sample_video):
    clip_id = _rendered_clip(client, sample_video)
    target_id = _target(client)
    client.post(
        "/api/v1/scheduled-posts",
        json={
            "clip_id": clip_id,
            "target_id": target_id,
            "scheduled_for": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
        },
    )

    run = client.post("/api/v1/publishing/run-due").json()
    assert run["examined"] == 0
    assert run["published"] == 0


def test_publish_now_is_idempotent(client, sample_video):
    clip_id = _rendered_clip(client, sample_video)
    target_id = _target(client)
    post_id = client.post(
        "/api/v1/scheduled-posts",
        json={
            "clip_id": clip_id,
            "target_id": target_id,
            "scheduled_for": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
        },
    ).json()["id"]

    first = client.post(f"/api/v1/scheduled-posts/{post_id}/publish-now")
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "published"

    # The claim guard means a second press cannot double-post.
    second = client.post(f"/api/v1/scheduled-posts/{post_id}/publish-now")
    assert second.status_code == 409


def test_cancel_prevents_publication(client, sample_video):
    clip_id = _rendered_clip(client, sample_video)
    target_id = _target(client)
    post_id = client.post(
        "/api/v1/scheduled-posts",
        json={
            "clip_id": clip_id,
            "target_id": target_id,
            "scheduled_for": (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
        },
    ).json()["id"]

    canceled = client.post(f"/api/v1/scheduled-posts/{post_id}/cancel")
    assert canceled.status_code == 200
    assert canceled.json()["status"] == "canceled"

    run = client.post("/api/v1/publishing/run-due").json()
    assert run["published"] == 0


def test_cancel_rejects_already_published(client, sample_video):
    clip_id = _rendered_clip(client, sample_video)
    target_id = _target(client)
    post_id = client.post(
        "/api/v1/scheduled-posts",
        json={
            "clip_id": clip_id,
            "target_id": target_id,
            "scheduled_for": (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
        },
    ).json()["id"]
    client.post(f"/api/v1/scheduled-posts/{post_id}/publish-now")

    response = client.post(f"/api/v1/scheduled-posts/{post_id}/cancel")
    assert response.status_code == 409


def test_list_posts_filters_by_status(client, sample_video):
    clip_id = _rendered_clip(client, sample_video)
    target_id = _target(client)
    post_id = client.post(
        "/api/v1/scheduled-posts",
        json={
            "clip_id": clip_id,
            "target_id": target_id,
            "scheduled_for": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
        },
    ).json()["id"]
    # Scope to this clip: the database is shared across the module.
    scheduled = client.get(f"/api/v1/scheduled-posts?status=scheduled&clip_id={clip_id}").json()
    assert [p["id"] for p in scheduled] == [post_id]
    published = client.get(f"/api/v1/scheduled-posts?status=published&clip_id={clip_id}").json()
    assert published == []

    bad = client.get("/api/v1/scheduled-posts?status=nonsense")
    assert bad.status_code == 422


def test_delete_target_removes_its_posts(client, sample_video):
    clip_id = _rendered_clip(client, sample_video)
    target_id = _target(client)
    post_id = client.post(
        "/api/v1/scheduled-posts",
        json={
            "clip_id": clip_id,
            "target_id": target_id,
            "scheduled_for": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
        },
    ).json()["id"]
    assert client.delete(f"/api/v1/publish-targets/{target_id}").status_code == 204
    # The cascade means the orphaned post is gone rather than left dangling.
    assert client.get(f"/api/v1/scheduled-posts/{post_id}").status_code == 404


def test_scheduled_post_out_reports_duration_free_shape(client, sample_video):
    """Guard against a response model that fails validation on real rows."""
    clip_id = _rendered_clip(client, sample_video)
    target_id = _target(client)
    created = client.post(
        "/api/v1/scheduled-posts",
        json={
            "clip_id": clip_id,
            "target_id": target_id,
            "scheduled_for": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
        },
    )
    body = created.json()
    # Timestamps must round-trip as ISO strings, not raw datetimes.
    assert isinstance(body["scheduled_for"], str)
    assert isinstance(body["created_at"], str)


def test_provenance_round_trips_on_a_project(client):
    """Provenance is a selling point, so it has to be readable, not just stored."""
    created = client.post(
        "/api/v1/projects",
        json={
            "title": "Client podcast",
            "license_note": "Clipping rights granted 2026-01-01, expires 2027-01-01",
            "authorized_by": "producer@example.com",
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["authorized_by"] == "producer@example.com"
    assert "Clipping rights granted" in body["license_note"]

    fetched = client.get(f"/api/v1/projects/{body['id']}").json()
    assert fetched["authorized_by"] == "producer@example.com"


def test_provenance_defaults_to_empty(client):
    created = client.post("/api/v1/projects", json={"title": "No license recorded"})
    body = created.json()
    assert body["license_note"] == ""
    assert body["authorized_by"] == ""
