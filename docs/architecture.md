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
Phase 7 "Viral Intelligence" work: persist outcome data per clip (views,
retention, shares) and fit the weights to it. Because the breakdown is stored
alongside every score, recalibration does not require re-running the pipeline.

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

1. Publish a clip, capture its platform id.
2. Poll or receive views, retention, shares and comment counts.
3. Store outcomes against the clip.
4. Refit `scoring_weights` and, if useful, the penalty thresholds.
5. Feed the new weights back through configuration.

The `score_breakdown` persisted per clip is the training input.

### Async workers and storage

Set `QUICKREEL_REDIS_URL` and `QUICKREEL_TASK_ALWAYS_EAGER=false` to move
processing to Celery workers. `storage.py` provides a local backend and an
S3-compatible backend behind the same interface for multi-node deployments.

## Data model

```
Project 1─1 MediaAsset      (path filled at upload, or after URL download)
Project 1─1 Transcript      (segments JSON: start, end, text, speaker)
Project 1─n Clip            (boundaries, score, score_breakdown, render_path)
```

`Project.source_kind` is `upload` or `url`; `Project.source_url` holds the
validated link for URL projects. A URL project's `MediaAsset` starts with an
empty path, which the ingest stage fills in once the download completes.

`Clip` keeps both the raw detector boundaries and the optimised boundaries, so
the effect of boundary optimisation is always auditable.
