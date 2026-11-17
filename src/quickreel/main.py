"""FastAPI application factory."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import get_settings
from .db import init_db
from .logging_setup import configure_logging, get_logger
from .media import ffmpeg

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging()
    settings.ensure_directories()
    init_db()
    try:
        ffmpeg.require_binaries()
    except ffmpeg.FFmpegError as exc:
        # Fail loudly at boot: every downstream stage needs ffmpeg.
        raise RuntimeError(str(exc)) from exc
    logger.info("app.started", env=settings.env)
    yield
    logger.info("app.stopped")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="QuickReel",
        version="0.1.0",
        description="AI viral clip generator: ingest long-form video, detect engaging moments, "
        "render platform-ready vertical clips.",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    from .api.routes import clips, health, projects

    app.include_router(health.router)
    app.include_router(projects.router)
    app.include_router(clips.router)
    return app


app = create_app()
