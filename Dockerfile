# QuickReel: one image, one process. The API and the built SPA are served from
# the same origin so there is no CORS or proxy config to get wrong.
#
# Three stages, because the frontend and backend toolchains share nothing: Node
# builds the SPA, Python builds the virtualenv, and the runtime image carries
# only the results plus ffmpeg.

# ---- Frontend ----
FROM node:22-bookworm-slim AS frontend
WORKDIR /app/frontend
# Copy manifests first so `npm ci` is cached until a dependency actually changes.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ---- Python dependencies ----
FROM python:3.12-slim-bookworm AS deps
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY pyproject.toml ./
COPY src/ ./src/
# The `ai` extra pulls faster-whisper and librosa, about 1.1GB, and is only
# needed when transcription and audio analysis run in this container. Providers
# are pluggable, so the default image stays lean and a deployment that wants
# local Whisper builds with --build-arg INSTALL_AI=1. Hosted vision and
# reasoning need only `hosted-ai`, which is small enough for a tiny instance.
ARG INSTALL_AI=0
ARG INSTALL_HOSTED_AI=1
# URL ingestion needs yt-dlp in every image: without it the downloader falls
# back to a stub that fails every link import with "yt-dlp is not installed on
# this server". It is a small pure-Python package, unlike the `ai` extra.
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && if [ "$INSTALL_AI" = "1" ]; then \
         /opt/venv/bin/pip install ".[ai,download]"; \
       elif [ "$INSTALL_HOSTED_AI" = "1" ]; then \
         /opt/venv/bin/pip install ".[hosted-ai,download]"; \
       else \
         /opt/venv/bin/pip install ".[download]"; \
       fi

# ---- Runtime ----
FROM python:3.12-slim-bookworm AS runtime

# ffmpeg and ffprobe are hard requirements: the app refuses to boot without
# them, so they belong in the image rather than in a start command.
# fonts-dejavu-core is not optional either: libass resolves the caption font
# through fontconfig, and with no font installed the subtitles filter warns
# "failed to find any fallback with glyph 0x0" and burns in nothing. The render
# still exits 0, so the clip looks fine and simply has no captions.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg ca-certificates fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    QUICKREEL_DATA_DIR=/var/data \
    QUICKREEL_FRONTEND_DIR=/app/frontend/dist \
    QUICKREEL_FFMPEG_BIN=ffmpeg \
    QUICKREEL_FFPROBE_BIN=ffprobe

WORKDIR /app
COPY --from=deps /opt/venv /opt/venv
COPY src/ ./src/
COPY --from=frontend /app/frontend/dist ./frontend/dist

# Render runs the container as an arbitrary UID, and the data directory holds
# uploads and renders. Making it world-writable is what lets a mounted disk or
# the container's own filesystem accept writes without a chown at boot.
RUN mkdir -p /var/data && chmod 777 /var/data

EXPOSE 8000

# One worker: rendering is CPU-bound and long, and the task queue (not the web
# process) is where concurrency belongs.
CMD ["sh", "-c", "uvicorn quickreel.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --proxy-headers --forwarded-allow-ips='*'"]
