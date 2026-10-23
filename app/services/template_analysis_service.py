import asyncio
import hashlib
import shutil
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database import SessionFactory
from app.evaluation.instruments import INSTRUMENTS
from app.repositories.template_analysis_repository import TemplateAnalysisRepository
from app.schemas.template_messages import TemplateAnalysisTask
from app.services.artifact_storage_service import ArtifactStorageService
from app.services.audio_quality_service import AudioQualityService
from app.services.audio_service import AudioService
from app.services.job_execution_service import JobExecutionService
from app.services.template_feature_service import TemplateFeatureService


class TemplateAnalysisError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class TemplateAnalysisService:
    def __init__(self, repository: TemplateAnalysisRepository | None = None,
                 session_factory: Callable[[], Session] = SessionFactory,
                 settings: Settings | None = None) -> None:
        self.repository = repository or TemplateAnalysisRepository()
        self.session_factory = session_factory
        self.settings = settings or get_settings()
        self.audio_service = AudioService()
        self.quality_service = AudioQualityService()
        self.feature_service = TemplateFeatureService()
        self.storage = ArtifactStorageService(self.settings.artifact_root)
        self.execution = JobExecutionService()

    async def process_task(self, task: TemplateAnalysisTask) -> None:
        await self.execution.run(lambda: self._process(task))

    def _find_or_create(self, session: Session, task: TemplateAnalysisTask):
        return self.repository.create_if_missing(
            session, task, INSTRUMENTS.get(task.instrumentCode, {}), self.quality_service.policy(),
        )

    async def _process(self, task: TemplateAnalysisTask) -> None:
        with self.session_factory() as session:
            record = self._find_or_create(session, task)
            if record.status in {"completed", "failed"}:
                return
            instrument = dict(record.instrument_config)
            policy = dict(record.quality_policy)
            analysis_version = record.analysis_version
            self.repository.mark_processing(session, record)
        if not instrument:
            raise TemplateAnalysisError("UNSUPPORTED_INSTRUMENT", "Nhạc cụ chưa được dịch vụ phân tích hỗ trợ.")

        work_directory = Path(self.settings.temp_directory) / uuid4().hex
        try:
            path = await self.audio_service.download(str(task.mediaUrl), work_directory)
            source_hash = await asyncio.to_thread(self._checksum, path)
            quality = await self.quality_service.inspect(path, policy)
            self.quality_service.ensure_acceptable(quality)
            features = await self.feature_service.extract(path, instrument)
            document = {
                "schemaVersion": 1, "analysisVersion": analysis_version,
                "analysisId": str(task.analysisId), "sheetMusicId": str(task.sheetMusicId),
                "mediaFileId": str(task.mediaFileId), "createdAt": datetime.now(timezone.utc).isoformat(),
                "source": {"mediaUrl": str(task.mediaUrl), "sha256": source_hash, "sizeBytes": path.stat().st_size},
                "instrument": {"code": task.instrumentCode, "name": instrument["name"],
                               "fminHz": float(instrument["fmin"]), "fmaxHz": float(instrument["fmax"]),
                               "toleranceCents": float(instrument["toleranceCents"])},
                "audio": {**quality["audio"], "analysisSampleRate": 22050}, "quality": quality,
                **features,
            }
            artifact = await asyncio.to_thread(self.storage.write, task, document)
            with self.session_factory() as session:
                record = self.repository.get(session, str(task.analysisId))
                self.repository.mark_completed(session, record, source_hash, artifact, quality)
        finally:
            await asyncio.to_thread(shutil.rmtree, work_directory, True)

    def record_failure(self, task: TemplateAnalysisTask, code: str, message: str,
                       quality_report: dict[str, Any] | None = None) -> None:
        with self.session_factory() as session:
            record = self._find_or_create(session, task)
            if record.status != "completed":
                self.repository.mark_failed(session, record, code, message, quality_report)

    @staticmethod
    def _checksum(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
