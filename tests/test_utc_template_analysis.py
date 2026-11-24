import asyncio
import hashlib
import json
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
from app.repositories.template_analysis_repository import TemplateAnalysisRepository, TemplateTaskConflictError
from app.schemas.template_messages import TemplateAnalysisTask
from app.services import template_feature_service as feature_module
from app.services.audio_quality_service import AudioQualityError
from app.services.template_analysis_service import TemplateAnalysisError, TemplateAnalysisService
from app.services.template_feature_service import TemplateFeatureService

FEATURES = {"pitch": {"f0Hz": [220.0, None], "voicedFlags": [True, False]}, "rhythm": {"tempoBpm": 100.0}}
POLICY = {"lowLevelDbfs": -45}
QUALITY = {"verdict": "PASS", "audio": {"durationSeconds": 1.0}, "rejections": []}


def make_task(**changes):
    values = dict(messageId=str(uuid4()), analysisId=str(uuid4()), sheetMusicId=str(uuid4()),
                  mediaFileId=str(uuid4()), mediaUrl="https://example.com/template.wav",
                  instrumentCode="dan_bau", requestedAt="2026-10-23T00:00:00Z",
                  instrument=dict(name="Đàn bầu", fminHz=65, fmaxHz=1200, toleranceCents=70))
    values.update(changes)
    return TemplateAnalysisTask(**values)


class FakeStorage:
    def __init__(self, fail=False):
        self.fail, self.writes = fail, []

    def write(self, task, document):
        if self.fail:
            raise OSError("disk full")
        self.writes.append(document)
        return {"key": f"k/{task.analysisId}.json", "sha256": "a" * 64, "sizeBytes": 10}


class FakeQuality:
    def __init__(self, reject=False, inspect_error=None):
        self.reject, self.inspect_error, self.inspected_policies = reject, inspect_error, []

    def policy(self):
        return dict(POLICY)

    async def inspect(self, path, policy):
        self.inspected_policies.append(policy)
        if self.inspect_error:
            raise self.inspect_error
        return dict(QUALITY)

    def ensure_acceptable(self, quality):
        if self.reject:
            raise AudioQualityError({"verdict": "REJECT", "rejections": [{"code": "AUDIO_SILENT", "message": "Âm thanh im lặng."}]})


class Env:
    pass


