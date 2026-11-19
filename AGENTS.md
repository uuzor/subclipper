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

## Frontend

`frontend/` is a Vite + React + TypeScript app served on port 5173. It proxies
`/api` to the backend on port 12000, so run both (`make run` plus
`npm run dev` in `frontend/`). Build with `npm run build` (`tsc -b && vite build`);
this must pass cleanly - `tsc` fails the build on any type error.

The visual world is "The Cutting Room": paper (`#e9e7e1`) for planning
surfaces, screen (`#0b0c0d`) for the timeline, cobalt (`#1b3ae8`) as the only
accent. Tokens live in `src/styles/tokens.css`; keep new UI on those tokens
rather than inventing colours.

`src/lib/types.ts` mirrors `quickreel.schemas`. When a response field changes on
the backend, update that file in the same change or the build breaks.

### Serving the built UI from the API

`QUICKREEL_FRONTEND_DIR` (default empty) makes the FastAPI process also serve the
Vite build, so the whole app runs on one origin with no proxy or CORS. `make
serve` builds `frontend/dist` and starts uvicorn with that variable set on
`0.0.0.0:12000`. `_mount_frontend` in `main.py` mounts `/assets` and a catch-all
`_SpaStaticFiles` at `/` that falls back to `index.html`, so client-side routes
survive a refresh. Routers are registered before the mount, so `/api` and
`/healthz` still win; an unknown API path returns the JSON 404, not the shell.

Two deployment traps this handles: the container's port forward only reaches the
process if uvicorn binds `0.0.0.0` (binding `127.0.0.1` yields a 502 from the
public URL), and a configured-but-unbuilt `frontend_dir` logs a warning instead
of crashing the server.

## Response schemas must tolerate legacy rows and derive missing fields

`ClipOut` and `ProjectOut` read ORM rows via `from_attributes`. Two traps:

- A JSON column with `default=dict` only defaults on *insert*. Rows written
  before the column existed read back `None`, and a bare `dict` field then raises
  a `ResponseValidationError` (a 500 from the endpoint). Coerce `None -> {}` in a
  `mode="before"` validator. Covered by `tests/test_schemas.py`.
- `Clip` has no `duration_seconds` column, but `ClipOut` exposes one. Without a
  `model_validator` deriving it from `end_seconds - start_seconds`, every clip
  reports `0.0` and the UI shows "0s". Derive it; do not add a stored column
  unless you also add it to the model and the missing-column helper handles it.

## Publishing and the post scheduler

`src/quickreel/publishing/` owns everything about getting a rendered clip onto a
platform. The split exists so adding a platform is additive:

- `base.py` — the `PlatformAdapter` protocol (`publish`, `check_status`,
  `fetch_metrics`) and the error vocabulary. **Error classification is part of
  the contract**: the scheduler decides whether to retry from the exception
  type, so an adapter that reports a permanent rejection as transient makes the
  system hammer a platform that already said no.
- `registry.py` — resolves a platform name to an adapter. `QUICKREEL_PUBLISHING_MODE=mock`
  returns the deterministic mock for any platform; `live` requires a registered
  real adapter and raises rather than falling back, because a publish that
  quietly went to a mock in production is worse than a loud failure.
- `mocks.py` — deterministic adapter with injectable failures (`fail_times`,
  `failure=transient|rate_limit|permanent|auth`) so every retry branch is
  testable offline.
- `scheduler.py` — claims due posts and drives retries.
- `platforms.py` — where real adapters register themselves. Empty today.

Two invariants in `scheduler.py` matter and should not be relaxed:

1. **A post is never published twice.** `_claim` is a conditional UPDATE
   (`scheduled`/`failed` -> `publishing`) whose `rowcount` is checked, so two
   workers racing for the same post produce exactly one publish. A "select then
   update" would let both win.
2. **A failure is never retried blindly.** `TransientError`/`RateLimitError`
   reschedule with backoff; `PermanentError`/`AuthError` are terminal. A
   terminal failure clears `next_attempt_at`, otherwise the post stays in the
   "due when the backoff elapses" set forever. `AuthError` also flips the target
   to `needs_reauth` so the UI can ask for a reconnect.

A platform-supplied `retry_after_seconds` always wins over the computed backoff:
that value is the platform saying exactly when it will accept a request again.

## External triggers: ingest without a human pasting a URL

