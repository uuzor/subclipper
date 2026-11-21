#!/usr/bin/env python3
"""Seed the app-review demo account.

TikTok and Meta both require a reviewer to be able to reach the publishing
feature without supplying their own hour-long podcast, so the filing needs a
tenant that is already populated: one processed project with scored clips, at
least one rendered to 1080x1920 with captions, and a scheduled post to a private
test account. This script builds exactly that, over the public HTTP API, so it
exercises the same code path a real user does.

It is idempotent: re-running it against a database that already has the demo
account reuses the existing project rather than creating a second one.

The source footage is synthetic (generated with ffmpeg, or espeak-ng when it is
installed). Nothing third-party goes into the demo, because the rights question
is the one this product must never invite into an app review.

Usage:

    .venv/bin/python scripts/seed_review_account.py \
        --base-url https://clipviralmoment.com \
        --email appreview@clipviralmoment.com \
        --password '<set at filing time>'

The password is never stored in this repository; pass it at run time.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

# Spoken by espeak-ng when it is available, so the transcript the moment
# detector sees is real speech rather than a tone. Kept short and clearly
# non-third-party.
DEMO_SCRIPT = (
    "The biggest mistake I made was trusting everyone. "
    "University degrees do not guarantee success. "
    "I gave away equity because I wanted to be liked. "
    "The lesson is that obsession beats intelligence."
)


def log(message: str) -> None:
    print(f"[seed] {message}", flush=True)


def build_sample(target: Path) -> Path:
    """Generate a synthetic long-form source. No third-party footage."""
    has_espeak = (
        subprocess.run(["which", "espeak-ng"], capture_output=True, text=True).returncode == 0
    )

    if has_espeak:
        speech = target.with_suffix(".wav")
        subprocess.run(
            ["espeak-ng", "-v", "en-us", "-s", "150", "-w", str(speech), DEMO_SCRIPT],
            check=True,
            capture_output=True,
        )
        audio_input = ["-i", str(speech)]
    else:
        log("espeak-ng not found; falling back to a tone track")
        audio_input = ["-f", "lavfi", "-i", "sine=frequency=220:duration=30"]

    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=navy:s=1280x720:d=30",
            *audio_input,
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


def signup(client: httpx.Client, email: str, password: str) -> None:
    response = client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": password, "workspace_name": "App review"},
    )
    if response.status_code == 201:
        log(f"created review account {email}")
        return
    # Already exists: sign in instead, so re-running the script is safe.
    response = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    if response.status_code != 200:
        raise SystemExit(
            f"could not create or sign in as {email}: {response.status_code} {response.text}"
        )
    log(f"signed in as existing account {email}")


def find_existing_project(client: httpx.Client, title: str) -> str | None:
    for project in client.get("/api/v1/projects").json():
        if project["title"] == title:
            return project["id"]
    return None


def find_existing_target(client: httpx.Client, display_name: str) -> str | None:
    for target in client.get("/api/v1/publish-targets").json():
        if target["display_name"] == display_name:
            return target["id"]
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--title", default="Demo: review walkthrough")
    args = parser.parse_args()

    with httpx.Client(base_url=args.base_url, timeout=300.0) as client:
        signup(client, args.email, args.password)

        project_id = find_existing_project(client, args.title)
        if project_id:
            log(f"reusing existing project {project_id}")
        else:
            created = client.post(
                "/api/v1/projects",
                json={
                    "title": args.title,
                    "authorized_by": "QuickReel",
                    "license_note": (
                        "Synthetic sample generated by QuickReel for app review; "
                        "no third-party footage"
                    ),
                },
            )
            created.raise_for_status()
            project_id = created.json()["id"]
            log(f"created project {project_id}")

            with tempfile.TemporaryDirectory() as tmp:
                sample = build_sample(Path(tmp) / "sample-longform.mp4")
                with sample.open("rb") as handle:
                    uploaded = client.post(
                        f"/api/v1/projects/{project_id}/upload",
                        files={"file": ("sample-longform.mp4", handle, "video/mp4")},
                    )
                uploaded.raise_for_status()
                log("uploaded synthetic source")

            processed = client.post(f"/api/v1/projects/{project_id}/process")
            if processed.status_code != 202:
                raise SystemExit(f"processing failed: {processed.status_code} {processed.text}")
            log("processed (transcribe -> analyze -> moments -> score)")

        clips = client.get(f"/api/v1/projects/{project_id}/clips").json()
        if not clips:
            raise SystemExit("pipeline produced no clips; nothing to demo")
        log(f"{len(clips)} scored clips available")

        rendered = client.post(
            f"/api/v1/projects/{project_id}/render",
            json={"clip_ids": [clips[0]["id"]]},
        )
        if rendered.status_code != 200:
            raise SystemExit(f"render failed: {rendered.status_code} {rendered.text}")
        log(f"rendered top clip ({clips[0]['id']}) to 9:16 with captions")

        target_name = "Review test account (private)"
        target_id = find_existing_target(client, target_name)
        if target_id:
            log(f"reusing publish target {target_id}")
        else:
            target = client.post(
                "/api/v1/publish-targets",
                json={
                    "platform": "tiktok",
                    "display_name": target_name,
                    "external_account_id": "review-private",
                },
            )
            target.raise_for_status()
            target_id = target.json()["id"]
            log(f"created publish target {target_id}")

        existing_posts = client.get(
            f"/api/v1/scheduled-posts?clip_id={clips[0]['id']}"
        ).json()
        if existing_posts:
            log(f"reusing scheduled post {existing_posts[0]['id']}")
        else:
            scheduled = client.post(
                "/api/v1/scheduled-posts",
                json={
                    "clip_id": clips[0]["id"],
                    "target_id": target_id,
                    "scheduled_for": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
                    "caption": "Demo clip for app review",
                },
            )
            scheduled.raise_for_status()
            log(f"scheduled post {scheduled.json()['id']} (status: scheduled)")

    log("demo account ready; put the credentials in the review form")
    log("rotate the password once the review closes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
