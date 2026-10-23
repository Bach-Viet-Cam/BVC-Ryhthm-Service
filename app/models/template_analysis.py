from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Index, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class TemplateAnalysis(Base):
    __tablename__ = "template_analyses"
    __table_args__ = (Index("ix_template_analyses_lookup", "sheet_music_id", "media_file_id",
                           "instrument_code", "analysis_version", "created_at"),)

    analysis_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    message_id: Mapped[str] = mapped_column(String(36))
    sheet_music_id: Mapped[str] = mapped_column(String(36))
    media_file_id: Mapped[str] = mapped_column(String(36))
    media_url: Mapped[str] = mapped_column(Text)
    instrument_code: Mapped[str] = mapped_column(String(64))
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    analysis_version: Mapped[str] = mapped_column(String(64))
    instrument_config: Mapped[dict[str, Any]] = mapped_column(JSON)
    quality_policy: Mapped[dict[str, Any]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(32), index=True)
    source_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    artifact_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    artifact_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    artifact_size_bytes: Mapped[int | None] = mapped_column(nullable=True)
    quality_report: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
