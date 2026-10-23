from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from app import api
from app.database import get_session
from test_template_analysis import setup as template_setup, task


def setup(tmp_path, monkeypatch):
    service, sessions = template_setup(tmp_path)
    service.settings.internal_api_key = "secret"
    monkeypatch.setattr(api, "get_settings", lambda: service.settings)
    # Route settings are the same service configuration in production.
    from app.config import get_settings
    monkeypatch.setattr("app.config.get_settings", lambda: service.settings)
    app = api.create_app()
    def dependency():
        with sessions() as session:
            yield session
    app.dependency_overrides[get_session] = dependency
    return service, sessions, app


def query(payload, **changes):
    result = dict(sheetMusicId=str(payload.sheetMusicId), mediaFileId=str(payload.mediaFileId),
                  instrumentCode=payload.instrumentCode, analysisVersion="template-pyin-v1")
    result.update(changes)
    return result


@pytest.mark.asyncio
async def test_template_api_requires_internal_key(tmp_path, monkeypatch):
    service, _, app = setup(tmp_path, monkeypatch)
    payload = task()
    await service.process_task(payload)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        for url in (f"/api/v1/template-analyses/{payload.analysisId}", f"/api/v1/template-analyses/{payload.analysisId}/artifact", "/api/v1/template-analyses/lookup"):
            response = await client.get(url, params=query(payload))
            assert response.status_code == 401


@pytest.mark.asyncio
async def test_lookup_main_audio_returns_correct_analysis_and_json(tmp_path, monkeypatch):
    service, _, app = setup(tmp_path, monkeypatch)
    payload = task()
    await service.process_task(payload)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer secret"}) as client:
        response = await client.get("/api/v1/template-analyses/lookup", params=query(payload))
        assert response.status_code == 200
        result = response.json()
        assert result["analysisId"] == str(payload.analysisId)
        assert result["sheetMusicId"] == str(payload.sheetMusicId) and result["mediaFileId"] == str(payload.mediaFileId)
        artifact = await client.get(result["artifactUrl"])
        assert artifact.status_code == 200 and artifact.headers["content-type"].startswith("application/json")
        assert artifact.json()["analysisId"] == str(payload.analysisId)
        status = await client.get(f"/api/v1/template-analyses/{payload.analysisId}")
        assert status.json()["quality"]["verdict"] == "PASS"


@pytest.mark.asyncio
async def test_lookup_distinguishes_audio_instrument_and_analysis_version(tmp_path, monkeypatch):
    service, _, app = setup(tmp_path, monkeypatch)
    payload = task()
    await service.process_task(payload)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer secret"}) as client:
        for change in ({"mediaFileId": str(uuid4())}, {"sheetMusicId": str(uuid4())}, {"instrumentCode": "sao_truc"}, {"analysisVersion": "future-version"}):
            response = await client.get("/api/v1/template-analyses/lookup", params=query(payload, **change))
            assert response.status_code == 404
        assert (await client.get("/api/v1/template-analyses/lookup", params={"sheetMusicId": str(payload.sheetMusicId)})).status_code == 422


@pytest.mark.asyncio
async def test_latest_processing_or_failed_job_does_not_fallback_to_old_completed(tmp_path, monkeypatch):
    service, sessions, app = setup(tmp_path, monkeypatch)
    original = task()
    await service.process_task(original)
    newer = task(sheetMusicId=str(original.sheetMusicId), mediaFileId=str(original.mediaFileId))
    with sessions() as session:
        saved = service.repository.create_if_missing(session, newer, {"name": "Đàn bầu", "fmin": 65, "fmax": 1200, "toleranceCents": 70}, service.quality_service.policy())
        saved.created_at = datetime.now(timezone.utc) + timedelta(days=1)
        service.repository.mark_processing(session, saved)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer secret"}) as client:
        response = await client.get("/api/v1/template-analyses/lookup", params=query(newer))
        assert response.status_code == 409 and response.json()["detail"]["status"] == "processing"
        with sessions() as session:
            service.repository.mark_failed(session, service.repository.get(session, str(newer.analysisId)), "AUDIO_SILENT", "Bản thu im lặng.")
        response = await client.get("/api/v1/template-analyses/lookup", params=query(newer))
        assert response.status_code == 409 and response.json()["detail"]["status"] == "failed"
        assert (await client.get(f"/api/v1/template-analyses/{newer.analysisId}/artifact")).status_code == 409
        assert (await client.get(f"/api/v1/template-analyses/{uuid4()}")).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("problem", ["missing", "corrupt", "traversal"])
async def test_missing_corrupt_or_escaping_artifact_returns_503(tmp_path, monkeypatch, problem):
    service, sessions, app = setup(tmp_path, monkeypatch)
    payload = task()
    await service.process_task(payload)
    with sessions() as session:
        saved = service.repository.get(session, str(payload.analysisId))
        path = service.storage.root / saved.artifact_key
        if problem == "missing":
            path.unlink()
        elif problem == "corrupt":
            path.write_bytes(b"corrupt")
        else:
            saved.artifact_key = "../secret.json"
            session.commit()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer secret"}) as client:
        response = await client.get(f"/api/v1/template-analyses/{payload.analysisId}/artifact")
        assert response.status_code == 503 and response.json()["detail"]["code"] == "ARTIFACT_UNAVAILABLE"
