"""Health and readiness endpoints."""

from __future__ import annotations

from fastapi import APIRouter

from ...ai.registry import (
    get_moment_provider,
    get_transcription_provider,
    get_vision_provider,
)
from ...config import get_settings

router = APIRouter(tags=["system"])


@router.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@router.get("/readyz")
def readyz() -> dict:
    settings = get_settings()
    return {
        "status": "ok",
        "env": settings.env,
        "providers": {
            "transcription": get_transcription_provider().name,
            "vision": get_vision_provider().name,
            "moment": get_moment_provider().name,
        },
    }
