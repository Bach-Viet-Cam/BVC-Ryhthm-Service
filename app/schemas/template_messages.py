from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, HttpUrl

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
