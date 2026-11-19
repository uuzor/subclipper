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
# The `ai` extra pulls torch for faster-whisper, which is hundreds of MB and is
# only needed when transcription runs in this container. Providers are
# pluggable, so the default image stays lean and a deployment that wants local
# Whisper builds with --build-arg INSTALL_AI=1.
ARG INSTALL_AI=0
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && if [ "$INSTALL_AI" = "1" ]; then \
         /opt/venv/bin/pip install ".[ai]"; \
       else \
         /opt/venv/bin/pip install .; \
       fi

# ---- Runtime ----
FROM python:3.12-slim-bookworm AS runtime

# ffmpeg and ffprobe are hard requirements: the app refuses to boot without
# them, so they belong in the image rather than in a start command.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
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
