import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import numpy as np
import pytest
import soundfile as sf
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.database import Base


def imports():
    from app.schemas.template_messages import TemplateAnalysisTask
    from app.services.template_analysis_service import TemplateAnalysisService
    from app.repositories.template_analysis_repository import TemplateAnalysisRepository, TemplateTaskConflictError
    from app.services.artifact_storage_service import ArtifactStorageService, ArtifactUnavailableError
    return SimpleNamespace(Task=TemplateAnalysisTask, Service=TemplateAnalysisService,
                           Repository=TemplateAnalysisRepository, Conflict=TemplateTaskConflictError,
                           Storage=ArtifactStorageService, Unavailable=ArtifactUnavailableError)


def task(**changes):
    values = dict(messageId=str(uuid4()), analysisId=str(uuid4()), sheetMusicId=str(uuid4()),
                  mediaFileId=str(uuid4()), mediaUrl="https://example.com/template.wav",
                  instrumentCode="dan_bau", requestedAt="2026-10-23T00:00:00Z")
    values.update(changes)
    return imports().Task(**values)


def setup(tmp_path):
    module = imports()
    engine = create_engine("sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    settings = Settings(_env_file=None, temp_directory=str(tmp_path / "work"), artifact_root=str(tmp_path / "artifacts"))
    service = module.Service(session_factory=sessions, settings=settings)

    async def download(url, directory):
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "audio.wav"
        sf.write(path, 0.2 * np.sin(2 * np.pi * 220 * np.arange(8000) / 8000), 8000)
        return path

    async def extract(path, instrument):
        return {"pitch": {"method": "pyin", "sampleRate": 22050, "hopLength": 512,
                          "frameLength": 2048, "center": True, "f0Hz": [220.0, None], "voicedFlags": [True, False]},
                "rhythm": {"tempoBpm": 100.0}, "software": {"librosa": "test"}}

    service.audio_service = SimpleNamespace(download=download)
    service.feature_service = SimpleNamespace(extract=extract)
    return service, sessions


@pytest.mark.parametrize("change", [
    {"sheetMusicId": "not-uuid"}, {"mediaFileId": "not-uuid"},
    {"requestedAt": "2026-10-23T00:00:00"}, {"unexpected": True},
    {"mediaUrl": "file:///tmp/template.wav"},
])
def test_template_contract_validates_main_audio_and_aware_timestamp(change):
    module = imports()
    with pytest.raises(ValueError):
        task(**change)


@pytest.mark.asyncio
async def test_template_persists_main_audio_keys_and_real_artifact(tmp_path):
    service, sessions = setup(tmp_path)
    payload = task()
    await service.process_task(payload)
    with sessions() as session:
        saved = service.repository.get(session, str(payload.analysisId))
        assert saved.status == "completed"
        assert saved.sheet_music_id == str(payload.sheetMusicId)
        assert saved.media_file_id == str(payload.mediaFileId)
        assert saved.artifact_key == f"template-analyses/{payload.sheetMusicId}/{payload.mediaFileId}/{payload.analysisId}/result.json"
        raw = service.storage.read(saved.artifact_key, saved.artifact_sha256)
        assert hashlib.sha256(raw).hexdigest() == saved.artifact_sha256
        assert len(raw) == saved.artifact_size_bytes
        document = json.loads(raw)
        assert document["sheetMusicId"] == str(payload.sheetMusicId)
        assert document["mediaFileId"] == str(payload.mediaFileId)
        assert document["pitch"]["f0Hz"] == [220.0, None]
        assert document["quality"]["verdict"] == "PASS"
        assert document["instrument"]["fminHz"] == 65.0
        assert document["instrument"]["fmaxHz"] == 1200.0
        assert saved.source_sha256 == document["source"]["sha256"]
    assert not any((tmp_path / "work").iterdir())


@pytest.mark.asyncio
async def test_completed_template_duplicate_skips_download_and_file_write(tmp_path):
    service, sessions = setup(tmp_path)
    payload = task()
    await service.process_task(payload)
    async def unexpected(*args):
        pytest.fail("Completed duplicate must not download again")
    service.audio_service = SimpleNamespace(download=unexpected)
    await service.process_task(payload.model_copy(update={"messageId": uuid4()}))
    with sessions() as session:
        assert service.repository.get(session, str(payload.analysisId)).status == "completed"


@pytest.mark.asyncio
async def test_same_id_changed_main_or_audio_conflicts_without_overwrite(tmp_path):
    service, sessions = setup(tmp_path)
    payload = task()
    await service.process_task(payload)
    for field in ("sheetMusicId", "mediaFileId"):
        with pytest.raises(imports().Conflict):
            await service.process_task(payload.model_copy(update={field: uuid4()}))
    with sessions() as session:
        saved = service.repository.get(session, str(payload.analysisId))
        assert saved.sheet_music_id == str(payload.sheetMusicId) and saved.status == "completed"


@pytest.mark.asyncio
async def test_processing_record_recovers_with_original_policy_snapshot(tmp_path, monkeypatch):
    service, sessions = setup(tmp_path)
    payload = task()
    with sessions() as session:
        saved = service.repository.create_if_missing(session, payload, {"name": "Đàn bầu", "fmin": 65.0, "fmax": 1200.0, "toleranceCents": 70}, service.quality_service.policy())
        service.repository.mark_processing(session, saved)
    from app.constants import audio_quality
    monkeypatch.setattr(audio_quality, "LOW_LEVEL_DBFS", -1)
    await service.process_task(payload)
    with sessions() as session:
        saved = service.repository.get(session, str(payload.analysisId))
        assert saved.status == "completed"
        assert saved.quality_report["policy"]["lowLevelDbfs"] == -45


@pytest.mark.asyncio
async def test_template_quality_rejection_does_not_extract_or_write(tmp_path):
    service, sessions = setup(tmp_path)
    payload = task()
    async def silence(url, directory):
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "audio.wav"
        sf.write(path, np.zeros(800), 8000)
        return path
    async def unexpected(*args):
        pytest.fail("Rejected template must not extract features")
    service.audio_service = SimpleNamespace(download=silence)
    service.feature_service = SimpleNamespace(extract=unexpected)
    from app.services.audio_quality_service import AudioQualityError
    with pytest.raises(AudioQualityError) as raised:
        await service.process_task(payload)
    service.record_failure(payload, raised.value.code, str(raised.value), raised.value.report)
    with sessions() as session:
        saved = service.repository.get(session, str(payload.analysisId))
        assert saved.status == "failed" and saved.error_code == "AUDIO_SILENT"
        assert saved.artifact_key is None and saved.quality_report["verdict"] == "REJECT"


def test_atomic_storage_preserves_existing_file_on_invalid_json_and_checks_integrity(tmp_path):
    module = imports()
    storage = module.Storage(tmp_path)
    payload = task()
    descriptor = storage.write(payload, {"f0Hz": [1.0, None]})
    original = storage.read(descriptor["key"], descriptor["sha256"])
    with pytest.raises(ValueError):
        storage.write(payload, {"f0Hz": [np.nan]})
    assert storage.read(descriptor["key"], descriptor["sha256"]) == original
    path = tmp_path / descriptor["key"]
    path.write_bytes(b"corrupt")
    with pytest.raises(module.Unavailable):
        storage.read(descriptor["key"], descriptor["sha256"])
    assert not list(path.parent.glob("*.tmp"))


@pytest.mark.parametrize("key", ["../secret.json", "/tmp/secret.json", "folder/../../secret", "folder\\secret"])
def test_artifact_read_rejects_path_traversal(tmp_path, key):
    module = imports()
    with pytest.raises(module.Unavailable):
        module.Storage(tmp_path).read(key, "checksum")


@pytest.mark.asyncio
async def test_template_feature_extraction_matches_legacy_helpers(tmp_path):
    from app.services.template_feature_service import TemplateFeatureService
    from app.evaluation.evaluator import _extract_f0, _estimate_tempo
    path = tmp_path / "tone.wav"
    sf.write(path, 0.2 * np.sin(2 * np.pi * 220 * np.arange(11025) / 22050), 22050)
    config = {"fmin": 65.0, "fmax": 1200.0}
    features = await TemplateFeatureService().extract(path, config)
    expected = _extract_f0(path, 65.0, 1200.0)
    actual = np.array([np.nan if n is None else n for n in features["pitch"]["f0Hz"]])
    np.testing.assert_allclose(actual, expected, equal_nan=True)
    assert features["rhythm"]["tempoBpm"] == _estimate_tempo(path)
    json.dumps(features, allow_nan=False)


@pytest.mark.asyncio
async def test_actual_template_pipeline_writes_json_with_real_pitch_features(tmp_path):
    from app.services.template_feature_service import TemplateFeatureService
    service, sessions = setup(tmp_path)
    service.feature_service = TemplateFeatureService()
    payload = task()
    await service.process_task(payload)
    with sessions() as session:
        saved = service.repository.get(session, str(payload.analysisId))
        document = json.loads(service.storage.read(saved.artifact_key, saved.artifact_sha256))
        assert saved.status == "completed"
        voiced = [n for n in document["pitch"]["f0Hz"] if n is not None]
        assert len(voiced) > 0 and np.median(voiced) == pytest.approx(220, rel=0.03)
        assert len(document["pitch"]["f0Hz"]) == len(document["pitch"]["voicedFlags"])
        json.dumps(document, allow_nan=False)



def test_atomic_replace_failure_preserves_previous_json_and_cleans_temp(tmp_path, monkeypatch):
    from app.services import artifact_storage_service as module
    storage = imports().Storage(tmp_path)
    payload = task()
    descriptor = storage.write(payload, {"result": "original"})
    original = storage.read(descriptor["key"], descriptor["sha256"])
    def fail_replace(*args):
        raise PermissionError("Không thể thay file.")
    monkeypatch.setattr(module.os, "replace", fail_replace)
    with pytest.raises(PermissionError):
        storage.write(payload, {"result": "replacement"})
    assert storage.read(descriptor["key"], descriptor["sha256"]) == original
    assert not list((tmp_path / descriptor["key"]).parent.glob("*.tmp"))
