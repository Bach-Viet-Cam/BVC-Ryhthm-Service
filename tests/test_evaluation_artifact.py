from types import SimpleNamespace

import httpx
import numpy as np
import pytest

from app import api
from app.database import get_session
from app.evaluation.evaluator import compare_recordings
from app.services.artifact_storage_service import ArtifactStorageService, ArtifactUnavailableError
from test_evaluation_quality import setup, task


def recording(f0, tempo=100.0):
    return {"instrumentId": "dan_bau", "instrument": "Đàn Bầu", "f0": np.asarray(f0, dtype=float),
            "tempoBpm": tempo, "toleranceCents": 70.0}


def analysis_compare(teacher_length=6):
    async def compare(teacher, student):
        return {"overallScore": 70, "pitchAccuracyPercent": 80, "rhythmAccuracyPercent": 55,
                "teacherTempoBpm": 100.0, "studentTempoBpm": 90.0,
                "_analysis": {"teacherF0Hz": [220.0] * teacher_length, "studentF0Hz": [221.0, None, 222.0],
                              "studentFrameByTeacherFrame": [0, 0, 1, 1, 2, 2][:teacher_length]}}
    return compare


def test_compare_returns_aligned_pitch_data_outside_the_scored_result():
    teacher = recording([220.0, 220.0, np.nan, 330.0, 330.0, 440.0])
    student = recording([220.0, np.nan, 330.0, 440.0])

    result = compare_recordings(teacher, student)

    assert "_studentFrames" not in result
    analysis = result["_analysis"]
    assert analysis["teacherF0Hz"] == [220.0, 220.0, None, 330.0, 330.0, 440.0]
    assert analysis["studentF0Hz"] == [220.0, None, 330.0, 440.0]
    frames = analysis["studentFrameByTeacherFrame"]
    assert len(frames) == 6
    assert frames == sorted(frames) and 0 <= min(frames) and max(frames) <= 3


@pytest.mark.asyncio
async def test_completed_evaluation_stores_and_serves_the_analysis_artifact(tmp_path, monkeypatch):
    service, sessions, _ = setup(tmp_path, 0.2)
    service.template_storage = ArtifactStorageService(tmp_path / "artifacts")
    service.rhythm_analysis_service.compare = analysis_compare()
    payload = task(attempt=2)

    await service.process_direct_task(payload)

    with sessions() as session:
        record = service.repository.get(session, payload.evaluationId)
        assert record.status == "completed"
        assert record.analysis_artifact_key == f"evaluations/{payload.evaluationId}/attempt-2/analysis.json"
        assert "_analysis" not in record.result_json
        assert record.result_json["analysisArtifactUrl"] == f"/api/v1/evaluations/{payload.evaluationId}/analysis"

    settings = SimpleNamespace(internal_api_key="secret", temp_directory=str(tmp_path), artifact_root=str(tmp_path / "artifacts"))
    monkeypatch.setattr(api, "get_settings", lambda: settings)
    app = api.create_app()

    def dependency():
        with sessions() as session:
            yield session

    app.dependency_overrides[get_session] = dependency
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer secret"}) as client:
        response = await client.get(f"/api/v1/evaluations/{payload.evaluationId}/analysis")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "private, no-store"
        document = response.json()
        assert document["attempt"] == 2 and document["templateAnalysisId"] is None
        assert document["pitch"] == {"sampleRate": 22050, "hopLength": 512, "teacherF0Hz": [220.0] * 6,
                                     "studentF0Hz": [221.0, None, 222.0]}
        assert document["rhythm"] == {"teacherTempoBpm": 100.0, "studentTempoBpm": 90.0}
        assert document["alignment"]["studentFrameByTeacherFrame"] == [0, 0, 1, 1, 2, 2]

        missing = await client.get("/api/v1/evaluations/unknown/analysis")
        assert missing.status_code == 404
        unauthorized = await client.get(f"/api/v1/evaluations/{payload.evaluationId}/analysis", headers={"Authorization": "Bearer wrong"})
        assert unauthorized.status_code == 401


@pytest.mark.asyncio
async def test_analysis_route_rejects_unfinished_evaluations(tmp_path, monkeypatch):
    service, sessions, _ = setup(tmp_path, 0.2)
    payload = task()
    with sessions() as session:
        service.repository.create_if_missing(session, payload)

    monkeypatch.setattr(api, "get_settings", lambda: SimpleNamespace(internal_api_key="secret", artifact_root=str(tmp_path)))
    app = api.create_app()

    def dependency():
        with sessions() as session:
            yield session

    app.dependency_overrides[get_session] = dependency
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer secret"}) as client:
        response = await client.get(f"/api/v1/evaluations/{payload.evaluationId}/analysis")
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "EVALUATION_NOT_READY"


@pytest.mark.asyncio
async def test_artifact_write_failure_still_completes_the_evaluation(tmp_path):
    service, sessions, _ = setup(tmp_path, 0.2)

    def broken(*args):
        raise ArtifactUnavailableError("disk full")

    service.template_storage = SimpleNamespace(write_evaluation=broken)
    service.rhythm_analysis_service.compare = analysis_compare()
    payload = task()

    await service.process_direct_task(payload)

    with sessions() as session:
        record = service.repository.get(session, payload.evaluationId)
        assert record.status == "completed"
        assert record.analysis_artifact_key is None
        assert "analysisArtifactUrl" not in record.result_json
        assert record.result_json["overallScore"] == 70


def test_evaluation_ids_cannot_escape_the_artifact_directory(tmp_path):
    storage = ArtifactStorageService(tmp_path)
    with pytest.raises(ArtifactUnavailableError):
        storage.write_evaluation("../outside", 1, {})