def build(tmp_path, quality=None, storage=None, download_error=None, extract_error=None):
    engine = create_engine("sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    settings = Settings(_env_file=None, temp_directory=str(tmp_path / "work"), artifact_root=str(tmp_path / "art"))
    service = TemplateAnalysisService(repository=TemplateAnalysisRepository(), session_factory=sessions, settings=settings)
    env = Env()
    env.calls = {"download": 0, "extract": 0}
    env.dirs = []

    async def download(url, directory):
        env.calls["download"] += 1
        if download_error:
            raise download_error
        directory.mkdir(parents=True, exist_ok=True)
        env.dirs.append(directory)
        path = directory / "audio.wav"
        sf.write(path, 0.2 * np.sin(2 * np.pi * 220 * np.arange(8000) / 8000), 8000)
        return path

    async def extract(path, instrument):
        env.calls["extract"] += 1
        env.instrument = instrument
        if extract_error:
            raise extract_error
        return dict(FEATURES)

    service.audio_service = SimpleNamespace(download=download)
    service.feature_service = SimpleNamespace(extract=extract)
    service.quality_service = quality or FakeQuality()
    service.storage = storage or FakeStorage()
    env.service, env.sessions = service, sessions
    return env


def record_of(env, task):
    with env.sessions() as session:
        return env.service.repository.get(session, str(task.analysisId))


# ---- process_task / _process ----
@pytest.mark.asyncio
async def test_utc_rta_001_new_task_completes_and_writes_artifact(tmp_path):
    env = build(tmp_path)
    task = make_task()
    await env.service.process_task(task)
    saved = record_of(env, task)
    assert saved.status == "completed" and saved.artifact_key == f"k/{task.analysisId}.json"
    assert len(env.service.storage.writes) == 1 and env.calls == {"download": 1, "extract": 1}


@pytest.mark.asyncio
async def test_utc_rta_002_document_contains_source_instrument_and_features(tmp_path):
    env = build(tmp_path)
    task = make_task()
    await env.service.process_task(task)
    doc = env.service.storage.writes[0]
    assert doc["schemaVersion"] == 1 and doc["analysisId"] == str(task.analysisId)
    assert doc["instrument"] == {"code": "dan_bau", "name": "Đàn bầu", "fminHz": 65.0, "fmaxHz": 1200.0, "toleranceCents": 70.0}
    assert doc["audio"]["analysisSampleRate"] == 22050 and doc["pitch"] == FEATURES["pitch"]
    assert doc["source"]["sizeBytes"] > 0 and len(doc["source"]["sha256"]) == 64
    assert record_of(env, task).source_sha256 == doc["source"]["sha256"]


@pytest.mark.asyncio
async def test_utc_rta_003_work_directory_removed_after_success(tmp_path):
    env = build(tmp_path)
    await env.service.process_task(make_task())
    assert env.dirs and not env.dirs[0].exists()


@pytest.mark.asyncio
async def test_utc_rta_004_completed_task_is_skipped(tmp_path):
    env = build(tmp_path)
    task = make_task()
    await env.service.process_task(task)
    await env.service.process_task(task.model_copy(update={"messageId": uuid4()}))
    assert env.calls == {"download": 1, "extract": 1} and len(env.service.storage.writes) == 1


@pytest.mark.asyncio
async def test_utc_rta_005_failed_task_is_skipped(tmp_path):
    env = build(tmp_path)
    task = make_task()
    env.service.record_failure(task, "X", "boom")
    await env.service.process_task(task)
    assert env.calls["download"] == 0 and record_of(env, task).status == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("sheetMusicId", uuid4()), ("mediaFileId", uuid4()),
                                         ("instrumentCode", "other"), ("mediaUrl", "https://example.com/other.wav")])
async def test_utc_rta_006_conflicting_task_raises_without_download(tmp_path, field, value):
    env = build(tmp_path)
    task = make_task()
    await env.service.process_task(task)
    with pytest.raises(TemplateTaskConflictError):
        await env.service.process_task(task.model_copy(update={field: value}))
    assert env.calls["download"] == 1 and record_of(env, task).status == "completed"


@pytest.mark.asyncio
async def test_utc_rta_007_download_failure_propagates_and_no_artifact(tmp_path):
    env = build(tmp_path, download_error=RuntimeError("network down"))
    task = make_task()
    with pytest.raises(RuntimeError, match="network down"):
        await env.service.process_task(task)
    assert env.calls["extract"] == 0 and env.service.storage.writes == []
    assert record_of(env, task).status == "processing"
    assert not (tmp_path / "work").exists() or not any((tmp_path / "work").iterdir())


@pytest.mark.asyncio
async def test_utc_rta_008_quality_rejection_stops_before_extract_and_cleans_up(tmp_path):
    env = build(tmp_path, quality=FakeQuality(reject=True))
    with pytest.raises(AudioQualityError) as raised:
        await env.service.process_task(make_task())
    assert raised.value.code == "AUDIO_SILENT"
    assert env.calls["extract"] == 0 and env.service.storage.writes == []
    assert not env.dirs[0].exists()


@pytest.mark.asyncio
async def test_utc_rta_009_quality_inspect_error_propagates(tmp_path):
    env = build(tmp_path, quality=FakeQuality(inspect_error=ValueError("bad wav")))
    with pytest.raises(ValueError, match="bad wav"):
        await env.service.process_task(make_task())
    assert env.calls["extract"] == 0


@pytest.mark.asyncio
async def test_utc_rta_010_feature_extraction_failure_no_artifact_cleanup(tmp_path):
    env = build(tmp_path, extract_error=RuntimeError("pyin crashed"))
    task = make_task()
    with pytest.raises(RuntimeError, match="pyin crashed"):
        await env.service.process_task(task)
    assert env.service.storage.writes == [] and not env.dirs[0].exists()
    assert record_of(env, task).status == "processing"


