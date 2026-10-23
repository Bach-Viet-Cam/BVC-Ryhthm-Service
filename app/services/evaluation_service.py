"""Coordinate one evaluation as ordered jobs (GBR-63), reporting each job as it starts (GBR-64)."""

import logging
import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar
from uuid import uuid4

from sqlalchemy.orm import Session

from app.audio.downloader import AudioDownloadError
from app.config import get_settings
from app.repositories.evaluation_repository import EvaluationRepository
from app.repositories.template_analysis_repository import TemplateAnalysisRepository
from app.schemas.messages import EvaluationTask
from app.services.audio_service import AudioService
from app.services.artifact_storage_service import ArtifactStorageService, ArtifactUnavailableError
from app.services.audio_quality_service import AudioProcessingError, AudioQualityError, AudioQualityService
from app.services.job_execution_service import JobExecutionService
from app.services.feedback_service import FeedbackService
from app.services.job_progress_service import JobProgressService
from app.services.rhythm_analysis_service import RhythmAnalysisService
from app.config import get_settings
from app.services.practice_progress_callback_client import PracticeProgressCallbackClient, ProgressJob

logger = logging.getLogger(__name__)
JobResult = TypeVar("JobResult")


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
        callback_client: PracticeProgressCallbackClient | None = None,
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
        self.callback_client = callback_client or PracticeProgressCallbackClient()
        self._active_stage: ProgressJob | None = None

    async def process_task(self, task: EvaluationTask) -> None:
        await self.execution.run(lambda: self._process_task(task))

    async def _process_task(self, task: EvaluationTask) -> None:
        session = self.session_factory()
        try:
            record = self.repository.create_if_missing(session, task)
            if record.status == "completed":
                return
            self.repository.mark_processing(session, record)
            attempt = record.attempt
        finally:
            session.close()

        stage: ProgressJob = "quality_check"
        self._active_stage = stage
        try:
            await self._notify(task, stage, "started")
            template = self._read_template_cache(task)
            student_path = await self.audio_service.download(str(task.studentUrl), work_directory)
            quality = await self.quality_service.inspect(student_path)
            quality_session = self.session_factory()
            try:
                quality_record = self.repository.get(quality_session, task.evaluationId)
                self.repository.save_quality(quality_session, quality_record, quality)
            finally:
                quality_session.close()
            self.quality_service.ensure_acceptable(quality)
            await self._notify(task, stage, "done")

            stage = "audio_analysis"
            self._active_stage = stage
            await self._notify(task, stage, "started")
            student_analysis = await self.rhythm_analysis_service.analyze(student_path, task.instrumentId)
            await self._notify(task, stage, "done")

            stage = "comparison"
            self._active_stage = stage
            await self._notify(task, stage, "started")
            if template is not None:
                try:
                    teacher_analysis = await self.rhythm_analysis_service.analyze_template(template, task.instrumentId)
                except (KeyError, TypeError, ValueError):
                    # A stale/corrupt artifact must never block a student's submission.
                    teacher_path = await self.audio_service.download(str(task.templateUrl), work_directory)
                    teacher_analysis = await self.rhythm_analysis_service.analyze(teacher_path, task.instrumentId)
            else:
                teacher_path = await self.audio_service.download(str(task.templateUrl), work_directory)
                teacher_analysis = await self.rhythm_analysis_service.analyze(teacher_path, task.instrumentId)
            result = await self.rhythm_analysis_service.compare(teacher_analysis, student_analysis)
            await self._notify(task, stage, "done")

            stage = "feedback"
            self._active_stage = stage
            await self._notify(task, stage, "started")
            result["aiFeedback"] = await self.feedback_service.generate(result)
            result["studentAudioQuality"] = quality
            session = self.session_factory()
            try:
                record = self.repository.get(session, task.evaluationId)
                self.repository.mark_completed(session, record, result)
            finally:
                session.close()
            await self._notify(task, stage, "done", result=result)
        except AudioQualityError as error:
            await self._notify(task, "quality_check", "rejected", code=error.code, reason=str(error))
            raise
        except Exception:
            raise
        finally:
            shutil.rmtree(work_directory, ignore_errors=True)

    async def report_final_failure(self, task: EvaluationTask, code: str, reason: str) -> None:
        await self._notify(task, self._active_stage or "quality_check", "failed", code=code, reason=reason)

    async def _notify(self, task: EvaluationTask, job: ProgressJob, status: str,
                      *, code: str | None = None, reason: str | None = None,
                      result: dict | None = None) -> None:
        try:
            await self.callback_client.send(task, job, status, code=code, reason=reason, result=result)
        except Exception:
            logger.exception("Unexpected error while delivering practice callback for %s", task.evaluationId)

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
            await self.report_final_failure(task, error.code, str(error))
            self.record_failure(task, error.code, str(error))
        except AudioDownloadError as error:
            await self.report_final_failure(task, error.code, str(error))
            self.record_failure(task, error.code, str(error))
        except (ValueError, PermanentEvaluationError) as error:
            code = error.code if isinstance(error, PermanentEvaluationError) else "EVALUATION_FAILED"
            await self.report_final_failure(task, code, str(error))
            self.record_failure(task, code, str(error))
        except Exception:
            logger.exception("Unexpected direct evaluation error for %s", task.evaluationId)
            await self.report_final_failure(task, "INTERNAL_ERROR", "Internal evaluation error")
            self.record_failure(task, "INTERNAL_ERROR", "Lỗi nội bộ khi phân tích audio")

    def record_failure(self, task: EvaluationTask, code: str, error_message: str) -> None:
        session = self.session_factory()
        try:
            record = self.repository.create_if_missing(session, task)
            self.repository.mark_failed(session, record, code, error_message)
        finally:
            session.close()
