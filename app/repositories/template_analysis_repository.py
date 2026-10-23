from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.template_analysis import TemplateAnalysis
from app.schemas.template_messages import ANALYSIS_VERSION, TemplateAnalysisTask


class TemplateTaskConflictError(ValueError):
    code = "TEMPLATE_TASK_CONFLICT"


class TemplateAnalysisRepository:
    def get(self, session: Session, analysis_id: str) -> TemplateAnalysis | None:
        return session.get(TemplateAnalysis, analysis_id)

    def create_if_missing(self, session: Session, task: TemplateAnalysisTask,
                          instrument_config: dict[str, Any], quality_policy: dict[str, Any]) -> TemplateAnalysis:
        record = self.get(session, str(task.analysisId))
        if record is not None:
            existing = (record.sheet_music_id, record.media_file_id, record.media_url, record.instrument_code)
            incoming = (str(task.sheetMusicId), str(task.mediaFileId), str(task.mediaUrl), task.instrumentCode)
            if existing != incoming:
                raise TemplateTaskConflictError("Mã phân tích đã được dùng cho MAIN, audio hoặc nhạc cụ khác.")
            return record
        record = TemplateAnalysis(
            analysis_id=str(task.analysisId), message_id=str(task.messageId),
            sheet_music_id=str(task.sheetMusicId), media_file_id=str(task.mediaFileId),
            media_url=str(task.mediaUrl), instrument_code=task.instrumentCode, requested_at=task.requestedAt,
            analysis_version=ANALYSIS_VERSION, instrument_config=dict(instrument_config),
            quality_policy=dict(quality_policy), status="queued",
        )
        session.add(record)
        session.commit()
        session.refresh(record)
        return record

    def lookup(self, session: Session, sheet_music_id: str, media_file_id: str,
               instrument_code: str, analysis_version: str) -> TemplateAnalysis | None:
        return session.scalar(select(TemplateAnalysis).where(
            TemplateAnalysis.sheet_music_id == sheet_music_id,
            TemplateAnalysis.media_file_id == media_file_id,
            TemplateAnalysis.instrument_code == instrument_code,
            TemplateAnalysis.analysis_version == analysis_version,
        ).order_by(TemplateAnalysis.created_at.desc(), TemplateAnalysis.analysis_id.desc()).limit(1))

    def mark_processing(self, session: Session, record: TemplateAnalysis) -> None:
        record.status = "processing"
        record.started_at = datetime.now(timezone.utc)
        record.completed_at = None
        record.error_code = record.error_message = None
        session.commit()

    def mark_completed(self, session: Session, record: TemplateAnalysis, source_sha256: str,
                       artifact: dict[str, Any], quality_report: dict[str, Any]) -> None:
        record.status = "completed"
        record.source_sha256 = source_sha256
        record.artifact_key = artifact["key"]
        record.artifact_sha256 = artifact["sha256"]
        record.artifact_size_bytes = artifact["sizeBytes"]
        record.quality_report = quality_report
        record.error_code = record.error_message = None
        record.completed_at = datetime.now(timezone.utc)
        session.commit()

    def mark_failed(self, session: Session, record: TemplateAnalysis, code: str, message: str,
                    quality_report: dict[str, Any] | None = None) -> None:
        record.status = "failed"
        record.error_code, record.error_message = code, message
        if quality_report is not None:
            record.quality_report = quality_report
        record.completed_at = datetime.now(timezone.utc)
        session.commit()
