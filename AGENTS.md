# AGENTS.md

Repository-specific guidance for agents working in this codebase.

## What this is

QuickReel: an AI viral clip generator. Long-form video in, ranked 9:16 clips out.
The differentiator is `analysis/` (moment detection, scoring, boundary
optimisation). Treat that package as the product; the rest is plumbing.

## Commands

```bash
make install      # venv + core deps
make install-ai   # optional heavy deps: faster-whisper, google-generativeai, librosa
make test         # pytest
make lint         # ruff check src tests
make fmt          # ruff format src tests
make run          # uvicorn on port 12000
make worker       # celery worker (needs Redis + eager disabled)
```

Always run `make lint` and `make test` before finishing a change. Both must pass.

## Environment

- ffmpeg and ffprobe must be on PATH. Install with
  `sudo apt-get install -y ffmpeg`. The app refuses to boot without them.
- Python 3.11+. The venv lives at `.venv`.
- Tests set `QUICKREEL_*` env vars in `tests/conftest.py` *before* importing the
  app, because settings and the DB engine are cached via `lru_cache`. If you add
  a test that needs different config, change the env var in `conftest.py` or call
  `quickreel.db.reset_engine()` and `quickreel.config.get_settings.cache_clear()`.

## Conventions

- Providers are protocols in `ai/base.py`. Never import a concrete provider
  outside `ai/registry.py`. New providers need a fake in `ai/fakes.py` and a
  branch in the registry, so the pipeline keeps running with no credentials.
- Analysis code is pure: it takes transcript/audio/visual artifacts and returns
  dataclasses. Keep it free of DB and filesystem access so it stays unit-testable.
- Scoring stays explainable. Every score must come with a `score_breakdown`
  containing components, penalties, features and weights. Do not add an opaque
  model that cannot explain itself.
- Shell out to ffmpeg only through `media/ffmpeg.py`, and only with argument
  lists, never shell strings.
- Every user-supplied URL must pass `media/url_source.validate_source_url` before
  any outbound request. Do not call a downloader with an unvalidated URL, and do
  not bypass the DNS check: it is the SSRF boundary.
- Downloaders live behind the `VideoDownloader` protocol in
  `media/downloader.py`. Tests use `FakeDownloader` (installed with
  `set_downloader`) so no test touches the network.
- Settings go in `config.py` under the `QUICKREEL_` prefix and must be documented
  in `.env.example`.

## Testing notes

- `sample_video` fixture generates a real 20s MP4 with ffmpeg. Media and API
  tests use real ffmpeg, not mocks.
- The API tests exercise the full pipeline through `TestClient` with fake AI
  providers and eager Celery.
- Prefer testing real code paths. Do not add mocks unless there is no
  alternative, and justify it in a comment when you do.

## Gotchas

- `db.py` builds the engine lazily. Importing `quickreel.db` does not connect.
  Tests call `reset_engine()` to drop cached engines between cases.
- Celery is configured with `memory://` when `QUICKREEL_REDIS_URL` is empty, so
  `task_always_eager=true` runs everything inline.
- URL ingestion is enabled and goes through the SSRF guard in
  `media/url_source.py`. It needs the optional `download` extra (`yt-dlp`); with
  yt-dlp absent the registry returns a fake that fails URL requests with a clear
  message, so the app still boots. A URL project downloads during `ingest` and
  its `MediaAsset.path` is empty until then; code that touches the path must
  tolerate `""`.
- Do not pass `max_filesize` to yt-dlp. A value of `0` means "zero bytes" and
  silently downloads nothing; yt-dlp reports success with no file. Size is
  enforced after download in `YtDlpDownloader.download`.
- YouTube blocks datacenter IPs, so a real download from this environment can
  fail with `HTTP Error 403` even for a public video. That is a network/geo
  restriction, not a bug; metadata probing still works. Use
  `QUICKREEL_DOWNLOADER_PROXY` or a cookies file if you need real media.
- `Transcript.segments` and `Clip.score_breakdown` are JSON columns; schema
  changes there need no migration but do need readers to tolerate old rows.
- The `ai` extra pins `av<16`. faster-whisper 1.2.x calls `av.open(...,
  metadata_errors=...)`, which PyAV 16 removed. Without the pin, transcription
  raises `TypeError: open() got an unexpected keyword argument 'metadata_errors'`.
- In `auto` mode a provider that fails *during a call* (not at construction)
  falls back to its fake via `_FallbackProxy`, logged as
  `provider.runtime_fallback`. Explicit provider names (`faster-whisper`,
  `gemini`, `llm`) never fall back; they surface the error.

## Verifying the real AI path

Real transcription needs the `ai` extra plus a speech video. espeak-ng makes one:

```bash
sudo apt-get install -y espeak-ng
espeak-ng -v en-us -s 150 -w /tmp/speech.wav "text to speak"
ffmpeg -y -f lavfi -i "color=c=navy:s=1280x720:d=20" -i /tmp/speech.wav \
  -shortest -c:v libx264 -preset ultrafast -pix_fmt yuv420p -c:a aac /tmp/podcast.mp4
```

Then upload and process through the API. The first run downloads the Whisper
model from Hugging Face, so it needs network access.

## Database schema changes

There is no migration framework. `Base.metadata.create_all` never alters an
existing table, so adding a column silently breaks every existing database with
`no such column: <name>`. `init_db` therefore calls `_add_missing_columns`,
which adds any model column missing from the live table. Only additive,
nullable-or-defaulted columns are handled; anything else is skipped with a
warning. If you need to drop, rename or retype a column, add a real migration
step rather than extending that helper.

## Provider provenance and fallback

A run records which provider actually served each stage on `Project.providers`
(`transcription`, `audio`, `vision`, `moment`) and exposes it as
`ProjectOut.providers`. This matters because in `auto` mode a failing real
provider degrades to its deterministic fake and the project still reports
`ready` - fake-but-plausible clips, no error. Set
`QUICKREEL_ALLOW_PROVIDER_FALLBACK=false` in production so the run fails loudly
instead. Tests assert provenance in `test_full_pipeline_produces_ranked_clips`.

## Testing real providers without network or credentials

`LLMMomentProvider` takes an optional `transport=` argument. Tests pass
`httpx.MockTransport(handler)` to drive the real request/retry/chunking code with
no network. `google.generativeai` is deprecated in favour of `google.genai`; the
vision provider still uses the former, so a migration is pending.
