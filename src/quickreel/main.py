"""FastAPI application factory."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

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

    from .api.routes import auth as auth_routes
    from .api.routes import clips, health, oauth, projects, publishing, triggers

    app.include_router(health.router)
    app.include_router(auth_routes.router)
    app.include_router(projects.router)
    app.include_router(clips.router)
    app.include_router(publishing.router)
    app.include_router(oauth.router)
    app.include_router(triggers.router)

    _mount_frontend(app)
    return app


class _SpaStaticFiles(StaticFiles):
    """Static files that fall back to index.html for client-side routes.

    The React app owns routing (/projects/<id>, /workbench). A deep link or a
    refresh on one of those paths must return the shell, not a 404. API and
    docs paths are never handed to this class, so a genuine API 404 stays JSON.
    """

    # Paths under these prefixes are never SPA routes. A request for an unknown
    # one (e.g. /api/v1/nope, where no router matched at all) must keep the
    # API's JSON 404 rather than silently returning the HTML shell, which would
    # make a broken client request look like a successful page load.
    _API_PREFIXES = ("api/", "healthz", "docs", "openapi.json", "redoc")

    async def get_response(self, path: str, scope):
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != 404:
                raise
            if path.startswith(self._API_PREFIXES):
                raise
            return await super().get_response("index.html", scope)


def _mount_frontend(app: FastAPI) -> None:
    settings = get_settings()
    directory: Path | None = settings.frontend_dir
    if directory is None:
        return

    index = directory / "index.html"
    if not index.is_file():
        # Serving was requested but nothing was built. Warn rather than crash:
        # the API is still useful, and the message names the fix.
        logger.warning(
            "frontend.missing",
            detail=f"{directory} has no index.html; run `npm run build` in frontend/",
        )
        return

    assets = directory / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    app.mount("/", _SpaStaticFiles(directory=directory, html=True), name="frontend")
    logger.info("frontend.serving", directory=str(directory))


app = create_app()