@pytest.mark.asyncio
async def test_utc_rta_011_storage_write_failure_leaves_record_not_completed(tmp_path):
    env = build(tmp_path, storage=FakeStorage(fail=True))
    task = make_task()
    with pytest.raises(OSError, match="disk full"):
        await env.service.process_task(task)
    saved = record_of(env, task)
    assert saved.status == "processing" and saved.artifact_key is None
    assert not env.dirs[0].exists()


@pytest.mark.asyncio
async def test_utc_rta_012_processing_record_uses_stored_policy_and_instrument(tmp_path):
    quality = FakeQuality()
    env = build(tmp_path, quality=quality)
    task = make_task()
    stored = {"name": "Stored", "fmin": 50.0, "fmax": 900.0, "toleranceCents": 40}
    with env.sessions() as session:
        rec = env.service.repository.create_if_missing(session, task, stored, {"lowLevelDbfs": -99})
        env.service.repository.mark_processing(session, rec)
    await env.service.process_task(task)
    assert quality.inspected_policies == [{"lowLevelDbfs": -99}]
    assert env.instrument == stored
    assert env.service.storage.writes[0]["instrument"]["name"] == "Stored"


@pytest.mark.asyncio
async def test_utc_rta_013_process_task_runs_through_execution_gate(tmp_path):
    env = build(tmp_path)
    seen = []

    class Gate:
        async def run(self, operation):
            seen.append("run")
            await operation()
    env.service.execution = Gate()
    await env.service.process_task(make_task())
    assert seen == ["run"] and env.calls["download"] == 1


def test_utc_rta_014_find_or_create_persists_queued_record_with_snapshots(tmp_path):
    env = build(tmp_path)
    task = make_task()
    with env.sessions() as session:
        rec = env.service._find_or_create(session, task)
        assert rec.status == "queued" and rec.quality_policy == POLICY
        assert rec.instrument_config == task.instrument.to_stored()
        assert env.service._find_or_create(session, task) is rec


# ---- record_failure ----
def test_utc_rta_015_record_failure_for_unseen_task_creates_failed_record(tmp_path):
    env = build(tmp_path)
    task = make_task()
    env.service.record_failure(task, "AUDIO_SILENT", "Âm thanh im lặng.", {"verdict": "REJECT"})
    saved = record_of(env, task)
    assert saved.status == "failed" and saved.error_code == "AUDIO_SILENT"
    assert saved.error_message == "Âm thanh im lặng." and saved.quality_report == {"verdict": "REJECT"}


@pytest.mark.asyncio
async def test_utc_rta_016_record_failure_does_not_override_completed(tmp_path):
    env = build(tmp_path)
    task = make_task()
    await env.service.process_task(task)
    env.service.record_failure(task, "X", "late failure")
    saved = record_of(env, task)
    assert saved.status == "completed" and saved.error_code is None


def test_utc_rta_017_record_failure_without_report_keeps_existing_report(tmp_path):
    env = build(tmp_path)
    task = make_task()
    env.service.record_failure(task, "A", "first", {"verdict": "REJECT"})
    env.service.record_failure(task, "B", "second")
    saved = record_of(env, task)
    assert saved.error_code == "B" and saved.quality_report == {"verdict": "REJECT"}


def test_utc_rta_018_record_failure_conflicting_task_raises(tmp_path):
    env = build(tmp_path)
    task = make_task()
    env.service.record_failure(task, "A", "first")
    with pytest.raises(TemplateTaskConflictError):
        env.service.record_failure(task.model_copy(update={"mediaFileId": uuid4()}), "A", "x")


# ---- _checksum ----
def test_utc_rta_019_checksum_matches_sha256(tmp_path):
    p = tmp_path / "a.bin"
    p.write_bytes(b"hello world")
    assert TemplateAnalysisService._checksum(p) == hashlib.sha256(b"hello world").hexdigest()


def test_utc_rta_020_checksum_empty_file(tmp_path):
    p = tmp_path / "empty.bin"
    p.write_bytes(b"")
    assert TemplateAnalysisService._checksum(p) == hashlib.sha256(b"").hexdigest()


