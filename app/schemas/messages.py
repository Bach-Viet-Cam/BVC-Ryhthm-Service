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


class EvaluationError(BaseModel):
    code: str
    message: str


class EvaluationRecordResponse(BaseModel):
    evaluationId: str
    status: Literal["queued", "processing", "completed", "failed"]
    result: dict[str, Any] | None = None
    error: EvaluationError | None = None
