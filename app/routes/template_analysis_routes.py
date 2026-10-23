from collections.abc import Callable
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app import config
from app.database import get_session
from app.models.template_analysis import TemplateAnalysis
from app.repositories.template_analysis_repository import TemplateAnalysisRepository
from app.services.artifact_storage_service import ArtifactStorageService, ArtifactUnavailableError


def _metadata(record: TemplateAnalysis) -> dict[str, Any]:
    return {
        "analysisId": record.analysis_id, "sheetMusicId": record.sheet_music_id,
        "mediaFileId": record.media_file_id, "instrumentCode": record.instrument_code,
        "analysisVersion": record.analysis_version, "status": record.status,
        "quality": record.quality_report, "sourceSha256": record.source_sha256,
        "error": {"code": record.error_code, "message": record.error_message} if record.error_code else None,
        "createdAt": record.created_at, "startedAt": record.started_at, "completedAt": record.completed_at,
        "artifactUrl": f"/api/v1/template-analyses/{record.analysis_id}/artifact" if record.status == "completed" else None,
    }


def _not_ready(record: TemplateAnalysis) -> HTTPException:
    return HTTPException(status_code=409, detail={
        "code": "TEMPLATE_NOT_READY", "message": "Kết quả phân tích template chưa sẵn sàng.",
        "analysisId": record.analysis_id, "status": record.status,
        "error": {"code": record.error_code, "message": record.error_message} if record.error_code else None,
    })


def create_template_router(require_auth: Callable[[str | None], None]) -> APIRouter:
    def authenticated(authorization: str | None = Header(default=None)) -> None:
        require_auth(authorization)

    router = APIRouter(prefix="/api/v1/template-analyses", dependencies=[Depends(authenticated)])
    repository = TemplateAnalysisRepository()

    def find(session: Session, analysis_id: UUID) -> TemplateAnalysis:
        record = repository.get(session, str(analysis_id))
        if record is None:
            raise HTTPException(status_code=404, detail={"code": "TEMPLATE_NOT_FOUND", "message": "Không tìm thấy yêu cầu phân tích template."})
        return record

    # Static lookup must precede the UUID path route.
    @router.get("/lookup")
    def lookup(
        sheet_music_id: UUID = Query(alias="sheetMusicId"),
        media_file_id: UUID = Query(alias="mediaFileId"),
        instrument_code: str = Query(alias="instrumentCode", min_length=1, max_length=64),
        analysis_version: str = Query(alias="analysisVersion", min_length=1, max_length=64),
        session: Session = Depends(get_session),
    ) -> dict[str, Any]:
        record = repository.lookup(session, str(sheet_music_id), str(media_file_id), instrument_code, analysis_version)
        if record is None:
            raise HTTPException(status_code=404, detail={"code": "TEMPLATE_NOT_FOUND", "message": "Chưa có kết quả cho MAIN, audio và phiên bản phân tích này."})
        if record.status != "completed":
            raise _not_ready(record)
        return _metadata(record)

    @router.get("/{analysis_id}")
    def status(analysis_id: UUID, session: Session = Depends(get_session)) -> dict[str, Any]:
        return _metadata(find(session, analysis_id))

    @router.get("/{analysis_id}/artifact")
    def artifact(analysis_id: UUID, session: Session = Depends(get_session)) -> Response:
        record = find(session, analysis_id)
        if record.status != "completed":
            raise _not_ready(record)
        try:
            if not record.artifact_key or not record.artifact_sha256:
                raise ArtifactUnavailableError("Thiếu thông tin file kết quả.")
            content = ArtifactStorageService(config.get_settings().artifact_root).read(record.artifact_key, record.artifact_sha256)
        except ArtifactUnavailableError:
            raise HTTPException(status_code=503, detail={"code": "ARTIFACT_UNAVAILABLE", "message": "Chưa thể đọc file kết quả phân tích. Vui lòng kiểm tra lưu trữ."}) from None
        return Response(content, media_type="application/json", headers={"Cache-Control": "private, no-store"})

    return router
