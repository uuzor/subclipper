# Architecture

## Pipeline

```
 ingest ─ probe ─ extract audio ─ transcribe ─ audio analysis
                                          └─ visual analysis
                                                   |
                                    moment detection ─ scoring ─ ranking
                                                   |
                              boundary optimisation ─ persist clips
                                                   |
                                        render 9:16 MP4 + captions
```

`pipeline/orchestrator.py` owns these stages and every `Project` state
transition. Each stage is a method, so it can be retried or tested alone.
`pipeline/tasks.py` wraps `Pipeline.run` in a Celery task; with
`QUICKREEL_TASK_ALWAYS_EAGER=true` the task executes in-process and no broker is
needed.

## Ingestion sources

A project starts from one of two sources, and both converge before `probe`:

- **Upload** (`POST /api/v1/projects/{id}/upload`) writes the file to the uploads
  directory and records its path on the `MediaAsset`.
- **URL** (`POST /api/v1/projects` with `source_url`, or
  `POST /api/v1/projects/{id}/source-url`) records `source_kind="url"` and the
  URL, plus a `MediaAsset` with an empty path. During `ingest`, the pipeline
  downloads the video with yt-dlp and fills in the path.

Everything downstream sees only a local file path, so URL and upload projects are
indistinguishable from the transcription stage onward.

### SSRF protection

`media/url_source.py` is the single gate for user-supplied URLs. It enforces a
scheme allowlist, rejects embedded credentials, applies an optional host
allowlist, and resolves the hostname to check that every address is globally
routable. Private, loopback, link-local, reserved and cloud-metadata addresses
are refused, including IPv4-mapped IPv6 forms and public names that resolve to
private addresses. The check runs on project creation and again inside the
pipeline immediately before the download, so the value actually fetched is the
value validated.

### Downloader abstraction

`media/downloader.py` defines a small `VideoDownloader` protocol with `probe`
(metadata only) and `download`. `YtDlpDownloader` is the real implementation;
`FakeDownloader` copies a local file and needs no network, which is what the test
suite uses. `get_downloader()` selects the implementation and degrades to a fake
that reports a clear error when yt-dlp is not installed, so the app never fails
to boot because of an optional dependency.

Downloads are capped by resolution (`download_max_height`) and by size
(`max_upload_bytes`), and `noplaylist` keeps a playlist link from fanning out into
many downloads.

## Why a multi-model pipeline

No single model does every job well. Speech recognition, visual understanding
and moment reasoning are separated behind the protocols in `ai/base.py`:

| Stage | Protocol | Real options | Fake |
| --- | --- | --- | --- |
| Transcription | `TranscriptionProvider` | faster-whisper | `FakeTranscriptionProvider` |
| Vision | `VisionProvider` | Gemini | `FakeVisionProvider` |
| Moment reasoning | `MomentProvider` | OpenAI-compatible LLM | `FakeMomentProvider` |
| Audio events | `LibrosaAudioAnalyzer` | librosa | `FakeAudioAnalyzer` |

The pipeline depends only on the protocols. `ai/registry.py` resolves the
configured provider, and with `auto` it falls back to the fake (logging why)
when a dependency or credential is missing. That is what makes the whole system
testable and runnable offline.

## Viral scoring engine

`analysis/scoring.py` computes

```
score = 100 * sum(weight_i * component_i) - penalties
```

Components come from `analysis/features.py`, which derives every signal from
pipeline artifacts (transcript, audio events, visual analysis), so scoring is a
pure function. Each clip stores a `score_breakdown` containing:

- `components`: per-factor weight, normalised value, contribution and explainer
- `penalties`: which rules fired and how many points they cost
- `features`: the raw signals, for debugging and recalibration
- `weights`: the exact weights used

Weights default to the brief's initial design assumptions and are normalised to
sum to 1.0 at construction. Calibrating them against real engagement data is the
Phase 7 "Viral Intelligence" work, implemented in `analysis/calibration.py`:
persist outcome data per clip (views, retention, shares), fit the weights to it,
and install a fit explicitly. Because the breakdown is stored alongside every
score, recalibration does not require re-running the pipeline.

## Boundary optimisation

`analysis/boundaries.py` turns a semantic span into a cut point. Steps, in
order: include the setup sentence, snap the end to a sentence boundary, extend
for a trailing reaction, snap lead-in/lead-out to nearby silence, then enforce
minimum and maximum durations. Each adjustment is recorded on the clip as
`boundary_adjustments` so the timeline UI can show why a clip starts and ends
where it does.

## Rendering

`rendering/renderer.py` builds an SRT with timings relative to the clip start,
then calls `media/ffmpeg.py:render_vertical_clip`, which scales to cover
1080x1920 and centre-crops. Audio is loudness-normalised to broadcast-ish
targets (`loudnorm=I=-16:TP=-1.5:LRA=11`).

## Extension points

### Face tracking (Phase 6)

`render_vertical_clip` currently emits a static centre crop. A MediaPipe
face-tracker plugs in by replacing the single `crop` filter with a per-frame x
offset, for example by building an expression over the detected face centre and
passing it to `crop=1080:1920:x='<expr>':y=0`. The renderer already isolates the
filter graph, so this is a local change.

