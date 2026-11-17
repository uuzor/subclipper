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
| Publishing to platforms | Not yet | - |
| Performance analytics / Viral Intelligence loop | Not yet | - |

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
```

The default configuration uses deterministic fake AI providers and eager task
execution, so the whole pipeline runs locally with no credentials and no broker.
Swap in real providers by setting `QUICKREEL_TRANSCRIPTION_PROVIDER`,
`QUICKREEL_VISION_PROVIDER` and `QUICKREEL_MOMENT_PROVIDER`.

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

Phased per the brief. Phase 1-5 are implemented here.

1. ~~Video upload and transcription~~
2. ~~AI identification of interesting moments~~
3. ~~Automatic clip extraction~~
4. ~~Vertical video conversion and captions~~
5. ~~Viral scoring and clip ranking~~
6. Face tracking and advanced editing (MediaPipe, per-frame crop offsets)
7. Performance analytics and the Viral Intelligence learning loop

## Legal and ethical note

Only ingest video you own or are licensed to use. QuickReel accepts links to
authorised sources; it does not grant rights to the underlying content, and
downloaded platform content remains subject to the platform's terms of service.
You are responsible for having the rights to clip and republish anything you
import.

## Security notes

- Upload filenames are never trusted; only a safe extension whitelist is kept.
- Storage keys are validated against directory traversal.
- ffmpeg is invoked with argument lists, never shell strings.
- No secrets are stored in code; all credentials come from the environment.
- Error messages are sanitised before reaching the API response.
- Source URLs pass an SSRF guard: scheme allowlist, optional host allowlist, and
  a DNS check that refuses any non-globally-routable address. The check is
  repeated inside the pipeline, immediately before the download.
- A `QUICKREEL_DOWNLOADER_COOKIES_FILE` grants account access. Keep it outside
  the repository, restrict its permissions, and never commit it.
- Playlist URLs download a single item (`noplaylist`), so one link cannot trigger
  an unbounded number of downloads.
