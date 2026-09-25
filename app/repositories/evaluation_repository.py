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
        )
        session.add(record)
        session.commit()
        session.refresh(record)
        return record

    def mark_processing(self, session: Session, record: RhythmEvaluation) -> None:
        record.status = "processing"
        record.started_at = datetime.now(timezone.utc)
        record.error_code = None
        record.error_message = None
        session.commit()

    def mark_completed(self, session: Session, record: RhythmEvaluation, result: dict[str, Any]) -> None:
        record.status = "completed"
        record.result_json = result
        record.completed_at = datetime.now(timezone.utc)
        session.commit()

    def mark_failed(self, session: Session, record: RhythmEvaluation, code: str, message: str) -> None:
        record.status = "failed"
        record.error_code = code
        record.error_message = message
        record.completed_at = datetime.now(timezone.utc)
        session.commit()