`src/quickreel/triggers/` is the "a new video exists, go process it" path. It is
deliberately separate from `pipeline/` (which assumes a project exists) and
`api/` (which is transport). The mechanism is WebSub (PubSubHubbub): we ask a
hub to watch a channel, the hub POSTs to our callback when it publishes, and we
create a project for review.

- `base.py` — the `HubClient` protocol and the failure vocabulary
  (`TransientHubError` vs `PermanentHubError`). Same reasoning as publishing: a
  4xx to a subscribe call will keep being a 4xx, so retrying hides the problem.
- `feed.py` — parses the untrusted notification body. Uses `defusedxml` when
  present (entity-expansion and XXE defence), caps the body size, and
  **shape-checks the video and channel ids** before they reach a URL. A video id
  is never interpolated into a URL without passing `^[A-Za-z0-9_-]{11}$`.
- `hub.py` — `MockHubClient` and `HttpHubClient`, plus `default_hub_client()`,
  which follows `effective_publishing_mode` so there is one notion of live/mock
  in the app rather than two that can disagree.
- `youtube.py` — subscription lookup, ingest policy, idempotency.
- `service.py` — create / renew / revoke subscription lifecycle.

Invariants worth keeping:

1. **The callback is public and unsigned.** Its defences are a shared secret in
   the path (`QUICKREEL_YOUTUBE_CALLBACK_SECRET`, required in production), an
   optional `X-Hub-Signature` HMAC, and a subscription allowlist. A notification
   for a channel we did not subscribe to is ignored, so a leaked secret does not
   cause processing of arbitrary channels.
2. **Always answer 2xx to a received notification.** The hub cannot fix a
   malformed body by retrying, and a 4xx makes it mark the subscription
   unhealthy. Parse failures return 204 and are logged; the event log records
   what happened.
3. **Idempotency is a unique constraint, not a lookup.** WebSub permits duplicate
   delivery. `(channel_id, video_id)` is unique on `youtube_events`; the lookup
   in `handle_notification` is the fast path and the `IntegrityError` catch is
   the real guard against two concurrent deliveries.
4. **A new video is a draft by default.** `QUICKREEL_YOUTUBE_AUTO_SCHEDULE=false`
   means a notification creates a project for review. Auto-processing an
   unattended channel spends money, so it is opt-in per subscription.
5. **Leases are renewed before they lapse.** A WebSub lease expires silently: the
   hub just stops calling. `renew_due_subscriptions` runs from a Celery beat
   task and renews within `QUICKREEL_YOUTUBE_RENEW_BEFORE_SECONDS` of expiry.

The callback is `POST /api/v1/triggers/youtube/notify[/{secret}]`, and
verification is the matching GET (it echoes `hub.challenge`). Subscription
management is under `/api/v1/triggers/youtube/subscriptions`.

## App-review materials live in docs/app-review/

`docs/app-review/` holds the TikTok, Meta/Instagram, and Google/YouTube
application text, the shared demo script, and the scope justifications. The rule
for editing them: **do not claim a capability the code does not have.** The scope
lists in `oauth.py` are the source of truth; each document justifies exactly
those and nothing more. When a scope changes in `oauth.py`, update the matching
table in `docs/app-review/README.md` in the same change, or the form and the code
disagree and the review bounces.

## Timestamps must be timezone-aware

`UTCDateTime` in `models.py` is used for every timestamp column. SQLite discards
`tzinfo` on write and returns naive datetimes, so comparing a loaded value
against `datetime.now(UTC)` raises `TypeError: can't compare offset-naive and
offset-aware datetimes`. Postgres with `timezone=True` returns aware values, so
the two backends disagree. The decorator normalises both directions. Use it
rather than a bare `DateTime(timezone=True)` for any new timestamp column.

## Platform APIs: publish is open, ingest is closed

Worth knowing before designing around these, because the asymmetry is deliberate:

- **TikTok** Content Posting API supports direct post and scheduling, but
  **unaudited clients can only post privately** until the app passes TikTok's
  audit. URL-based posting requires a **verified domain**. Both are app-review
  gates, not code problems.
- **Meta** Reels publishing requires a Business/Creator account plus app review.
- **YouTube** has no download endpoint at all, and its API Services Terms grant
  no licence to reproduce or distribute audiovisual content outside the API
  Services. That is why datacenter IPs get 403 on media while metadata succeeds
  (see the YouTube ingestion section above).

