from pydantic import BaseModel, Field, model_validator


class InstrumentTune(BaseModel):
    """Một dây buông/nốt chuẩn trong bảng lên dây của nhạc cụ."""

    name: str = Field(min_length=1, max_length=128)
    pitchHz: float = Field(gt=0)


class InstrumentConfig(BaseModel):
    """Thông số nhạc cụ do backend gửi kèm mỗi job."""

    name: str = Field(min_length=1, max_length=128)
    fminHz: float = Field(gt=0)
    fmaxHz: float = Field(gt=0)
    toleranceCents: float = Field(gt=0)
    # Không bắt buộc: job phân tích bản mẫu không cần, thiếu thì chỉ mất phần chẩn đoán lỗi lên dây
    tunes: list[InstrumentTune] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def validate_range(self) -> "InstrumentConfig":
        if self.fminHz >= self.fmaxHz:
            raise ValueError("fminHz must be less than fmaxHz")
        return self

    def to_stored(self) -> dict[str, float | str]:
        return {"name": self.name, "fmin": float(self.fminHz), "fmax": float(self.fmaxHz),
                "toleranceCents": float(self.toleranceCents)}
