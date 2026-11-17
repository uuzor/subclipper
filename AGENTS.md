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
make test         # pytest (32 tests)
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
- URL ingestion returns `501` on purpose: remote download needs a rights check
  first. Do not enable it without one.
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
