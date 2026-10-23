from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, status
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import SessionFactory, get_session, initialize_database
from app.models.evaluation import RhythmEvaluation
from app.repositories.evaluation_repository import EvaluationRepository
from app.schemas.messages import (
    EvaluationError,
    EvaluationRecordResponse,
    EvaluationTask,
    JobCode,
    JobReport,
    RetryRequest,
)
from app.services.evaluation_service import EvaluationService


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

        if record.status not in {"completed", "processing", "waiting"}:
            evaluation_service = EvaluationService(repository, SessionFactory, get_settings().temp_directory)
            background_tasks.add_task(evaluation_service.process_direct_task, task)

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

    @app.post("/api/v1/evaluations/{evaluation_id}/jobs/{job}", response_model=EvaluationRecordResponse)
    async def report_job(
        evaluation_id: str,
        job: JobCode,
        report: JobReport,
        background_tasks: BackgroundTasks,
        authorization: str | None = Header(default=None),
        session: Session = Depends(get_session),
    ) -> EvaluationRecordResponse:
        """Progress report of one job; external job runners (JOB-01) call this when they finish."""
        _require_internal_auth(authorization)
        if report.status == "rejected" and job != "quality_check":
            raise HTTPException(status_code=422, detail="Only the quality check can reject a recording")
        repository = EvaluationRepository()
        if repository.get(session, evaluation_id) is None:
            raise HTTPException(status_code=404, detail="Evaluation not found")

        evaluation_service = EvaluationService(repository, SessionFactory, get_settings().temp_directory)
        outcome = await evaluation_service.progress.report(evaluation_id, job, report)
        if outcome is None:
            raise HTTPException(status_code=409, detail="Report does not match the current attempt")
        if outcome.next_job and outcome.next_task:
            background_tasks.add_task(evaluation_service.run_guarded, outcome.next_task, outcome.next_job, report.attempt)

        session.expire_all()
        return _to_response(repository.get(session, evaluation_id))

    @app.post(
        "/api/v1/evaluations/{evaluation_id}/retry",
        response_model=EvaluationRecordResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def retry_evaluation(
        evaluation_id: str,
        request: RetryRequest,
        background_tasks: BackgroundTasks,
        authorization: str | None = Header(default=None),
        session: Session = Depends(get_session),
    ) -> EvaluationRecordResponse:
        """Start a new attempt from the job that failed (GBR-63)."""
        _require_internal_auth(authorization)
        repository = EvaluationRepository()
        record = repository.get(session, evaluation_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Evaluation not found")
        if record.status in {"completed", "rejected"} or request.attempt <= record.attempt:
            raise HTTPException(status_code=409, detail="Evaluation cannot be retried")

        repository.start_attempt(session, record, request.attempt)
        evaluation_service = EvaluationService(repository, SessionFactory, get_settings().temp_directory)
        background_tasks.add_task(
            evaluation_service.run_guarded, repository.to_task(record), request.fromJob, request.attempt
        )
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
        stage=record.stage,
        attempt=record.attempt,
        result=record.result_json,
        error=error,
    )
