# QuickReel

AI viral clip generator. Give it a long-form video (podcast, interview, stream,
lecture) by uploading a file or pasting a link, and QuickReel transcribes it,
finds the moments most likely to stop a scroll, cuts them to clean boundaries,
and renders platform-ready 9:16 clips.

This repository implements the pipeline described in the product brief, with the
viral moment detection engine as the centrepiece. It is a working backend, not a
mockup: the pipeline runs end to end and produces real 1080x1920 MP4 files.

## What is implemented

| Stage | Status | Where |
| --- | --- | --- |
| Video ingestion (upload + probe) | Done | `api/routes/projects.py`, `media/ffmpeg.py` |
| Video ingestion (URL / link import) | Done (yt-dlp, SSRF-guarded) | `media/downloader.py`, `media/url_source.py` |
| Speech recognition | Done (Whisper / faster-whisper, fake fallback) | `ai/providers.py`, `ai/fakes.py` |
| Scene + visual understanding | Done (Gemini, fake fallback) | `ai/providers.py` |
| Audio analysis (laughter, emphasis, silence) | Done (librosa, fake fallback) | `ai/providers.py`, `media/ffmpeg.py` |
| Viral moment detection | Done | `analysis/moments.py` |
| Viral scoring engine | Done | `analysis/scoring.py` |
| Intelligent clipping (boundary optimisation) | Done | `analysis/boundaries.py` |
| Vertical render + captions | Done | `rendering/renderer.py` |
| Async processing queue | Done (Celery + Redis, eager fallback) | `pipeline/` |
| Face tracking / speaker-aware crop | Not yet | hook in `media/ffmpeg.py` |
| Publishing to platforms | Mock adapters only | `publishing/` |
| Channel ingestion trigger (WebSub) | Done (mock hub fallback) | `triggers/` |
| Performance feedback loop (sample collection) | Done (mock adapters) | `publishing/metrics.py` |
| Viral Intelligence weight recalibration | Done (propose + explicit apply) | `analysis/calibration.py` |

## Architecture

```
Frontend (Next.js / React)  ·  upload · timeline · clip editor
              |
        FastAPI backend  ·  auth · projects · orchestration
              |
    AI analysis pipeline  ·  speech AI · vision AI · reasoning AI
              |
  Viral moment detection engine  ·  extract · score · rank · optimise boundaries
              |
     Video rendering engine  ·  FFmpeg · captions · reframing · normalise
              |
     Finished short-form video  ·  MP4 · 9:16 · 1080x1920
```

See `docs/architecture.md` for the module map, the scoring model, and the
extension points for face tracking and the learning loop.

## Quickstart

```bash
make install          # venv + core dependencies
make install-ai       # optional: Whisper, Gemini SDK, librosa
make install-download # optional: yt-dlp, for importing videos from a link
cp .env.example .env
make test             # real ffmpeg, no API keys needed
make run              # http://localhost:12000/docs
make serve            # one process: API + built frontend, http://localhost:12000
```

The API is behind email + password authentication. Open `http://localhost:12000`
and create an account, or use the API directly: sign up, keep the session
cookie, and send it with every later call.

```bash
curl -s -c /tmp/qr.jar -X POST localhost:12000/api/v1/auth/signup \
  -H 'content-type: application/json' \
  -d '{"email":"you@example.com","password":"a-good-password","workspace_name":"My studio"}'

curl -s -b /tmp/qr.jar localhost:12000/api/v1/auth/me
curl -s -b /tmp/qr.jar -X POST localhost:12000/api/v1/auth/logout
```

The default configuration uses deterministic fake AI providers and eager task
execution, so the whole pipeline runs locally with no credentials and no broker.
Swap in real providers by setting `QUICKREEL_TRANSCRIPTION_PROVIDER`,
`QUICKREEL_VISION_PROVIDER` and `QUICKREEL_MOMENT_PROVIDER`.

### Deploying behind a port forward

