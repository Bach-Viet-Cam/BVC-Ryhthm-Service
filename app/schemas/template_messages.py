from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, HttpUrl, field_validator

from app.evaluation.instruments import normalize_instrument_id

ANALYSIS_VERSION = "template-pyin-v1"


class TemplateAnalysisTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    messageId: UUID
    analysisId: UUID
    sheetMusicId: UUID
    mediaFileId: UUID
    mediaUrl: HttpUrl
    instrumentCode: str = Field(min_length=1, max_length=64)
    requestedAt: AwareDatetime

    @field_validator("instrumentCode")
    @classmethod
    def normalize_instrument(cls, value: str) -> str:
        return normalize_instrument_id(value)
