from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import AnyHttpUrl, BaseModel, Field, field_validator

from app.evaluation.instruments import normalize_instrument_id


class EvaluationTask(BaseModel):
    messageId: str = Field(min_length=1, max_length=128)
    evaluationId: str = Field(min_length=1, max_length=128)
    templateUrl: AnyHttpUrl
    studentUrl: AnyHttpUrl
    instrumentId: str = Field(min_length=1, max_length=64)
    templateAnalysisId: UUID | None = None
    callbackUrl: AnyHttpUrl | None = None
    attempt: int = Field(default=1, ge=1)
    requestedAt: datetime | None = None

    @field_validator("instrumentId")
    @classmethod
    def normalize_instrument(cls, value: str) -> str:
        return normalize_instrument_id(value)


class EvaluationError(BaseModel):
    code: str
    message: str


class EvaluationRecordResponse(BaseModel):
    evaluationId: str
    status: Literal["queued", "processing", "completed", "failed"]
    result: dict[str, Any] | None = None
    error: EvaluationError | None = None
