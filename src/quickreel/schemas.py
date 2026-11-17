"""Pydantic request/response schemas for the HTTP API."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class MediaAssetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    path: str
    size_bytes: int
    duration_seconds: float
    width: int
    height: int
    fps: float
    has_audio: bool


class TranscriptSegmentOut(BaseModel):
    start: float
    end: float
    text: str
    speaker: str | None = None


class TranscriptOut(BaseModel):
    language: str
    provider: str
    text: str
    segments: list[TranscriptSegmentOut] = Field(default_factory=list)


class ClipOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    project_id: str
    start_seconds: float
    end_seconds: float
    raw_start_seconds: float
    raw_end_seconds: float
    duration_seconds: float = 0.0
    title: str
    hook: str
    transcript_excerpt: str
    moment_type: str
    rationale: str
    score: float
    score_components: dict
    score_breakdown: dict
    rank: int
    status: str
    render_path: str | None = None
    render_meta: dict = Field(default_factory=dict)


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    source_kind: str
    source_url: str | None = None
    status: str
    error: str | None = None
    created_at: datetime
    updated_at: datetime


class ProjectDetailOut(ProjectOut):
    media: MediaAssetOut | None = None
    transcript: TranscriptOut | None = None
    clips: list[ClipOut] = Field(default_factory=list)


class ProjectCreateIn(BaseModel):
    title: str = Field(default="Untitled project", max_length=500)
    source_url: HttpUrl | None = None


class RenderRequest(BaseModel):
    clip_ids: list[str] | None = None
    max_clips: int = Field(default=3, ge=1, le=20)
    vertical: bool = True
    burn_captions: bool = True
