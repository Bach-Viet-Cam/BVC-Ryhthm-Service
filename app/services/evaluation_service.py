"""Coordinate one evaluation as ordered jobs (GBR-63), reporting each job as it starts (GBR-64)."""

import asyncio
import shutil
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, TypeVar
from uuid import uuid4

from sqlalchemy.orm import Session

from app.audio.downloader import AudioDownloadError
from app.config import get_settings
from app.repositories.evaluation_repository import EvaluationRepository
from app.schemas.messages import JOB_ORDER, EvaluationTask, JobCode, JobReport
from app.services.audio_service import AudioService
from app.services.audio_quality_service import AudioProcessingError, AudioQualityError, AudioQualityService
from app.services.job_execution_service import JobExecutionService
from app.services.feedback_service import FeedbackService
from app.services.job_progress_service import JobProgressService
from app.services.rhythm_analysis_service import RhythmAnalysisService

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
        callback_service: CallbackService | None = None,
    ) -> None:
        self.repository = repository
        self.session_factory = session_factory
        self.temp_directory = temp_directory
        self.audio_service = audio_service or AudioService()
        self.rhythm_analysis_service = rhythm_analysis_service or RhythmAnalysisService()
        self.feedback_service = feedback_service or FeedbackService()
        self.quality_service = AudioQualityService()
        self.execution = JobExecutionService()

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

        try:
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

    async def run_guarded(self, task: EvaluationTask, from_job: JobCode, attempt: int) -> None:
        await self._persist_errors(task, self.run_from(task, from_job, attempt))

    async def process_direct_task(self, task: EvaluationTask) -> None:
        """Process an HTTP task and persist errors instead of raising them."""
        try:
            await self.process_task(task)
        except AudioQualityError as error:
            self.record_failure(task, error.code, str(error))
        except AudioProcessingError as error:
            self.record_failure(task, error.code, str(error))
        except AudioDownloadError as error:
            await self.record_failure(task, error.code, str(error))
        except (ValueError, PermanentEvaluationError) as error:
            code = error.code if isinstance(error, PermanentEvaluationError) else "EVALUATION_FAILED"
            await self.record_failure(task, code, str(error))
        except Exception:
            logger.exception("Unexpected direct evaluation error for %s", task.evaluationId)
            await self.record_failure(task, "INTERNAL_ERROR", "Lỗi nội bộ khi phân tích audio")