### Viral Intelligence loop (Phase 7)

1. Publish a clip, capture its platform id (`ScheduledPost.external_post_id`).
2. Poll or receive views, retention, shares and comment counts; store samples in
   `ClipMetric`. Recording starts from the first publish rather than when the
   loop is switched on, because the learning needs history that already exists.
   **Implemented** in `publishing/metrics.py`: an hourly Celery beat sweep
   samples every published post whose cadence has elapsed, via the adapter's
   `fetch_metrics`. `metrics_enabled=false` turns it off for a deployment that
   must not make outbound calls.
3. Store outcomes against the clip. **Implemented** — `GET
   /api/v1/clips/{id}/performance` aggregates the samples into latest-per-platform
   plus lifetime totals.
4. Refit `scoring_weights` and, if useful, the penalty thresholds.
   **Implemented** in `analysis/calibration.py`. A fit reads every clip that has
   both `score_components` and recorded samples, ranks outcomes within each
   platform, and fits the six weights by projected gradient descent under three
   constraints: the vector sums to one, every factor keeps a floor, and a ridge
   term pulls toward the current weights with a strength that decays as the
   sample grows. A proposal is stored as a `ScoringCalibration` row and applied
   only by an explicit call, so the model never silently rewrites the thing it
   is judged by. A fit that cannot explain more engagement variance than the
   weights it would replace is recorded as not applicable and refused at apply
   time. The pipeline loads a workspace's applied vector when it builds its
   scorer, so an installed fit changes what gets clipped from the next run;
   existing clips keep the score they were given, which keeps the next fit
   comparable.

   Two things are deliberately left for later. Penalty thresholds are not
   refitted, because a penalty fires on a property of the clip rather than a
   continuous factor and needs a different estimator. And a fit is per
   workspace, not global: one audience's signal is not another's, so a shared
   vector would blend two different things.
5. Feed the new weights back through configuration. **Implemented** — an applied
   `ScoringCalibration` is the workspace's active vector, read by
   `Pipeline._active_weights`; configuration remains the fallback for a workspace
   that has never applied a fit.

The `score_breakdown` persisted per clip is the training input.

### Distribution and scheduling

`src/quickreel/publishing/` publishes rendered clips to connected accounts and
is built so a platform is a plug-in rather than a fork:

```
ScheduledPost ──> registry.get_adapter(platform) ──> PlatformAdapter.publish()
      │                                                      │
      └── PublishAttempt (one row per call, full retry history)
                                                             │
                       ClipMetric <── fetch_metrics() <──────┘
```

`PlatformAdapter` (`publishing/base.py`) has three methods: `publish`,
`check_status`, and `fetch_metrics`. The adapter's exception type drives the
retry policy — `TransientError`/`RateLimitError` reschedule with backoff,
`PermanentError`/`AuthError` are terminal. Adding a platform means implementing
the protocol and registering it in `publishing/platforms.py`; the scheduler,
models, and HTTP routes are untouched.

Scheduling is a sweep rather than a per-post timer: Celery beat calls
`quickreel.run_due_posts` every minute, which publishes whatever is due. Each
post is claimed with an atomic conditional update, so overlapping sweeps cannot
double-post. Backoff is expressed as `ScheduledPost.next_attempt_at`, which keeps
retry state in the database instead of in a queue that a restart would lose.

`QUICKREEL_PUBLISHING_MODE=mock` resolves every platform to the deterministic
mock adapter, so the whole loop is exercisable with no credentials. In `live`
mode an unregistered platform raises instead of silently using the mock.

### Async workers and storage

Set `QUICKREEL_REDIS_URL` and `QUICKREEL_TASK_ALWAYS_EAGER=false` to move
processing to Celery workers. `storage.py` provides a local backend and an
S3-compatible backend behind the same interface for multi-node deployments.

## Data model

```
Project 1─1 MediaAsset      (path filled at upload, or after URL download)
Project 1─1 Transcript      (segments JSON: start, end, text, speaker)
Project 1─n Clip            (boundaries, score, score_breakdown, render_path)

Clip        n─n ScheduledPost  (one row per clip/platform/time)
PublishTarget 1─n ScheduledPost
ScheduledPost 1─n PublishAttempt  (full retry history, never overwritten)
ScheduledPost 1─n ClipMetric      (performance samples for the learning loop)
```

`Project.source_kind` is `upload` or `url`; `Project.source_url` holds the
validated link for URL projects. A URL project's `MediaAsset` starts with an
empty path, which the ingest stage fills in once the download completes.

`Project.license_note` and `Project.authorized_by` record provenance: on what
basis this material may be republished. They are surfaced through the API so a
creator or agency can see it, and so a clip can always answer the question.

`Clip` keeps both the raw detector boundaries and the optimised boundaries, so
the effect of boundary optimisation is always auditable.

`PublishTarget.credentials` holds OAuth material for a connected account and is
stored as-is today. A production deployment should encrypt it at rest; the field
is deliberately opaque to the API layer, which never serialises it back out.