On a host that maps a local port to a public URL (a container runtime, a VM, a
tunnel), `serve` is enough if the public origin is not needed for anything the
app generates. When it is — OAuth redirect URIs and the WebSub callback are both
built from `QUICKREEL_PUBLIC_BASE_URL` — use `deploy` and pass that URL:

```bash
make deploy PUBLIC_BASE_URL=https://my-app.example.com PORT=12000
```

The app builds no links from the request host, so this is the only thing that
has to be right for a forwarded deployment. Both targets bind `0.0.0.0`, so the
port forward reaches them.

### Deploying to Render

`Dockerfile` and `render.yaml` describe the deployment: one container serving
the API and the built SPA, PostgreSQL for state, and a persistent disk for
uploads and renders. Point Render at this repository and pick **Blueprint**.

```bash
# Verify a running deployment
curl -fsS https://<host>/health
curl -fsS -o /dev/null -w '%{http_code}\n' https://<host>/workbench   # SPA deep link
```

Production mode (`QUICKREEL_ENV=production`) refuses to start on an unsafe
configuration — mock publishing, a missing OAuth state secret, or a non-HTTPS
public origin — so a misconfiguration is a boot failure rather than a publish
that silently reaches nobody. See [docs/deployment.md](docs/deployment.md) for
the required values and how to scale the worker out.

## Using the API

### Option A: import from a link (YouTube, TikTok, Instagram, ...)

```bash
# 0. Optional: check the link is supported and see its metadata, no download
curl -s -X POST localhost:12000/api/v1/projects/source-url/probe \
  -H 'content-type: application/json' \
  -d '{"source_url":"https://www.youtube.com/watch?v=..."}'

# 1. Create a project directly from the link
PROJECT=$(curl -s -X POST localhost:12000/api/v1/projects \
  -H 'content-type: application/json' \
  -d '{"title":"Founder interview","source_url":"https://www.youtube.com/watch?v=..."}' \
  | python -c 'import sys,json;print(json.load(sys.stdin)["id"])')

# 2. Run the pipeline. The video is downloaded during ingest, then analysed.
curl -s -X POST localhost:12000/api/v1/projects/$PROJECT/process
```

### Option B: upload a file you own or are licensed to use

```bash
# 1. Create a project
PROJECT=$(curl -s -X POST localhost:12000/api/v1/projects \
  -H 'content-type: application/json' \
  -d '{"title":"Founder interview"}' | python -c 'import sys,json;print(json.load(sys.stdin)["id"])')

# 2. Upload the video
curl -s -X POST localhost:12000/api/v1/projects/$PROJECT/upload \
  -F file=@podcast.mp4

# 3. Run the pipeline (transcribe, analyse, detect and score moments)
curl -s -X POST localhost:12000/api/v1/projects/$PROJECT/process
```

Both options converge from here:

```bash
# 4. Read the ranked clips, with a full score breakdown for each
curl -s localhost:12000/api/v1/projects/$PROJECT/clips | python -m json.tool

# 5. Render the top 3 clips to vertical MP4
curl -s -X POST localhost:12000/api/v1/projects/$PROJECT/render \
  -H 'content-type: application/json' -d '{"max_clips":3}'

# 6. Download a rendered clip
curl -sO localhost:12000/api/v1/clips/<clip_id>/download
```

A link can also be attached to an existing project with
`POST /api/v1/projects/{id}/source-url`, which mirrors `/upload`.

### Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/healthz` | Liveness |
| `GET` | `/readyz` | Readiness plus resolved AI providers |
| `POST` | `/api/v1/projects` | Create a project (optionally with `source_url`) |
| `GET` | `/api/v1/projects` | List projects |
| `GET` | `/api/v1/projects/{id}` | Project detail with transcript and clips |
| `POST` | `/api/v1/projects/{id}/upload` | Upload source video |
| `POST` | `/api/v1/projects/{id}/source-url` | Attach a remote video URL |
| `POST` | `/api/v1/projects/source-url/probe` | Fetch URL metadata without downloading |
| `POST` | `/api/v1/projects/{id}/process` | Run the analysis pipeline (downloads first if needed) |
| `GET` | `/api/v1/projects/{id}/clips` | Ranked clips (`min_score`, `moment_type` filters) |
| `POST` | `/api/v1/projects/{id}/render` | Render clips to 9:16 MP4 |
| `GET` | `/api/v1/clips/{id}` | Single clip with score breakdown |
| `GET` | `/api/v1/clips/{id}/download` | Download rendered MP4 |
| `DELETE` | `/api/v1/projects/{id}` | Delete a project and its renders |

## Importing from a URL

Users can paste a link instead of uploading a file. Downloading is handled by
[yt-dlp](https://github.com/yt-dlp/yt-dlp), which covers YouTube, TikTok,
Instagram, X, Vimeo, Facebook and about a thousand other sites. Install it with:

```bash
pip install 'quickreel[download]'
```

If yt-dlp is not installed the app still runs; URL requests fail with a clear
message rather than crashing.

The download happens during the pipeline's ingest stage, so the rest of the
pipeline only ever deals with a local file. A placeholder media asset is created
when the link is saved, and its path is filled in after the download.

Because the server makes an outbound request on the user's behalf, every URL
passes an SSRF guard (`media/url_source.py`) before anything is fetched:

1. Only `http` and `https` are allowed.
2. URLs with embedded credentials are rejected.
3. An optional host allowlist can lock ingestion to known platforms.
4. The hostname is resolved and every resulting address must be globally
   routable. Loopback, private, link-local, reserved and cloud-metadata
   addresses are refused, including IPv4-mapped IPv6 forms.

The URL is validated again inside the pipeline, so the stored value is re-checked
at the point of use.

## Viral scoring

Six weighted factors, all normalised to 0..1, combined into a 0..100 score:

| Factor | Weight | What it measures |
| --- | --- | --- |
| Emotional intensity | 20% | Emotional language plus reactive audio (laughter, emphasis) |
| Hook strength | 20% | Whether the first 2-3 seconds capture attention |
| Storytelling / curiosity | 20% | Narrative shape: setup, turn, payoff |
| Shareability | 15% | Quotability, length fit, cleanliness |
| Audio & visual engagement | 10% | Loudness peaks, scene changes, visual highlights |
| Standalone context | 15% | Whether the clip makes sense on its own |

Penalties are subtracted for mid-sentence openings, incomplete endings, extreme
durations, filler-heavy delivery and long pauses. Every clip stores a full
`score_breakdown` (per-factor contributions, penalties, raw features, weights)
so the ranking is explainable and recalibratable. Weights are initial design
assumptions and live in configuration; the plan is to calibrate them against
real views, retention and share data.

Viral potential is a prediction, not a guarantee.

## Boundary optimisation

A detected moment is a semantic span, not a cut point. The optimiser expands and
trims it so the exported clip:

1. Opens on a clean sentence, backing up to include the setup.
2. Keeps complete sentences at the tail.
3. Extends to include a trailing reaction (laughter, applause).
4. Snaps lead-in/lead-out to nearby silence.
5. Respects minimum and maximum durations.

The brief's worked example - statement 10:32-10:48, setup from 10:28, laughter
at 10:50, ideal clip 10:26-10:54 - is covered directly in
`tests/test_boundaries.py`.

## Configuration

All settings are environment variables prefixed with `QUICKREEL_`. See
`.env.example` for the full list. Highlights:

| Variable | Default | Meaning |
| --- | --- | --- |
| `QUICKREEL_DATABASE_URL` | SQLite file | PostgreSQL in production, e.g. `postgresql+psycopg://...` |
| `QUICKREEL_REDIS_URL` | empty | Empty means eager in-process tasks |
| `QUICKREEL_TASK_ALWAYS_EAGER` | `true` | Set `false` with Redis for async workers |
| `QUICKREEL_TRANSCRIPTION_PROVIDER` | `auto` | `auto`, `faster-whisper`, `fake` |
| `QUICKREEL_VISION_PROVIDER` | `auto` | `auto`, `gemini`, `fake` |
| `QUICKREEL_MOMENT_PROVIDER` | `auto` | `auto`, `llm`, `fake` |
| `QUICKREEL_MAX_UPLOAD_MB` | `2048` | Upload size limit (also caps downloaded files) |
| `QUICKREEL_URL_INGESTION_ENABLED` | `true` | Allow importing from a link |
| `QUICKREEL_ALLOWED_SOURCE_HOSTS` | empty | Comma-separated host allowlist; empty means any public host |
| `QUICKREEL_DOWNLOAD_MAX_HEIGHT` | `1080` | Cap on downloaded resolution |
| `QUICKREEL_DOWNLOAD_TIMEOUT_SECONDS` | `1800` | Per-download socket timeout |
| `QUICKREEL_DOWNLOADER_COOKIES_FILE` | empty | Netscape cookies file for logged-in sites (treat as a secret) |
| `QUICKREEL_DOWNLOADER_PROXY` | empty | Proxy URL for yt-dlp |

`auto` selects the real provider when its dependency and credentials are present,
and otherwise logs the reason and falls back to the deterministic fake.

Some platforms (YouTube in particular) block datacenter IPs, so a download may
fail with a 403 from a cloud host even though the same link works locally. In
that case run the downloader through a residential proxy
(`QUICKREEL_DOWNLOADER_PROXY`) or supply a cookies file.

## Frontend

The UI is a Vite + React + TypeScript app in `frontend/`. In development it runs
on its own port and proxies `/api` to the backend, so no CORS setup is needed:

```bash
make run                 # terminal 1: API on http://localhost:12000
cd frontend && npm install && npm run dev   # terminal 2: UI on http://localhost:5173
```

To run the whole app on a single origin — API and UI from one process, which is
how it is deployed — build the UI and point the server at the output:

```bash
make serve               # builds frontend/, then serves everything on :12000
```

`make serve` sets `QUICKREEL_FRONTEND_DIR` to `frontend/dist` and binds
`0.0.0.0:12000`. The server mounts `/assets` for hashed bundles and falls back to
`index.html` for any other path, so client-side routes like `/projects/<id>`
survive a hard refresh. Unknown `/api` paths still return JSON 404s.

## Development

```bash
make test    # pytest
make lint    # ruff check
make fmt     # ruff format
make worker  # Celery worker (requires Redis and eager disabled)
```

## Project layout

```
src/quickreel/
  config.py            environment-driven settings
  db.py  models.py     SQLAlchemy engine, session, ORM models
  schemas.py           API request/response models
  storage.py           local filesystem / S3-compatible storage
  media/ffmpeg.py      probe, audio extraction, silencedetect, vertical render
  media/downloader.py  yt-dlp URL downloader plus an offline fake
  media/url_source.py  source URL validation and SSRF protection
  ai/base.py           provider protocols and data contracts
  ai/fakes.py          deterministic offline providers
  ai/providers.py      Whisper, Gemini, LLM, librosa implementations
  ai/registry.py       provider selection with graceful fallback
  analysis/features.py normalised virality signals
  analysis/scoring.py  weighted scoring engine
  analysis/boundaries.py clip boundary optimiser
  analysis/moments.py  detect -> dedup -> optimise -> score -> rank
  pipeline/            orchestrator, Celery app and tasks
  rendering/renderer.py subtitles and vertical export
  api/routes/          health, projects, clips
tests/                 unit, media and end-to-end API tests
```

## Roadmap

Phased per the brief. Phases 1-5 are implemented, plus the distribution
foundation (scheduler, adapter interface, mocks).

1. ~~Video upload and transcription~~
2. ~~AI identification of interesting moments~~
3. ~~Automatic clip extraction~~
4. ~~Vertical video conversion and captions~~
5. ~~Viral scoring and clip ranking~~
6. Face tracking and advanced editing (MediaPipe, per-frame crop offsets)
7. ~~Performance analytics and the Viral Intelligence learning loop~~ (recording and recalibration)

Distribution work, in build order:

- ~~Scheduler, retry policy, adapter interface, mock adapters~~ (done)
- ~~Upload and authorised-channel ingest feeding the scheduler~~ (done)
- ~~YouTube upload-event trigger (WebSub; needs no app audit)~~ (done)
- TikTok and YouTube Shorts adapters, then Meta once app review clears
- ~~Performance feedback loop~~ (done — samples recorded from first publish)
- ~~Viral Intelligence weight recalibration~~ (done — fits are proposed, applied explicitly)
- Trend-to-back-catalogue matching

## Publishing and scheduling

Rendered clips can be queued for publication to a connected account. The
scheduler is deliberately platform-agnostic: a platform is an adapter
implementing three methods, and adding one does not touch the scheduler, the
models, or the routes.

```bash
# Connect an account (credentials are stored, never echoed back)
curl -X POST localhost:12000/api/v1/publish-targets \
  -H 'Content-Type: application/json' \
  -d '{"platform": "tiktok", "display_name": "My account"}'

# Queue a rendered clip
curl -X POST localhost:12000/api/v1/scheduled-posts \
  -H 'Content-Type: application/json' \
  -d '{"clip_id": "...", "target_id": "...", "scheduled_for": "2026-06-01T09:00:00Z",
       "caption": "Watch this"}'

# Publish everything that is due (Celery beat does this every minute)
curl -X POST localhost:12000/api/v1/publishing/run-due
```

Retry policy is the part worth knowing. A transient or rate-limit failure is
rescheduled with exponential backoff, capped so a throttled post cannot drift
days out; a platform-supplied `retry-after` always wins over the computed delay.
A permanent rejection or an authentication failure is terminal — retrying a
rejected video forever is how an app gets rate-limited or banned. Every adapter
call is recorded as a `PublishAttempt`, so "it worked on the fourth try" survives
as history rather than being overwritten by the last error.

No live platform adapter ships yet. `QUICKREEL_PUBLISHING_MODE=mock` (the
default) resolves every platform to a deterministic mock, so the whole loop runs
and is tested without credentials. In `live` mode an unregistered platform raises
rather than silently falling back to the mock.

## Watching a channel (automatic ingestion)

Pasting a link is fine for one video; watching a channel is what makes the
product run without a person. QuickReel subscribes to a channel through the
PubSubHubbub hub, and the hub pushes a notification when the channel publishes.
Ingest creates a project for the new video, which then flows through the normal
pipeline.

```bash
# Start watching a channel. Record the authorisation: it is carried onto every
# project (and clip) this channel produces.
curl -X POST localhost:12000/api/v1/triggers/youtube/subscriptions \
  -H 'Content-Type: application/json' \
  -d '{"channel_id": "UCxxxxxxxxxxxxxxxxxxxxxx", "channel_title": "My show",
       "authorized_by": "me@example.com", "license_note": "I own this channel"}'

# See what has arrived and what QuickReel did with it
curl localhost:12000/api/v1/triggers/youtube/events
```

The callback the hub calls is
`POST /api/v1/triggers/youtube/notify`; the matching `GET` answers the hub's
verification handshake. Three things about the design are worth knowing:

- **A new video is a draft by default.** `auto_process` is off unless you turn it
  on, so a notification creates a project to review rather than spending money on
  an unattended pipeline. Turn it on per subscription when the channel is trusted.
- **The callback is public and unsigned.** It is defended by a shared secret in
  the path (`QUICKREEL_YOUTUBE_CALLBACK_SECRET`, required in production) and by
  the subscription allowlist: a notification for a channel we did not subscribe
  to is ignored, so a leaked secret does not cause arbitrary processing.
- **Duplicate delivery is normal.** WebSub permits it, so `(channel_id, video_id)`
  is unique and a repeat delivery is a no-op.

Set `QUICKREEL_PUBLISHING_MODE=mock` (the default) and the hub is simulated too:
subscriptions are created and leases tracked in memory, with no outbound calls,
so the whole loop runs in tests and locally. The Channels page in the UI is the
front end for this.

## Performance feedback loop

Publishing is only half of a distribution system. The other half is knowing what
worked. Every published post is re-sampled on a cadence and the platform's
counters — views, likes, comments, shares, average watch time — are stored in
`ClipMetric`. Recording starts from the first publish, not from when someone
turns the loop on, because the learning needs history that already exists.

```bash
# Sample every post whose cadence has elapsed (Celery beat does this hourly)
curl -X POST localhost:12000/api/v1/metrics/run-due

# Read the samples for one clip, or the aggregate
curl localhost:12000/api/v1/clips/{clip_id}/metrics
curl localhost:12000/api/v1/clips/{clip_id}/performance
```

Two properties matter. A sampling failure is never fatal: the loop runs
unattended against rate-limited third-party APIs, so one adapter raising does not
stop the rest of the batch, and an authentication failure flags the target for a
reconnect instead of failing the same way forever. And totals use the newest
sample per platform rather than the sum across samples, because platform
counters are cumulative — summing the series would count the same view once per
sweep.

Sampling is what feeds recalibration, described next.
`QUICKREEL_METRICS_ENABLED=false` turns it off for a deployment that must not
make outbound calls.

## Viral Intelligence: recalibrating the weights

The samples above are only worth recording if something reads them. The scoring
weights started as the brief's design assumptions; recalibration replaces them
with weights fitted to what this workspace's clips actually did.

```bash
# Fit a proposal from recorded engagement (does not change scoring)
curl -X POST localhost:12000/api/v1/scoring/calibration

# Review proposals, and the weights currently in force
curl localhost:12000/api/v1/scoring/calibration
curl localhost:12000/api/v1/scoring/weights

# Install one
curl -X POST localhost:12000/api/v1/scoring/calibration/{id}/apply
```

Proposing and applying are separate calls on purpose. A loop that silently
rewrote the model it is judged by would have no ground truth left to check it
against, so a fit only ever produces a reviewable record: the weights it would
install, the sample it was fitted on, per-platform counts, and diagnostics
including how much of the engagement variance the current weights explain versus
the fitted ones.

The fit itself makes four deliberate choices. Outcomes are ranked *within each
platform* before fitting, because a view on TikTok and a view on Shorts are not
the same unit and one big platform would otherwise dominate the target. The
weights stay a distribution — sum to one with a floor under every factor — so a
factor that looks useless is shrunk, not deleted, on the strength of a sample
too small to justify deleting it. A ridge term pulls the fit toward the current
weights and weakens as evidence accumulates, so early fits barely move. And a fit
that cannot explain engagement better than the weights it would replace is
recorded as not applicable and refused at apply time, which is the honest answer
on thin or noisy data.

Below `QUICKREEL_SCORING_CALIBRATION_MIN_SAMPLES` (default 12) observed clips the
fit returns the prior untouched and says so. `QUICKREEL_SCORING_CALIBRATION_ENABLED=false`
disables the whole path.

An applied vector takes effect on the next analysis run: the pipeline loads the
workspace's active weights when it builds its scorer, so a recalibration changes
what gets clipped from then on. Clips already scored keep the score they were
given, which is what makes the next fit comparable to the last.

## Trends: repurpose, do not re-upload

Trend detection is a legitimate input, but it is not a licence to re-post someone
else's video. Clipping a trending video and publishing it verbatim is exactly the
behaviour that gets an account struck, and no amount of trend signal justifies
it. The rule this project follows:

- **The trend tells you what to make, not what to take.** A trending format,
  topic, or sound is a signal about audience appetite. It is not source footage.
- **Source material is your own or licensed.** The trend feature is aimed at a
  creator's own back catalogue: match a signal from an official API against
  videos you already have the rights to, then make a new clip from that. The
  existing pipeline already records `license_note` and `authorized_by` per
  project, so every clip can answer where its footage came from.
- **The output is a transformation, not a copy.** Captions, vertical reframing,
  boundary optimisation and re-scoring produce a new work from permitted input.
  Re-uploading an untouched third-party clip is not a transformation.

This is also why the platform APIs are asymmetric on purpose: publishing is open,
ingestion is closed. TikTok, Meta, and YouTube all offer sanctioned ways to post
your own content, and none offers a sanctioned way to pull someone else's content
in. Anything that reads as "find a trending video and clip it" is the wrong
shape for this product.

## App review

Publishing to TikTok, Instagram Reels, and YouTube Shorts each requires an
approved developer app. `docs/app-review/` holds the application text, the scope
justifications, and the shared demo script. The scope lists in `oauth.py` are the
source of truth; the documents justify exactly those and nothing more.

## Legal and ethical note

Only ingest video you own or are licensed to use. QuickReel accepts links to
authorised sources; it does not grant rights to the underlying content, and
downloaded platform content remains subject to the platform's terms of service.
You are responsible for having the rights to clip and republish anything you
import.

Record provenance on each project (`license_note`, `authorized_by`) so every
clip carries the basis on which it may be republished. This is both a legal
safeguard and a selling point for creators and agencies.

Note that the platform APIs are asymmetric on purpose: **publishing is open,
ingestion is closed.** TikTok, Meta, and YouTube all offer sanctioned ways to
post your own content, and none offers a sanctioned way to pull someone else's
content in. The trend features are therefore aimed at a creator's own back
catalogue, matched against signals from official APIs, rather than at clipping
third-party videos.

## Security notes

- Upload filenames are never trusted; only a safe extension whitelist is kept.
- Storage keys are validated against directory traversal.
- ffmpeg is invoked with argument lists, never shell strings.
- No secrets are stored in code; all credentials come from the environment.
- Error messages are sanitised before reaching the API response.
- Passwords are hashed with scrypt (memory-hard, parameters stored in the hash
  so they can be raised later); session tokens are random and stored only as a
  SHA-256, so a database leak does not yield live sessions.
- Login answers identically for an unknown address and a wrong password, and
  spends the same time on both, so it cannot be used to enumerate accounts.
  Failed attempts are throttled per address and per client.
- The session cookie is `HttpOnly` and `SameSite=Lax`; the API requires a JSON
  body, which a cross-site HTML form cannot produce, so the two together stand
  in for a CSRF token. `Secure` is set whenever `QUICKREEL_PUBLIC_BASE_URL` is
  HTTPS, which the production guard requires.
- Every content route is scoped to the caller's workspace. Scoped lookups take
  the workspace id as a required argument, so a query cannot be written without
  it, and another tenant's row is a 404 rather than a 403 — the API does not
  confirm which ids exist.
- Platform credentials (`PublishTarget.credentials`) are never serialised back
  out through the API, and each target belongs to a workspace. They are stored
  unencrypted today; encrypt them at rest before handling other people's
  accounts. Use least-privilege OAuth scopes: quickreel needs permission to
  publish, not to read a creator's private analytics or messages.
- Source URLs pass an SSRF guard: scheme allowlist, optional host allowlist, and
  a DNS check that refuses any non-globally-routable address. The check is
  repeated inside the pipeline, immediately before the download.
- A `QUICKREEL_DOWNLOADER_COOKIES_FILE` grants account access. Keep it outside
  the repository, restrict its permissions, and never commit it.
- Playlist URLs download a single item (`noplaylist`), so one link cannot trigger
  an unbounded number of downloads.
