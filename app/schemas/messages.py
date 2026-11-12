from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import AnyHttpUrl, BaseModel, Field, model_validator

from app.schemas.instrument import InstrumentConfig


class EvaluationTask(BaseModel):
    messageId: str = Field(min_length=1, max_length=128)
    evaluationId: str = Field(min_length=1, max_length=128)
    templateUrl: AnyHttpUrl
    studentUrl: AnyHttpUrl
    instrumentId: str = Field(min_length=1, max_length=64)
    instrument: InstrumentConfig
    templateAnalysisId: UUID | None = None
    callbackUrl: AnyHttpUrl | None = None
    attempt: int = Field(default=1, ge=1)
    requestedAt: datetime | None = None
    templateStartSec: float | None = Field(default=None, ge=0)
    templateEndSec: float | None = Field(default=None, gt=0)
    baselineEvaluationId: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_segment(self) -> "EvaluationTask":
        if (self.templateStartSec is None) != (self.templateEndSec is None):
            raise ValueError("templateStartSec and templateEndSec must be provided together")
        if self.templateStartSec is not None and self.templateEndSec <= self.templateStartSec:
            raise ValueError("templateEndSec must be greater than templateStartSec")
        if self.baselineEvaluationId is not None and self.templateStartSec is None:
            raise ValueError("baselineEvaluationId requires a template range")
        return self

    @property
    def is_segment(self) -> bool:
        return self.templateStartSec is not None


class EvaluationError(BaseModel):
    code: str
    message: str


class EvaluationRecordResponse(BaseModel):
    evaluationId: str
    status: Literal["queued", "processing", "completed", "failed"]
    result: dict[str, Any] | None = None
    error: EvaluationError | None = None
