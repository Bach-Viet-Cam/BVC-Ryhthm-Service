from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

KeyName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
KeyValue = Annotated[str, StringConstraints(strip_whitespace=True, min_length=10, max_length=512)]


class LlmKeyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: KeyName
    key: KeyValue


class LlmKeyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: KeyName | None = None
    status: Literal["active", "disabled"] | None = None
    key: KeyValue | None = None

    @model_validator(mode="after")
    def require_one_field(self) -> "LlmKeyUpdate":
        if self.name is None and self.status is None and self.key is None:
            raise ValueError("Cần ít nhất một trường để cập nhật.")
        return self


class LlmKeyResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    id: str
    name: str
    status: str
    cooldownUntil: datetime | None = None
    lastUsedAt: datetime | None = None
    lastErrorCode: str | None = None
    successCount: int = 0
    failCount: int = 0
    createdAt: datetime


class LlmKeyListResponse(BaseModel):
    items: list[LlmKeyResponse] = Field(default_factory=list)
