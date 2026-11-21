# Deploying QuickReel

The deployment is one container: FastAPI serves the API and the built React app
from the same origin, with PostgreSQL for state and S3-compatible object
storage for uploads and renders. `Dockerfile` is the whole configuration.

## Northflank

The live deployment runs as a **combined** service in project `unboundable`.
A combined service builds the image and runs it, so there is no separate build
and deployment service to keep in sync.

1. Create the service from this repository: type **combined**, branch to deploy,
   Dockerfile `/Dockerfile`, build context `/`, port `http` on `8000` marked
   public. The health check is an HTTP readiness probe on `/healthz` port `8000`
   with a 20 second initial delay; the app needs that long to connect to
   Postgres and run `init_db`.
2. Create a secret group and link it to the service. Every setting below goes in
   that group as a variable, so a redeploy carries the configuration with it.
3. Deploy. The first build takes a few minutes because the image installs
   ffmpeg.

### Required configuration

| Variable | Value |
| --- | --- |
| `QUICKREEL_ENV` | `production` |
| `QUICKREEL_DATABASE_URL` | Neon Postgres connection string |
| `QUICKREEL_PUBLIC_BASE_URL` | `https://clipviralmoment.com` |
| `QUICKREEL_CORS_ORIGINS` | the same origin |
| `QUICKREEL_STORAGE_BACKEND` | `s3`, plus bucket and credentials |
| `QUICKREEL_OAUTH_STATE_SECRET` | `openssl rand -hex 32` |
| `QUICKREEL_YOUTUBE_CALLBACK_SECRET` | `openssl rand -hex 32` |

At least one AI provider must be set. The deployed configuration uses three,
each doing the job it is best at:

| Variable | Provider |
| --- | --- |
| `QUICKREEL_SMALLEST_API_KEY` | transcription (`smallest-pulse`) |
| `QUICKREEL_GEMINI_API_KEY` | vision (`gemini`) |
| `QUICKREEL_LLM_API_KEY`, `QUICKREEL_LLM_BASE_URL`, `QUICKREEL_LLM_MODEL` | moment selection (`llm-moments`) |

A single `QUICKREEL_NVIDIA_API_KEY` still covers all three stages, which is the
shortest path to a working deployment, but its transcription estimates word
timings rather than measuring them, so clip boundaries drift. Prefer a
purpose-built ASR model wherever one is available.

`QUICKREEL_PUBLIC_BASE_URL` must be an `https://` origin. It is the OAuth
redirect base and the source of the Secure-cookie decision, and production mode
refuses to boot without it.

### No persistent volume

Northflank's free tier provides no persistent disk, so the deployment is built
around that:

- **Postgres carries all state.** Point `QUICKREEL_DATABASE_URL` at Neon. SQLite
  at `/tmp/data` would lose every row on each redeploy.
- **S3 carries every file.** With `QUICKREEL_STORAGE_BACKEND=s3` the source
  video and each render live in the bucket, and the download and stream
  endpoints hand back presigned URLs. Without it a restart keeps the database
  row and loses the MP4, leaving clips marked `rendered` with nothing to play.
- **Local disk is scratch only.** `/tmp/data` is ephemeral working space for the
  ffmpeg job in flight. Never treat it as durable.

### Custom domain

The service's `*.code.run` hostname is a placeholder. To serve
`clipviralmoment.com`:

1. Register the domain. It must exist before it can be attached: Northflank
   rejects a domain it does not recognise with
   `Subdomain clipviralmoment.com not found`.
2. Add the domain to the service's `http` port, along with `www`. Northflank
   returns the DNS records to create.
3. Create those records at the registrar and wait for propagation.
4. Set `QUICKREEL_PUBLIC_BASE_URL` and `QUICKREEL_CORS_ORIGINS` to
   `https://clipviralmoment.com` and redeploy, so session cookies and OAuth
   callbacks use the real origin.

Until step 4, the app is fully functional on the `*.code.run` hostname. The two
must be changed together, because a mismatch between the served origin and
`QUICKREEL_PUBLIC_BASE_URL` breaks login: the cookie is set for one origin and
sent to another.

### Why production mode is strict

`QUICKREEL_ENV=production` refuses to boot unless the configuration is safe:

| Condition | Why it aborts boot |
| --- | --- |
| `QUICKREEL_PUBLISHING_MODE=mock` | A publish would report success while reaching no platform. |
| Empty `QUICKREEL_OAUTH_STATE_SECRET` | Account linking would be forgeable (login CSRF). |
| Empty `QUICKREEL_YOUTUBE_CALLBACK_SECRET` | The WebSub callback is public and unsigned. |
| Non-HTTPS `QUICKREEL_PUBLIC_BASE_URL` | The session cookie would travel in the clear. |

