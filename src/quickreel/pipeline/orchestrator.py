"""Pipeline orchestration.

Stages (mirroring the MVP phases in the brief):

    ingest -> probe -> extract audio -> transcribe -> audio analysis
           -> visual analysis -> moment detection & scoring -> persist clips

Each stage is a method so it can be invoked, retried, or tested independently.
The orchestrator owns all state transitions on the ``Project`` row.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..ai.base import AudioEvent, TranscriptResult, VisualAnalysis
from ..ai.registry import (
    get_audio_analyzer,
    get_moment_provider,
    get_transcription_provider,
    get_vision_provider,
)
from ..analysis.boundaries import BoundaryOptimizer
from ..analysis.moments import MomentSelector
from ..analysis.scoring import ViralScorer
from ..config import get_settings
from ..logging_setup import get_logger
from ..media import ffmpeg
from ..media.downloader import DownloadError, get_downloader
from ..media.url_source import SourceUrlError, validate_source_url
from ..models import Clip, ClipStatus, MediaAsset, Project, ProjectStatus, Transcript

logger = get_logger(__name__)


class PipelineError(RuntimeError):
    pass


class Pipeline:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.settings = get_settings()

    # -- public entry point ------------------------------------------------

    def run(self, project_id: str) -> None:
        project = self.session.get(Project, project_id)
        if project is None:
            raise PipelineError(f"project {project_id} not found")

        stage = "ingesting"
        try:
            asset = self.ingest(project)
            stage = "transcribing"
            transcript_result = self.transcribe(project, asset)
            stage = "analyzing"
            audio_events = self.analyze_audio(asset)
            visual = self.analyze_visual(asset, transcript_result)
            self.detect_moments(project, asset, transcript_result, visual, audio_events)
            self._set_status(project, ProjectStatus.ready)
        except Exception as exc:  # noqa: BLE001 - recorded on the project row
            logger.error(
                "pipeline.failed", project_id=project_id, stage=stage, error=str(exc)
            )
            project.error = str(exc)
            self._set_status(project, self._failure_status(stage))
            raise

    @staticmethod
    def _failure_status(stage: str) -> ProjectStatus:
        return {
            "ingesting": ProjectStatus.ingesting_failed,
            "transcribing": ProjectStatus.transcribe_failed,
            "analyzing": ProjectStatus.analyze_failed,
        }.get(stage, ProjectStatus.analyze_failed)

    # -- stages ------------------------------------------------------------

    def ingest(self, project: Project) -> MediaAsset:
        self._set_status(project, ProjectStatus.ingesting)
        asset = self.session.scalar(
            select(MediaAsset).where(MediaAsset.project_id == project.id)
        )
        if asset is None:
            raise PipelineError("project has no media asset to ingest")

        # A URL project has no local file yet; fetch it now so that every later
        # stage only ever deals with a local path.
        if project.source_kind == "url" and (
            not asset.path or not Path(asset.path).exists()
        ):
            self._download_remote(project, asset)

        if not asset.path or not Path(asset.path).exists():
            project.error = f"media file missing at {asset.path}"
            self._set_status(project, ProjectStatus.ingesting_failed)
            raise PipelineError(project.error)

        info = ffmpeg.probe(asset.path)
        asset.duration_seconds = info.duration_seconds
        asset.width = info.width
        asset.height = info.height
        asset.fps = info.fps
        asset.has_audio = info.has_audio
        # Preserve any download metadata recorded for a URL source; assigning
        # the ffprobe payload directly would drop it.
        download_info = (asset.probe or {}).get("download")
        asset.probe = info.raw
        if download_info is not None:
            asset.probe = {**info.raw, "download": download_info}
        self.session.commit()
        logger.info(
            "ingest.probed",
            project_id=project.id,
            duration=round(info.duration_seconds, 2),
            size=f"{info.width}x{info.height}",
        )
        return asset

    def _download_remote(self, project: Project, asset: MediaAsset) -> None:
        """Fetch ``project.source_url`` into the uploads directory.

        The URL is re-validated here even though the API validates it on create:
        the stored value is what we act on, and validation is cheap next to a
        download. This is the last line of defence against SSRF.
        """
        if not self.settings.url_ingestion_enabled:
            raise PipelineError("URL ingestion is disabled on this server")
        if not project.source_url:
            raise PipelineError("project is marked as a URL source but has no URL")

        try:
            validated = validate_source_url(
                project.source_url,
                allowed_hosts=self.settings.allowed_source_host_list,
            )
        except SourceUrlError as exc:
            project.error = str(exc)
            raise PipelineError(f"rejected source URL: {exc}") from exc

        downloader = get_downloader()
        stem = project.id
        destination = self.settings.uploads_dir
        logger.info(
            "ingest.download_start",
            project_id=project.id,
            host=validated.host,
            downloader=downloader.name,
        )
        try:
            result = downloader.download(validated.url, destination, stem)
        except DownloadError as exc:
            project.error = str(exc)
            raise PipelineError(f"could not download source video: {exc}") from exc

        asset.path = str(result.path)
        asset.size_bytes = result.size_bytes
        if result.info.title and project.title in {"", "Untitled project"}:
            project.title = result.info.title[:500]
        if result.info.raw:
            asset.probe = {"download": result.info.raw}
        self.session.commit()
        logger.info(
            "ingest.download_done",
            project_id=project.id,
            bytes=result.size_bytes,
            extractor=result.info.extractor,
        )

    def transcribe(self, project: Project, asset: MediaAsset) -> TranscriptResult:
        self._set_status(project, ProjectStatus.transcribing)
        audio_path = self.settings.artifacts_dir / f"{project.id}.wav"
        if not asset.has_audio:
            raise PipelineError("source media has no audio track to transcribe")
        if not audio_path.exists():
            ffmpeg.extract_audio(asset.path, audio_path)

        provider = get_transcription_provider()
        result = provider.transcribe(audio_path)

        transcript = self.session.scalar(
            select(Transcript).where(Transcript.project_id == project.id)
        )
        if transcript is None:
            transcript = Transcript(project_id=project.id)
            self.session.add(transcript)
        transcript.language = result.language
        transcript.text = result.text
        transcript.provider = result.provider
        transcript.segments = [
            {"start": s.start, "end": s.end, "text": s.text, "speaker": s.speaker}
            for s in result.segments
        ]
        self.session.commit()
        logger.info(
            "transcribe.done",
            project_id=project.id,
            provider=result.provider,
            segments=len(result.segments),
        )
        return result

    def analyze_audio(self, asset: MediaAsset) -> list[AudioEvent]:
        audio_path = self.settings.artifacts_dir / f"{Path(asset.path).stem}.wav"
        project_audio = self.settings.artifacts_dir / f"{asset.project_id}.wav"
        target = project_audio if project_audio.exists() else audio_path
        if not target.exists():
            target = ffmpeg.extract_audio(asset.path, project_audio)
        analyzer = get_audio_analyzer()
        events = analyzer.analyze(target)
        logger.info("audio.analyzed", events=len(events), provider=analyzer.name)
        return events

    def analyze_visual(
        self, asset: MediaAsset, transcript: TranscriptResult
    ) -> VisualAnalysis:
        provider = get_vision_provider()
        return provider.analyze(Path(asset.path), transcript)

    def detect_moments(
        self,
        project: Project,
        asset: MediaAsset,
        transcript: TranscriptResult,
        visual: VisualAnalysis,
        audio_events: list[AudioEvent],
    ) -> list[Clip]:
        self._set_status(project, ProjectStatus.analyzing)
        selector = MomentSelector(
            provider=get_moment_provider(),
            scorer=ViralScorer(),
            optimizer=BoundaryOptimizer(),
        )
        moments = selector.select(
            transcript,
            visual,
            audio_events,
            media_duration=asset.duration_seconds or None,
        )

        # Replace any previous candidate set so re-runs are idempotent.
        for existing in self.session.scalars(
            select(Clip).where(Clip.project_id == project.id)
        ):
            self.session.delete(existing)
        self.session.flush()

        clips: list[Clip] = []
        for moment in moments:
            clip = Clip(
                project_id=project.id,
                start_seconds=moment.boundary.start,
                end_seconds=moment.boundary.end,
                raw_start_seconds=moment.boundary.raw_start,
                raw_end_seconds=moment.boundary.raw_end,
                title=moment.candidate.title,
                hook=moment.candidate.hook,
                transcript_excerpt=moment.candidate.excerpt,
                moment_type=moment.candidate.moment_type,
                rationale=moment.candidate.rationale,
                score=moment.score.score,
                score_components=moment.score.components,
                score_breakdown={
                    **moment.score.breakdown,
                    "boundary_adjustments": moment.boundary.adjustments,
                },
                rank=moment.rank,
                status=ClipStatus.candidate,
            )
            self.session.add(clip)
            clips.append(clip)
        self.session.commit()
        logger.info("moments.persisted", project_id=project.id, clips=len(clips))
        return clips

    # -- helpers -----------------------------------------------------------

    def _set_status(self, project: Project, status: ProjectStatus) -> None:
        project.status = status
        if status is ProjectStatus.ready:
            project.error = None
        self.session.commit()
