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
from app.services.callback_service import CallbackService
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
        self.callback_service = callback_service or CallbackService()

    @property
    def progress(self) -> JobProgressService:
        # Built on access so a replaced session_factory or callback_service is always used
        return JobProgressService(self.repository, self.session_factory, self.callback_service)

    async def process_task(self, task: EvaluationTask) -> None:
        session = self.session_factory()
        try:
            record = self.repository.create_if_missing(session, task)
            if record.status == "completed":
                return
            self.repository.mark_processing(session, record)
            attempt = record.attempt
        finally:
            session.close()
        await self.run_from(task, JOB_ORDER[0], attempt)

    async def run_from(self, task: EvaluationTask, from_job: JobCode, attempt: int) -> None:
        """Run the jobs from `from_job` on; errors propagate so the caller decides about retries."""
        work_directory = Path(self.temp_directory) / uuid4().hex
        scores = self.progress.load_scores(task.evaluationId) if from_job == "feedback" else None
        try:
            for job in JOB_ORDER[JOB_ORDER.index(from_job):]:
                if not await self._report(task, job, JobReport(status="started", attempt=attempt)):
                    return  # a newer attempt took over
                if job == "quality_check" and get_settings().quality_check_mode == "external":
                    self.progress.mark_waiting(task.evaluationId)
                    return  # resumed by POST /evaluations/{id}/jobs/quality_check
                if job == "audio_analysis" or (job != "quality_check" and scores is None):
                    scores = await self._run_job(self._analyze(task, work_directory))
                result = await self._finish_job(job, scores)
                await self._report(task, job, JobReport(status="done", attempt=attempt), result)
        finally:
            shutil.rmtree(work_directory, ignore_errors=True)

    async def run_guarded(self, task: EvaluationTask, from_job: JobCode, attempt: int) -> None:
        await self._persist_errors(task, self.run_from(task, from_job, attempt))

    async def process_direct_task(self, task: EvaluationTask) -> None:
        """Process an HTTP task and persist errors instead of raising them."""
        await self._persist_errors(task, self.process_task(task))

    async def record_failure(self, task: EvaluationTask, code: str, error_message: str) -> None:
        session = self.session_factory()
        try:
            record = self.repository.create_if_missing(session, task)
            job, attempt = record.stage or JOB_ORDER[0], record.attempt
        finally:
            session.close()
        await self.progress.report(
            task.evaluationId, job, JobReport(status="failed", attempt=attempt, code=code, reason=error_message)
        )

    async def _finish_job(self, job: JobCode, scores: dict[str, Any] | None) -> dict[str, Any] | None:
        # JOB-01 is bypassed and JOB-02/03 share evaluate_pair until the algorithm is split
        if job == "comparison":
            return scores
        if job == "feedback":
            feedback = await self._run_job(self.feedback_service.generate(scores))
            return {**scores, "aiFeedback": feedback}
        return None

    async def _analyze(self, task: EvaluationTask, work_directory: Path) -> dict[str, Any]:
        teacher_path = await self.audio_service.download(str(task.templateUrl), work_directory)
        student_path = await self.audio_service.download(str(task.studentUrl), work_directory)
        return await self.rhythm_analysis_service.evaluate(teacher_path, student_path, task.instrumentId)

    async def _run_job(self, work: Awaitable[JobResult]) -> JobResult:
        try:
            return await asyncio.wait_for(work, timeout=get_settings().job_timeout_seconds)
        except TimeoutError as error:
            # GBR-65: a job running longer than the limit counts as failed
            raise PermanentEvaluationError("JOB_TIMEOUT", "Bước xử lý chạy quá thời gian cho phép") from error

    async def _report(
        self, task: EvaluationTask, job: JobCode, report: JobReport, result: dict[str, Any] | None = None
    ) -> bool:
        return await self.progress.report(task.evaluationId, job, report, result) is not None

    async def _persist_errors(self, task: EvaluationTask, work: Awaitable[None]) -> None:
        try:
            await work
        except AudioDownloadError as error:
            await self.record_failure(task, error.code, str(error))
        except (ValueError, PermanentEvaluationError) as error:
            code = error.code if isinstance(error, PermanentEvaluationError) else "EVALUATION_FAILED"
            await self.record_failure(task, code, str(error))
        except Exception:
            logger.exception("Unexpected direct evaluation error for %s", task.evaluationId)
            await self.record_failure(task, "INTERNAL_ERROR", "Lỗi nội bộ khi phân tích audio")
