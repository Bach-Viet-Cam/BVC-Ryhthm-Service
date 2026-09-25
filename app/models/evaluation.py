from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class RhythmEvaluation(Base):
    __tablename__ = "rhythm_evaluations"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    evaluation_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    template_url: Mapped[str] = mapped_column(Text)
    student_url: Mapped[str] = mapped_column(Text)
    instrument_id: Mapped[str] = mapped_column(String(64))
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
