"""Coordinate one evaluation while preserving the existing task lifecycle."""

import logging
import json
import shutil
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

from sqlalchemy.orm import Session

from app.audio.downloader import AudioDownloadError
from app.repositories.evaluation_repository import EvaluationRepository
from app.repositories.template_analysis_repository import TemplateAnalysisRepository
from app.schemas.messages import EvaluationTask
from app.services.audio_service import AudioService
from app.services.artifact_storage_service import ArtifactStorageService, ArtifactUnavailableError
from app.services.audio_quality_service import AudioProcessingError, AudioQualityError, AudioQualityService
from app.services.job_execution_service import JobExecutionService
from app.services.feedback_service import FeedbackService
from app.services.rhythm_analysis_service import RhythmAnalysisService
from app.config import get_settings

logger = logging.getLogger(__name__)


class PermanentEvaluationError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvaluationService:
    def __init__(
        self,
        repository: EvaluationRepository,
        session_factory: Callable[[], Session],
        temp_directory: str,
        audio_service: AudioService | None = None,
        rhythm_analysis_service: RhythmAnalysisService | None = None,
        feedback_service: FeedbackService | None = None,
    ) -> None:
        self.repository = repository
        self.session_factory = session_factory
        self.temp_directory = temp_directory
        self.audio_service = audio_service or AudioService()
        self.rhythm_analysis_service = rhythm_analysis_service or RhythmAnalysisService()
        self.feedback_service = feedback_service or FeedbackService()
        self.quality_service = AudioQualityService()
        self.execution = JobExecutionService()
        self.template_repository = TemplateAnalysisRepository()
        self.template_storage = ArtifactStorageService(get_settings().artifact_root)

    async def process_task(self, task: EvaluationTask) -> None:
        await self.execution.run(lambda: self._process_task(task))

    async def _process_task(self, task: EvaluationTask) -> None:
        session = self.session_factory()
        work_directory = Path(self.temp_directory) / uuid4().hex
        try:
            record = self.repository.create_if_missing(session, task)
            if record.status == "completed":
                return
            self.repository.mark_processing(session, record)
        finally:
            session.close()

        try:
            template = self._read_template_cache(task)
            if template is None:
                teacher_path = await self.audio_service.download(str(task.templateUrl), work_directory)
            student_path = await self.audio_service.download(str(task.studentUrl), work_directory)
            quality = await self.quality_service.inspect(student_path)
            quality_session = self.session_factory()
            try:
                quality_record = self.repository.get(quality_session, task.evaluationId)
                self.repository.save_quality(quality_session, quality_record, quality)
            finally:
                quality_session.close()
            self.quality_service.ensure_acceptable(quality)
            if template is not None:
                try:
                    result = await self.rhythm_analysis_service.evaluate_against_template(template, student_path, task.instrumentId)
                except (KeyError, TypeError, ValueError):
                    # A stale/corrupt artifact must never block a student's submission.
                    teacher_path = await self.audio_service.download(str(task.templateUrl), work_directory)
                    result = await self.rhythm_analysis_service.evaluate(teacher_path, student_path, task.instrumentId)
            else:
                result = await self.rhythm_analysis_service.evaluate(teacher_path, student_path, task.instrumentId)
            result["aiFeedback"] = await self.feedback_service.generate(result)
            result["studentAudioQuality"] = quality
            session = self.session_factory()
            try:
                record = self.repository.get(session, task.evaluationId)
                self.repository.mark_completed(session, record, result)
            finally:
                session.close()
        finally:
            shutil.rmtree(work_directory, ignore_errors=True)

    def _read_template_cache(self, task: EvaluationTask) -> dict | None:
        if task.templateAnalysisId is None:
            return None
        try:
            with self.session_factory() as session:
                record = self.template_repository.get(session, str(task.templateAnalysisId))
                if record is None or record.status != "completed" or record.instrument_code != task.instrumentId:
                    return None
                if not record.artifact_key or not record.artifact_sha256:
                    return None
                raw = self.template_storage.read(record.artifact_key, record.artifact_sha256)
            artifact = json.loads(raw)
            if artifact.get("analysisId") != str(task.templateAnalysisId):
                return None
            return artifact
        except (ArtifactUnavailableError, OSError, UnicodeError, ValueError, TypeError):
            return None

    async def process_direct_task(self, task: EvaluationTask) -> None:
        """Process an HTTP task and persist errors instead of raising them."""
        try:
            await self.process_task(task)
        except AudioQualityError as error:
            self.record_failure(task, error.code, str(error))
        except AudioProcessingError as error:
            self.record_failure(task, error.code, str(error))
        except AudioDownloadError as error:
            self.record_failure(task, error.code, str(error))
        except (ValueError, PermanentEvaluationError) as error:
            code = error.code if isinstance(error, PermanentEvaluationError) else "EVALUATION_FAILED"
            self.record_failure(task, code, str(error))
        except Exception:
            logger.exception("Unexpected direct evaluation error for %s", task.evaluationId)
            self.record_failure(task, "INTERNAL_ERROR", "Lỗi nội bộ khi phân tích audio")

    def record_failure(self, task: EvaluationTask, code: str, error_message: str) -> None:
        session = self.session_factory()
        try:
            record = self.repository.create_if_missing(session, task)
            self.repository.mark_failed(session, record, code, error_message)
        finally:
            session.close()
