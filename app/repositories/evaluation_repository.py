from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.evaluation import RhythmEvaluation
from app.schemas.messages import EvaluationTask


class EvaluationRepository:
    def get(self, session: Session, evaluation_id: str) -> RhythmEvaluation | None:
        return session.scalar(select(RhythmEvaluation).where(RhythmEvaluation.evaluation_id == evaluation_id))

    def create_if_missing(self, session: Session, task: EvaluationTask) -> RhythmEvaluation:
        record = self.get(session, task.evaluationId)
        if record is not None:
            return record

        record = RhythmEvaluation(
            evaluation_id=task.evaluationId,
            status="queued",
            template_url=str(task.templateUrl),
            student_url=str(task.studentUrl),
            instrument_id=task.instrumentId,
            callback_url=str(task.callbackUrl) if task.callbackUrl else None,
            attempt=task.attempt,
        )
        session.add(record)
        session.commit()
        session.refresh(record)
        return record

    def to_task(self, record: RhythmEvaluation) -> EvaluationTask:
        return EvaluationTask(
            messageId=f"{record.evaluation_id}:{record.attempt}",
            evaluationId=record.evaluation_id,
            templateUrl=record.template_url,
            studentUrl=record.student_url,
            instrumentId=record.instrument_id,
            callbackUrl=record.callback_url,
            attempt=record.attempt,
        )

    def mark_processing(self, session: Session, record: RhythmEvaluation) -> None:
        record.status = "processing"
        record.stage = None
        record.started_at = datetime.now(timezone.utc)
        record.error_code = None
        record.error_message = None
        session.commit()

    def start_attempt(self, session: Session, record: RhythmEvaluation, attempt: int) -> None:
        record.attempt = attempt
        record.status = "processing"
        record.error_code = None
        record.error_message = None
        record.completed_at = None
        record.started_at = datetime.now(timezone.utc)
        session.commit()

    def mark_stage(self, session: Session, record: RhythmEvaluation, stage: str) -> None:
        record.status = "processing"
        record.stage = stage
        session.commit()

    def mark_waiting(self, session: Session, record: RhythmEvaluation) -> None:
        record.status = "waiting"
        session.commit()

    def save_job_state(self, session: Session, record: RhythmEvaluation, job_state: dict[str, Any]) -> None:
        record.job_state = job_state
        session.commit()

    def mark_completed(self, session: Session, record: RhythmEvaluation, result: dict[str, Any]) -> None:
        record.status = "completed"
        record.result_json = result
        record.completed_at = datetime.now(timezone.utc)
        session.commit()

    def mark_rejected(self, session: Session, record: RhythmEvaluation, code: str, message: str) -> None:
        self.mark_failed(session, record, code, message, status="rejected")

    def mark_failed(
        self, session: Session, record: RhythmEvaluation, code: str, message: str, status: str = "failed"
    ) -> None:
        record.status = status
        record.error_code = code
        record.error_message = message
        record.completed_at = datetime.now(timezone.utc)
        session.commit()