These are startup failures on purpose. Each one is a silent-wrong-behaviour bug
that would otherwise surface as a user noticing their posts never appeared.

### Topology, and when to change it

The single container runs Celery tasks in-process (`QUICKREEL_TASK_ALWAYS_EAGER`).
A long video therefore occupies the request that started it, and the HTTP
response waits for the pipeline. That is the honest trade for a deployment
without a persistent volume: the container's disk is scratch, so there is no
shared filesystem a second worker could mount.

Scale out in this order:

1. Storage is already S3 (`QUICKREEL_STORAGE_BACKEND=s3`), so uploads and
   renders are already visible to any replica.
2. Add Redis and set `QUICKREEL_REDIS_URL` with
   `QUICKREEL_TASK_ALWAYS_EAGER=false`.
3. Run the same image as a second service whose start command is
   `celery -A quickreel.pipeline.celery_app.celery_app worker`, and a third
   running `... beat` if the publishing and metrics schedules should fire.

`QUICKREEL_RENDER_MAX_CONCURRENT` (default `1`) keeps one encode at a time per
instance, which is what makes a 512MB container viable.

### Memory, and why the renderer caps its threads

A 512MB instance is enough to serve and analyze, but not to encode with every
core the host reports. x264 sizes its frame pools from the detected core count,
and a container inherits the host's count regardless of its own memory limit:
16 threads on a 512MB instance is OOM-killed mid-render, and because the encoder
runs in the web process the OOM killer takes the whole service down instead of
failing one clip.

`QUICKREEL_RENDER_THREADS` (default `2`) caps both the encoder and the filter
graph. Two threads render 1080x1920 comfortably inside 512MB. Raise it only
alongside the instance size.

### Image size

The default image omits the `ai` extra, which keeps torch and Whisper out of the
build. That is fine while transcription runs through a hosted provider. To
transcribe locally, build with `--build-arg INSTALL_AI=1`; the image grows by
several hundred MB.

## Any Docker host

```bash
docker build -t quickreel .
docker run -p 8000:8000 \
  -e QUICKREEL_ENV=production \
  -e QUICKREEL_DATABASE_URL='postgresql://user:pass@host/db' \
  -e QUICKREEL_PUBLIC_BASE_URL='https://example.com' \
  -e QUICKREEL_OAUTH_STATE_SECRET="$(openssl rand -hex 32)" \
  -e QUICKREEL_YOUTUBE_CALLBACK_SECRET="$(openssl rand -hex 32)" \
  -e QUICKREEL_PUBLISHING_MODE=live \
  -e QUICKREEL_STORAGE_BACKEND=s3 \
  quickreel
```

The database defaults to SQLite, which is only durable when a volume is
mounted at `QUICKREEL_DATA_DIR`. A host without a volume loses every row on
redeploy, so point `QUICKREEL_DATABASE_URL` at Postgres there. A `postgres://`
or `postgresql://` URL is accepted and rewritten to the psycopg 3 driver the
project installs, so a provider's connection string works unmodified.

Rendered clips are also uploaded to S3-compatible storage when
`QUICKREEL_STORAGE_BACKEND=s3`. The container's filesystem is ephemeral, so
without it a restart keeps the database row and loses the MP4, leaving clips
marked `rendered` with nothing to play. Set the bucket and credentials, and the
download and stream endpoints hand back presigned URLs instead of local files.
That is why the Northflank deployment uses Postgres and S3 together and mounts
no volume at all: both the rows and the files outlive the container.

## Verifying a deployment

```bash
curl -fsS https://<host>/healthz    # liveness: {"status":"ok"}
curl -fsS https://<host>/readyz     # env and which providers are serving
curl -fsS -o /dev/null -w '%{http_code}\n' https://<host>/        # SPA shell
curl -fsS -o /dev/null -w '%{http_code}\n' https://<host>/docs    # OpenAPI UI
```

`/readyz` is the one to read after a deploy: it names the provider behind each
stage, so a run served by the deterministic fakes is visible there instead of
looking like a healthy deployment that quietly produces fake clips.

A deep link such as `/workbench` must return the SPA shell, while an unknown
`/api/v1/...` path must stay a JSON 404 rather than quietly returning HTML.

The schema is created at startup (`init_db`), including the additive column
backfill and the legacy-workspace adoption, so no migration step is needed.
