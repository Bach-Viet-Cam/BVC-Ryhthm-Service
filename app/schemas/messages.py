from datetime import datetime
from typing import Any, Literal

from pydantic import AnyHttpUrl, BaseModel, Field


class EvaluationTask(BaseModel):
    messageId: str = Field(min_length=1, max_length=128)
    evaluationId: str = Field(min_length=1, max_length=128)
    templateUrl: AnyHttpUrl
    studentUrl: AnyHttpUrl
    instrumentId: str = Field(min_length=1, max_length=64)
    requestedAt: datetime | None = None
    callbackUrl: AnyHttpUrl | None = None
    # Attempt number owned by the backend; a republished job keeps it so its reports are not stale
    attempt: int = Field(default=1, ge=1)


JobCode = Literal["quality_check", "audio_analysis", "comparison", "feedback"]
JOB_ORDER: tuple[JobCode, ...] = ("quality_check", "audio_analysis", "comparison", "feedback")
EvaluationStatus = Literal["queued", "processing", "waiting", "completed", "failed", "rejected"]


class JobReport(BaseModel):
    """Status of one job, reported by the pipeline itself or by an external job runner."""

    status: Literal["started", "done", "failed", "rejected"]
    attempt: int = Field(ge=1)
    code: str | None = Field(default=None, max_length=64)
    reason: str | None = Field(default=None, max_length=1000)


class RetryRequest(BaseModel):
    fromJob: JobCode
    attempt: int = Field(ge=2)


class ProgressCallback(BaseModel):
    evaluationId: str
    attempt: int
    job: JobCode
    status: Literal["started", "done", "failed", "rejected"]
    code: str | None = None
    reason: str | None = None
    result: dict[str, Any] | None = None


class EvaluationError(BaseModel):
    code: str
    message: str


class EvaluationRecordResponse(BaseModel):
    evaluationId: str
    status: EvaluationStatus
    stage: JobCode | None = None
    attempt: int = 1
    result: dict[str, Any] | None = None
    error: EvaluationError | None = None
