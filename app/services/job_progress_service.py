"""Apply one job report to an evaluation and forward it to the backend.

Both the in-process pipeline and external job runners (the future quality check)
report through here, so the stored state and the callbacks stay identical.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.models.evaluation import RhythmEvaluation
from app.repositories.evaluation_repository import EvaluationRepository
from app.schemas.messages import JOB_ORDER, EvaluationTask, JobCode, JobReport, ProgressCallback
from app.services.callback_service import CallbackService

FINAL_STATUSES = {"completed", "failed", "rejected"}


@dataclass(frozen=True)
class ReportOutcome:
    # Set when an external job finished while the pipeline was waiting for it
    next_job: JobCode | None = None
    next_task: EvaluationTask | None = None


class JobProgressService:
    def __init__(
        self,
        repository: EvaluationRepository,
        session_factory: Callable[[], Session],
        callback_service: CallbackService,
    ) -> None:
        self.repository = repository
        self.session_factory = session_factory
        self.callback_service = callback_service

    async def report(
        self, evaluation_id: str, job: JobCode, report: JobReport, result: dict[str, Any] | None = None
    ) -> ReportOutcome | None:
        """Return None when the report is stale (other attempt) or the evaluation already ended."""
        session = self.session_factory()
        try:
            record = self.repository.get(session, evaluation_id)
            if record is None or record.attempt != report.attempt or record.status in FINAL_STATUSES:
                return None
            was_waiting = record.status == "waiting"
            self._apply(session, record, job, report, result)
            callback_url = record.callback_url
            outcome = ReportOutcome()
            if report.status == "done" and was_waiting and job != JOB_ORDER[-1]:
                outcome = ReportOutcome(JOB_ORDER[JOB_ORDER.index(job) + 1], self.repository.to_task(record))
        finally:
            session.close()

        is_completed = report.status == "done" and job == JOB_ORDER[-1]
        await self.callback_service.notify(callback_url, ProgressCallback(
            evaluationId=evaluation_id, attempt=report.attempt, job=job, status=report.status,
            code=report.code, reason=report.reason, result=result if is_completed else None,
        ))
        return outcome

    def mark_waiting(self, evaluation_id: str) -> None:
        session = self.session_factory()
        try:
            record = self.repository.get(session, evaluation_id)
            if record is not None:
                self.repository.mark_waiting(session, record)
        finally:
            session.close()

    def load_scores(self, evaluation_id: str) -> dict[str, Any] | None:
        session = self.session_factory()
        try:
            record = self.repository.get(session, evaluation_id)
            return (record.job_state or {}).get("scores") if record else None
        finally:
            session.close()

    def _apply(
        self, session: Session, record: RhythmEvaluation, job: JobCode, report: JobReport, result: dict[str, Any] | None
    ) -> None:
        if report.status == "started":
            self.repository.mark_stage(session, record, job)
        elif report.status == "done" and job == JOB_ORDER[-1]:
            self.repository.mark_completed(session, record, result or {})
        elif report.status == "done":
            if result is not None:
                self.repository.save_job_state(session, record, {"scores": result})
            self.repository.mark_stage(session, record, job)
        elif report.status == "rejected":
            self.repository.mark_rejected(
                session, record, report.code or "QUALITY_REJECTED", report.reason or "Bản thu không đạt chất lượng"
            )
        else:
            self.repository.mark_failed(
                session, record, report.code or "EVALUATION_FAILED", report.reason or "Lỗi khi phân tích audio"
            )
