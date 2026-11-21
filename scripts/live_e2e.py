"""End-to-end check against a deployed QuickReel instance.

Runs the real user journey: sign up, create a project from a video URL, process
it, wait for clips, download one. Used to verify that URL ingestion works on a
given host, which is the question a datacenter IP can only answer empirically.

Usage:
    python scripts/live_e2e.py --base-url https://example.onrender.com --url <video>
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path

import httpx


def _fail(step: str, response: httpx.Response) -> None:
    print(f"FAIL {step}: HTTP {response.status_code}")
    print(response.text[:1500])
    raise SystemExit(1)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--url", required=True, help="source video URL to ingest")
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--download", type=Path, default=None)
    args = parser.parse_args()

    base = args.base_url.rstrip("/")
    email = f"e2e-{uuid.uuid4().hex[:12]}@example.com"
    password = "correct-horse-battery-staple"

    with httpx.Client(base_url=base, timeout=120.0, follow_redirects=True) as client:
        health = client.get("/healthz")
        if health.status_code != 200:
            _fail("healthz", health)
        print(f"healthz OK ({base})")

        signup = client.post(
            "/api/v1/auth/signup",
            json={"email": email, "password": password, "workspace_name": "E2E"},
        )
        if signup.status_code not in (200, 201):
            _fail("signup", signup)
        print(f"signup OK as {email}")

        project = client.post(
            "/api/v1/projects",
            json={
                "title": "Live URL ingestion",
                "source_url": args.url,
                "license_note": "operator supplied URL",
                "authorized_by": "e2e",
            },
        )
        if project.status_code not in (200, 201):
            _fail("create project", project)
        project_id = project.json()["id"]
        print(f"project {project_id} created from URL")

        started = time.time()
        process = client.post(f"/api/v1/projects/{project_id}/process")
        if process.status_code not in (200, 202):
            _fail("process", process)
        print("processing started; polling for clips")

        clips: list[dict] = []
        last_status = None
        while time.time() - started < args.timeout:
            detail = client.get(f"/api/v1/projects/{project_id}")
            if detail.status_code != 200:
                _fail("project detail", detail)
            body = detail.json()
            status = body.get("status")
            if status != last_status:
                print(f"  status={status} elapsed={time.time() - started:.0f}s")
                last_status = status
            if status in {"failed", "error"}:
                print("FAIL project failed")
                print(json.dumps(body, indent=2)[:2000])
                return 1
            clips = body.get("clips") or []
            if status == "ready" and clips:
                break
            time.sleep(10)

        if not clips:
            print("FAIL no clips produced within the timeout")
            return 1

        elapsed = time.time() - started
        print(f"\n{len(clips)} clips in {elapsed:.0f}s")
        for clip in clips:
            print(
                f"  {clip.get('start_seconds', clip.get('start'))}-"
                f"{clip.get('end_seconds', clip.get('end'))} "
                f"score={clip.get('viral_score', clip.get('score'))} "
                f"{str(clip.get('title', ''))[:60]}"
            )

        transcript = body.get("transcript") or {}
        print(f"transcript provider={transcript.get('provider')} "
              f"segments={len(transcript.get('segments') or [])}")
        print(f"providers={json.dumps(body.get('providers') or body.get('provider_notes') or {})}")

        if args.download:
            # Rendering is a separate explicit step; a clip exists before its
            # video does, so downloading without this returns 409.
            render = client.post(
                f"/api/v1/projects/{project_id}/render",
                json={"max_clips": 1, "vertical": True, "burn_captions": True},
            )
            if render.status_code != 200:
                _fail("render", render)
            clip_id = render.json()[0]["id"]
            download = client.get(f"/api/v1/clips/{clip_id}/download")
            if download.status_code != 200:
                _fail("download", download)
            args.download.parent.mkdir(parents=True, exist_ok=True)
            args.download.write_bytes(download.content)
            print(f"downloaded {len(download.content)} bytes to {args.download}")

    print("\nPASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
