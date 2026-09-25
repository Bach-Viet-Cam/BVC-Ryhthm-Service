from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, status
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_session, initialize_database
from app.messaging.rabbitmq_worker import RabbitMqWorker
from app.models.evaluation import RhythmEvaluation
from app.repositories.evaluation_repository import EvaluationRepository
from app.schemas.messages import EvaluationError, EvaluationRecordResponse, EvaluationTask


def create_app() -> FastAPI:
    app = FastAPI(title="Rhythm Service", version="1.0.0")

    @app.on_event("startup")
    def startup() -> None:
        initialize_database()

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post(
        "/api/v1/evaluations",
        response_model=EvaluationRecordResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def create_evaluation(
        task: EvaluationTask,
        background_tasks: BackgroundTasks,
        authorization: str | None = Header(default=None),
        session: Session = Depends(get_session),
    ) -> EvaluationRecordResponse:
        _require_internal_auth(authorization)
        repository = EvaluationRepository()
        record = repository.create_if_missing(session, task)

        if record.status not in {"completed", "processing"}:
            background_tasks.add_task(RabbitMqWorker().process_direct_task, task)

        return _to_response(record)

    @app.get("/api/v1/evaluations/{evaluation_id}", response_model=EvaluationRecordResponse)
    def get_evaluation(
        evaluation_id: str,
        authorization: str | None = Header(default=None),
        session: Session = Depends(get_session),
    ) -> EvaluationRecordResponse:
        _require_internal_auth(authorization)

        record = EvaluationRepository().get(session, evaluation_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Evaluation not found")

        return _to_response(record)

    return app


def _require_internal_auth(authorization: str | None) -> None:
    settings = get_settings()
    expected = f"Bearer {settings.internal_api_key}"
    if not settings.internal_api_key or authorization != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")


def _to_response(record: RhythmEvaluation) -> EvaluationRecordResponse:
    error = None
    if record.error_code:
        error = EvaluationError(code=record.error_code, message=record.error_message or "Evaluation failed")
    return EvaluationRecordResponse(
        evaluationId=record.evaluation_id,
        status=record.status,
        result=record.result_json,
        error=error,
    )