def test_utc_rta_021_checksum_multi_chunk_file(tmp_path):
    data = b"x" * (1024 * 1024 * 2 + 5)
    p = tmp_path / "big.bin"
    p.write_bytes(data)
    assert TemplateAnalysisService._checksum(p) == hashlib.sha256(data).hexdigest()


def test_utc_rta_022_checksum_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        TemplateAnalysisService._checksum(tmp_path / "nope.bin")


# ---- TemplateAnalysisError ----
def test_utc_rta_023_template_analysis_error_carries_code_and_message():
    err = TemplateAnalysisError("TEMPLATE_X", "Lỗi mẫu")
    assert isinstance(err, ValueError) and err.code == "TEMPLATE_X" and str(err) == "Lỗi mẫu"


# ---- TemplateFeatureService ----
def test_utc_rta_024_extract_maps_nan_to_none_with_mocked_librosa_helpers(tmp_path, monkeypatch):
    seen = {}

    def fake_f0(path, fmin, fmax):
        seen["args"] = (path, fmin, fmax)
        return np.array([220.0, np.nan, 440.0])
    monkeypatch.setattr(feature_module, "_extract_f0", fake_f0)
    monkeypatch.setattr(feature_module, "_estimate_tempo", lambda path: 96.5)
    out = TemplateFeatureService._extract(tmp_path / "a.wav", {"fmin": "65", "fmax": 1200})
    assert seen["args"] == (tmp_path / "a.wav", 65.0, 1200.0)
    assert out["pitch"]["f0Hz"] == [220.0, None, 440.0]
    assert out["pitch"]["voicedFlags"] == [True, False, True]
    assert out["pitch"]["method"] == "pyin" and out["pitch"]["hopLength"] == 512
    assert out["rhythm"]["tempoBpm"] == 96.5 and "librosa" in out["software"]
    json.dumps(out, allow_nan=False)


def test_utc_rta_025_extract_non_finite_tempo_becomes_none(tmp_path, monkeypatch):
    monkeypatch.setattr(feature_module, "_extract_f0", lambda *a: np.array([100.0]))
    monkeypatch.setattr(feature_module, "_estimate_tempo", lambda path: float("nan"))
    assert TemplateFeatureService._extract(tmp_path / "a.wav", {"fmin": 65, "fmax": 1200})["rhythm"]["tempoBpm"] is None


def test_utc_rta_026_extract_empty_f0_gives_empty_lists(tmp_path, monkeypatch):
    monkeypatch.setattr(feature_module, "_extract_f0", lambda *a: np.array([]))
    monkeypatch.setattr(feature_module, "_estimate_tempo", lambda path: 0.0)
    out = TemplateFeatureService._extract(tmp_path / "a.wav", {"fmin": 65, "fmax": 1200})
    assert out["pitch"]["f0Hz"] == [] and out["pitch"]["voicedFlags"] == []


def test_utc_rta_027_extract_missing_instrument_range_raises_keyerror(tmp_path, monkeypatch):
    monkeypatch.setattr(feature_module, "_extract_f0", lambda *a: np.array([1.0]))
    with pytest.raises(KeyError):
        TemplateFeatureService._extract(tmp_path / "a.wav", {"fmin": 65})


@pytest.mark.asyncio
async def test_utc_rta_028_extract_async_on_tiny_synthetic_wav(tmp_path):
    path = tmp_path / "tone.wav"
    sf.write(path, 0.2 * np.sin(2 * np.pi * 220 * np.arange(11025) / 22050), 22050)
    out = await TemplateFeatureService().extract(path, {"fmin": 65.0, "fmax": 1200.0})
    assert len(out["pitch"]["f0Hz"]) == len(out["pitch"]["voicedFlags"]) > 0
    json.dumps(out, allow_nan=False)


@pytest.mark.asyncio
async def test_utc_rta_029_extract_async_propagates_helper_error(tmp_path, monkeypatch):
    def boom(*a):
        raise RuntimeError("decode failed")
    monkeypatch.setattr(feature_module, "_extract_f0", boom)
    with pytest.raises(RuntimeError, match="decode failed"):
        await TemplateFeatureService().extract(tmp_path / "a.wav", {"fmin": 65, "fmax": 1200})