The product consequence: ingest is upload-first (creator's own material), and
trend signals from official APIs are aimed at the creator's own back catalogue
rather than at third-party videos. Provenance (`license_note`, `authorized_by`
on `Project`) is recorded so a clip can always answer on what basis it may be
republished.

## Authentication and tenancy

Email and password only. Every account belongs to at least one workspace, and
every piece of content belongs to a workspace. There is no way to read or write
content without a session and a workspace.

The pieces:

- `auth.py` — password hashing (scrypt, parameters stored in the hash), session
  token generation/hashing, and the in-process `LoginThrottle`. No DB, no HTTP.
- `auth_service.py` — users, workspaces, memberships, sessions. The policy layer;
  reachable from a test or a script without going through HTTP.
- `api/deps.py` — `current_principal`, `current_workspace`, `require_owner`, the
  cookie helpers, and the scoped-lookup helpers.
- `api/routes/auth.py` — signup, login, logout, `/me`, workspace switch,
  password change. The only routes reachable with no session (plus health and
  the WebSub callback).

Invariants worth keeping:

1. **The workspace is a required argument, never looked up.** Scoped helpers
   (`get_scoped_project`, `scoped_or_404`) take `workspace_id` as a positional
   requirement, so a handler cannot forget to filter — the query cannot be
   written without it. A row in another tenant and a row that does not exist both
   return 404, so the API is not an oracle for which ids are real.
2. **Sessions are rows, not signed tokens.** Logout and revocation have to
   actually work, and a JWT cannot be un-issued. Only the SHA-256 of the token is
   stored, so a database leak does not hand over live sessions. A password change
   deletes every session for the user, including the one making the request.
3. **Login cannot be used to enumerate accounts.** A missing account and a wrong
   password return the same status and the same message, and the missing-account
   path verifies against a dummy hash so it costs the same time. Signup is the
   one unavoidable exception (it has to say an address is taken).
4. **The cookie is the session; there is no bearer token.** `HttpOnly` keeps it
   from JavaScript, `SameSite=Lax` keeps it off cross-site POSTs, and the API
   requires a JSON body that a cross-site HTML form cannot produce. Together
   those stand in for a CSRF token. `Secure` is set whenever the public origin is
   HTTPS — in production the config guard requires an HTTPS `public_base_url`, so
   it is always on there.
5. **OAuth state carries the workspace.** The callback is a cross-site redirect
   with no session cookie, so the tenant it files the account under is the one
   the server signed into the state (`<nonce>.<issued>.<workspace>.<platform>.<hmac>`),
   never one supplied by the request.

### Tests and the session cookie

`tests/conftest.py` provides two authenticated clients, because the content
routes now 401 without a session:

- `client` — no session. Use it for the public routes and to assert that
  something is refused.
- `auth_client` — a signed-up user. This is the default for content tests.
- `second_auth_client` — a second user in a separate workspace, for isolation
  tests (one tenant must not see the other's rows).

Two traps that cost time to find:

- **The test clients use `base_url="https://testserver"`.** A `Secure` cookie is
  not sent over `http://`, so with the default `http://testserver` every
  authenticated test fails with a 401 that has nothing to do with what it tests.
- **The test database persists between runs** (`tests/.tmp/test.db`). Fixed
  emails and fixed ids therefore collide on the second run (a 409 from signup, a
  UNIQUE violation on a literal id). `tests/test_auth.py` mints a unique address
  per test through the `mail` fixture; do the same rather than hardcoding one.
  Delete `tests/.tmp/test.db` to test a fresh-database path.

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

## Vision frames and the Gemini SDK

`GeminiVisionProvider._load_frame` must return the SDK's blob **dict** form
(`{"mime_type": ..., "data": ...}`). The SDK rejects raw `bytes`, and PIL is not
a dependency, so a bare `path.read_bytes()` raises
`TypeError: Could not create Blob` and surfaces as a 500 from the process
endpoint. Covered by `test_gemini_load_frame_returns_sdk_blob_dict`.

The default `QUICKREEL_GEMINI_MODEL` is `gemini-flash-lite-latest`. Older names
such as `gemini-2.0-flash` and `gemini-2.5-flash` are retired and return 404 for
new API keys; higher-tier flash models can return 503 under load.

## YouTube ingestion is blocked from datacenter IPs

`yt-dlp` metadata extraction works, but every media download path returns
HTTP 403 (`unable to download video data`) or "Sign in to confirm you're not a
bot" from cloud IPs, for every player client, including audio-only. URL
ingestion therefore fails loudly with a 422 naming the 403 - it does not produce
fake clips. Uploads are unaffected; local development can generate a source with
`espeak-ng` + `ffmpeg` as in `examples/clips/README.md`.

## Publishing: scheduler, adapters, and the metrics loop

`src/quickreel/publishing/` is a plug-in seam, not a platform-specific module.
An adapter implements `PlatformAdapter` (`base.py`): `publish`, `fetch_status`,
`fetch_metrics`, plus the error classes that classify a failure
(`AuthError`/`PermanentError`/`RateLimitError`/`TransientError`). Adding a real
platform is a new module plus one entry in `registry.py`; the scheduler, models
and routes do not change. `mocks.py` is a deterministic adapter whose failure
modes are injectable, so every retry branch is tested offline.

`QUICKREEL_PUBLISHING_MODE=mock` (the default) resolves every platform to the
mock. `live` requires a registered real adapter and raises rather than silently
falling back — a mock that reports success in production is the worst failure
mode this subsystem has.

`publishing/metrics.py` is the performance feedback loop. An hourly Celery beat
entry (`quickreel.collect_metrics`) samples every published post whose cadence
has elapsed and writes a `ClipMetric` row. Two invariants: a sampling failure
never aborts the batch (one adapter raising must not stop the rest), and an
`AuthError` flags the target `needs_reauth` instead of retrying forever. Totals
in `clip_performance_summary` use the newest sample per platform, never the sum
across samples — platform counters are cumulative, so summing the series counts
the same view once per sweep.

`pipeline/celery_app.py` had a broken relative import (`from .config` instead of
`..config`) that nothing imported, so tests passed while `make worker` could not
start. If you touch the worker, import the module in a test.

`analysis/calibration.py` is the read half of that loop: it fits the scoring
weights to the recorded samples. Invariants worth not breaking:

- **A fit never applies itself.** `propose_calibration` writes a
  `ScoringCalibration` row; installing it is a separate explicit call. A loop
  that silently rewrote the model it is judged by has no ground truth left.
- **A fit that cannot beat the prior is refused.** If `fitted_r_squared <=
  prior_r_squared` the proposal is recorded as `applicable: false` and
  `apply_calibration` raises. Do not relax this to make a demo look better.
- **Weights stay a distribution.** Projected onto sum-to-one with a
  `WEIGHT_FLOOR` under each factor. Never zero a factor on a small sample.
- **Outcomes are ranked within each platform** before fitting, because a TikTok
  view and a Shorts view are not the same unit.
- **Pure standard library.** numpy is only in the optional `ai` extra, so
  calibration must not import it or the core install breaks.

`Pipeline._active_weights` is the only place weights are resolved for scoring;
it prefers the workspace's applied vector and falls back to configuration. A
per-request `ViralScorer()` built from settings would silently ignore every
applied calibration, which is the bug this seam exists to prevent.

## Trends: repurpose, do not re-upload

Trend detection is a signal about what to make, not source footage to re-post.
The trend feature targets a creator's own back catalogue, matched against
official API signals; every project records `license_note` and `authorized_by`.
See the README section "Trends: repurpose, do not re-upload".

## Deployment: Render, and the two traps it exposed

The app ships as one container (`Dockerfile`, `render.yaml`, `docs/deployment.md`)
running uvicorn with eager Celery tasks. Two failures only appeared on a real
deploy, and both are easy to reintroduce.

**Optional-dependency fallbacks must fail in the constructor.** The registry
chooses between a real provider and its fake by catching `ProviderUnavailable`
from the constructor. `LibrosaAudioAnalyzer` originally imported librosa inside
`analyze()`, so the constructor always succeeded, the fallback never ran, and
every project failed at the audio stage on an image built without the `ai`
extra. Probe with `importlib.util.find_spec` in `__init__`.

**Cap encoder threads.** x264 sizes its frame pools from the detected core
count, and a container inherits the host's count rather than its own memory
limit. On a 512MB instance 16 threads is OOM-killed mid-render; because eager
mode encodes inside the web process, that takes the entire service down and
every request returns 502 until it restarts. `QUICKREEL_RENDER_THREADS`
(default `2`) caps both the encoder and the filter graph.

Related: `/healthz` is the liveness route and `/readyz` names the provider
behind each stage. The SPA catch-all answers unknown paths with HTML, so a
health check pointed at `/health` passes on static files alone.
