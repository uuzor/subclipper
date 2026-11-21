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
from ..analysis.calibration import latest_applied_weights
from ..analysis.moments import MomentSelector
from ..analysis.scoring import ViralScorer
from ..config import get_settings
from ..logging_setup import get_logger
from ..media import ffmpeg
from ..media.downloader import DownloadError, get_downloader
from ..media.url_source import SourceUrlError, validate_source_url
from ..models import Clip, ClipStatus, MediaAsset, Project, ProjectStatus, Transcript
from ..storage import get_storage

logger = get_logger(__name__)


class PipelineError(RuntimeError):
    pass


class Pipeline:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.settings = get_settings()
        # Set by detect_moments so run() can record provenance without building
        # the moment provider a second time.
        self._moment_provider_name = "unknown"

    def _active_weights(self, project: Project) -> dict[str, float]:
        """The scoring weights this project's workspace has calibrated to.

        Falls back to the configured defaults when the workspace has never
        applied a fit, so a fresh tenant scores exactly as it always did.
        """
        if not self.settings.scoring_calibration_enabled:
            return dict(self.settings.scoring_weights)
        calibrated = latest_applied_weights(self.session, project.workspace_id)
        return calibrated or dict(self.settings.scoring_weights)

    # -- public entry point ------------------------------------------------

    def run(self, project_id: str) -> None:
        project = self.session.get(Project, project_id)
        if project is None:
            raise PipelineError(f"project {project_id} not found")

        stage = "ingesting"
        provenance: dict[str, str] = {}
        try:
            asset = self.ingest(project)
            stage = "transcribing"
            audio_path = self.extract_audio(asset)
            transcript_result = self.transcribe(project, asset, audio_path)
            provenance["transcription"] = transcript_result.provider
            stage = "analyzing"
            audio_events, audio_provider = self.analyze_audio(audio_path)
            provenance["audio"] = audio_provider
            visual = self.analyze_visual(asset, transcript_result)
            provenance["vision"] = visual.provider
            self.detect_moments(project, asset, transcript_result, visual, audio_events)
            provenance["moment"] = self._moment_provider_name
            # Record what actually served each stage so a degraded run is
            # auditable even though it succeeded.
            project.providers = provenance
            # Moments are only rows until ffmpeg turns them into MP4s, and a
            # candidate row renders as an empty player. Claim the top moments
            # before the project turns ready: the client stops polling once a
            # project is ready with nothing rendering, so a clip left as a
            # candidate here would never be picked up by the open page.
            pending = (
                self._claim_for_auto_render(project)
                if self.settings.auto_render_after_process
                else []
            )
            self._set_status(project, ProjectStatus.ready)
            for clip in pending:
                self._render_one(clip)
        except Exception as exc:  # noqa: BLE001 - recorded on the project row
            logger.error("pipeline.failed", project_id=project_id, stage=stage, error=str(exc))
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
        asset = self.session.scalar(select(MediaAsset).where(MediaAsset.project_id == project.id))
        if asset is None:
            raise PipelineError("project has no media asset to ingest")

        # A URL project has no local file yet; fetch it now so that every later
        # stage only ever deals with a local path.
        if project.source_kind == "url" and (not asset.path or not Path(asset.path).exists()):
            self._download_remote(project, asset)

        if asset.source_key and (not asset.path or not Path(asset.path).exists()):
            self._restore_source(project, asset)

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

    def _restore_source(self, project: Project, asset: MediaAsset) -> None:
        """Fetch the source video back from object storage into the uploads dir.

        A container restart wipes the uploads directory, and the media row then
        points at a file that no longer exists. Without this the only way forward
        is asking the user to upload the same video again.
        """
        if not asset.path:
            asset.path = str(self.settings.uploads_dir / f"{project.id}.mp4")
        try:
            get_storage().get(asset.source_key, asset.path)
        except Exception as exc:  # noqa: BLE001 - reported as a missing source
            project.error = f"could not restore source video from storage: {exc}"
            raise PipelineError(project.error) from exc
        self.session.commit()
        logger.info("ingest.source_restored", project_id=project.id, key=asset.source_key)

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
        # Keep a durable copy for the same reason uploads get one: the download
        # is expensive to repeat and the local file does not survive a restart.
        storage = get_storage()
        if storage.is_remote:
            key = f"sources/{project.id}{Path(result.path).suffix}"
            try:
                storage.put(result.path, key)
                asset.source_key = key
            except Exception as exc:  # noqa: BLE001 - the local copy still works
                logger.error("ingest.backup_failed", project_id=project.id, error=str(exc))
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

    def extract_audio(self, asset: MediaAsset) -> Path:
        """Extract the mono WAV once and hand it to every consumer.

        Previously transcription and audio analysis each derived the artifact
        path independently and could disagree about where it lived.
        """
        if not asset.has_audio:
            raise PipelineError("source media has no audio track to transcribe")
        audio_path = self.settings.artifacts_dir / f"{asset.project_id}.wav"
        if not audio_path.exists():
            ffmpeg.extract_audio(asset.path, audio_path)
        return audio_path

    def transcribe(self, project: Project, asset: MediaAsset, audio_path: Path) -> TranscriptResult:
        self._set_status(project, ProjectStatus.transcribing)
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

    def analyze_audio(self, audio_path: Path) -> tuple[list[AudioEvent], str]:
        analyzer = get_audio_analyzer()
        events = analyzer.analyze(audio_path)
        logger.info("audio.analyzed", events=len(events), provider=analyzer.name)
        return events, analyzer.name

    def analyze_visual(self, asset: MediaAsset, transcript: TranscriptResult) -> VisualAnalysis:
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
        moment_provider = get_moment_provider()
        selector = MomentSelector(
            provider=moment_provider,
            scorer=ViralScorer(self._active_weights(project)),
            optimizer=BoundaryOptimizer(),
        )
        moments = selector.select(
            transcript,
            visual,
            audio_events,
            media_duration=asset.duration_seconds or None,
        )

        # Replace any previous candidate set so re-runs are idempotent. Delete the
        # rendered files too, otherwise every re-run orphans the previous MP4s.
        for existing in self.session.scalars(select(Clip).where(Clip.project_id == project.id)):
            if existing.render_path:
                Path(existing.render_path).unlink(missing_ok=True)
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
        self._moment_provider_name = moment_provider.name
        return clips

    # -- helpers -----------------------------------------------------------

    def _claim_for_auto_render(self, project: Project) -> list[Clip]:
        """Mark the top moments as rendering so the client keeps polling."""
        clips = list(
            self.session.scalars(
                select(Clip)
                .where(Clip.project_id == project.id, Clip.status == ClipStatus.candidate)
                .order_by(Clip.rank)
                .limit(self.settings.auto_render_max_clips)
            )
        )
        for clip in clips:
            clip.status = ClipStatus.rendering
        self.session.commit()
        return clips

    def _render_one(self, clip: Clip) -> None:
        """Render a claimed clip. A failure leaves it retryable, not fatal.

        The project is already ``ready`` by now, so a render problem must not
        demote the analysis: the clip records its own failure and the client can
        still ask for a render.
        """
        from ..rendering.renderer import render_clip

        try:
            render_clip(
                self.session,
                clip,
                platform=self.settings.auto_render_platform or None,
            )
        except Exception as exc:  # noqa: BLE001 - recorded per clip
            logger.error("pipeline.auto_render_failed", clip_id=clip.id, error=str(exc))

    def _set_status(self, project: Project, status: ProjectStatus) -> None:
        project.status = status
        if status is ProjectStatus.ready:
            project.error = None
        self.session.commit()
