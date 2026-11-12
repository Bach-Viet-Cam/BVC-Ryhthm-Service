import asyncio
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
from app.repositories.evaluation_repository import EvaluationRepository
from app.schemas.instrument import InstrumentConfig
from app.schemas.messages import EvaluationTask
from app.services.evaluation_service import EvaluationService
from app.services.job_execution_service import JobExecutionService


def setup(tmp_path, amplitude):
    engine = create_engine("sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    service = EvaluationService(EvaluationRepository(), sessions, str(tmp_path / "work"))
    calls = []
    async def download(url, directory):
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / ("student.wav" if "student" in url else "teacher.wav")
        level = amplitude if "student" in url else 0.2
        sf.write(path, level * np.sin(2 * np.pi * 220 * np.arange(8000) / 8000), 8000)
        return path
    async def analyze(path, instrument, config=None):
        calls.append("analyze")
        return {"path": path, "instrumentId": instrument}
    async def compare(teacher, student):
        calls.append("compare")
        return {"overallScore": 88, "pitchAccuracyPercent": 90, "rhythmAccuracyPercent": 85}
    async def feedback(result):
        calls.append("feedback")
        return "Tiếp tục luyện tập."
    service.audio_service = SimpleNamespace(download=download)
    service.rhythm_analysis_service = SimpleNamespace(analyze=analyze, compare=compare)
    service.feedback_service = SimpleNamespace(generate=feedback)
    return service, sessions, calls


def task(**changes):
    values = dict(messageId=str(uuid4()), evaluationId=str(uuid4()),
                  templateUrl="https://example.com/teacher.wav", studentUrl="https://example.com/student.wav", instrumentId="dan_bau",
                  instrument=dict(name="Đàn bầu", fminHz=65, fmaxHz=1200, toleranceCents=70))
    values.update(changes)
    return EvaluationTask(**values)


BAU_CONFIG = InstrumentConfig(name="Đàn bầu", fminHz=65, fmaxHz=1047, toleranceCents=35)


def test_analyze_recording_uses_range_and_tolerance_from_config(monkeypatch):
    from app.evaluation import evaluator

    ranges = []
    monkeypatch.setattr(evaluator, "_extract_f0", lambda _path, fmin, fmax: ranges.append((fmin, fmax)) or np.array([220.0]))
    monkeypatch.setattr(evaluator, "_estimate_tempo", lambda _: 100.0)

    result = evaluator.analyze_recording(Path("a.wav"), "DBA103", BAU_CONFIG)

    assert ranges == [(65.0, 1047.0)]
    assert (result["instrumentId"], result["instrument"], result["toleranceCents"]) == ("DBA103", "Đàn bầu", 35.0)


def test_evaluate_pair_uses_config_name_and_code(monkeypatch):
    from app.evaluation import evaluator

    monkeypatch.setattr(evaluator, "_extract_f0", lambda *_: np.array([220.0, 220.0]))
    monkeypatch.setattr(evaluator, "_estimate_tempo", lambda _: 100.0)

    result = evaluator.evaluate_pair(Path("teacher.wav"), Path("student.wav"), "DBA103", BAU_CONFIG)

    assert result["instrumentId"] == "DBA103"
    assert result["instrument"] == "Đàn bầu"


def test_template_analysis_to_recording_rejects_other_instrument_code():
    from app.evaluation import evaluator

    template = {"instrument": {"code": "dan_nhi"}, "pitch": {"sampleRate": evaluator.SAMPLE_RATE, "hopLength": evaluator.HOP_LENGTH,
                "f0Hz": [220.0]}, "rhythm": {"tempoBpm": 100.0}}
    with pytest.raises(ValueError):
        evaluator.template_analysis_to_recording(template, "dan_bau", BAU_CONFIG)


@pytest.mark.asyncio
async def test_successful_evaluation_reports_each_practice_stage_in_order(tmp_path):
    service, _, _ = setup(tmp_path, 0.2)
    events = []

    class Callback:
        async def send(self, payload, job, status, **extra):
            events.append((job, status, extra.get("result")))
            return True

    service.callback_client = Callback()
    await service.process_direct_task(task(callbackUrl="https://backend.example/progress", attempt=2))

    assert [(job, status) for job, status, _ in events] == [
        ("quality_check", "started"), ("quality_check", "done"),
        ("audio_analysis", "started"), ("audio_analysis", "done"),
        ("comparison", "started"), ("comparison", "done"),
        ("feedback", "started"), ("feedback", "done"),
    ]
    assert events[-1][2]["overallScore"] == 88


@pytest.mark.asyncio
async def test_quality_rejection_reports_rejected_without_later_stages(tmp_path):
    service, _, _ = setup(tmp_path, 0)
    events = []

    class Callback:
        async def send(self, payload, job, status, **extra):
            events.append((job, status, extra.get("code")))
            return True

    service.callback_client = Callback()
    await service.process_direct_task(task(callbackUrl="https://backend.example/progress"))
    assert events[0:2] == [("quality_check", "started", None), ("quality_check", "rejected", "AUDIO_SILENT")]
    assert len(events) == 2


async def run_with_cached_template(tmp_path, cached_range):
    """Evaluate a task (range 65-1200) against a stored template analysed with `cached_range`; returns (downloads, cache_hits)."""
    from app.repositories.template_analysis_repository import TemplateAnalysisRepository
    from app.schemas.template_messages import TemplateAnalysisTask
    from app.services.artifact_storage_service import ArtifactStorageService

    service, sessions, _ = setup(tmp_path, 0.2)
    config = dict(name="Đàn bầu", fminHz=65, fmaxHz=1200, toleranceCents=70)
    analysis = TemplateAnalysisTask(messageId=uuid4(), analysisId=uuid4(), sheetMusicId=uuid4(), mediaFileId=uuid4(),
                                    mediaUrl="https://example.com/teacher.wav", instrumentCode="dan_bau", requestedAt="2026-10-23T00:00:00Z",
                                    instrument=config)
    storage = ArtifactStorageService(tmp_path / "artifacts")
    artifact = storage.write(analysis, {
        "analysisId": str(analysis.analysisId),
        "instrument": {"code": "dan_bau", "fminHz": cached_range[0], "fmaxHz": cached_range[1]},
        "pitch": {"sampleRate": 22050, "hopLength": 512, "f0Hz": [220.0, None]},
        "rhythm": {"tempoBpm": 100.0},
    })
    template_repository = TemplateAnalysisRepository()
    with sessions() as session:
        record = template_repository.create_if_missing(session, analysis, {"name": "Đàn bầu"}, {})
        template_repository.mark_completed(session, record, "source", artifact, {"verdict": "PASS"})

    downloads, cache_hits = [], []
    async def download(url, directory):
        downloads.append(str(url))
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / ("student.wav" if "student" in str(url) else "teacher.wav")
        sf.write(path, 0.2 * np.sin(2 * np.pi * 220 * np.arange(8000) / 8000), 8000)
        return path
    async def analyze(path, instrument, config=None):
        return {"recording": "student", "instrumentId": instrument}
    async def cached(template, instrument, config=None):
        cache_hits.append(template["analysisId"])
        return {"recording": "template", "instrumentId": instrument}
    async def compare(template, student):
        return {"overallScore": 91, "pitchAccuracyPercent": 92, "rhythmAccuracyPercent": 90}

    service.template_storage = storage
    service.audio_service = SimpleNamespace(download=download)
    service.rhythm_analysis_service = SimpleNamespace(analyze=analyze, analyze_template=cached, compare=compare)
    payload = task(templateAnalysisId=analysis.analysisId, instrument=config)
    await service.process_direct_task(payload)
    with sessions() as session:
        assert service.repository.get(session, payload.evaluationId).result_json["overallScore"] == 91
    assert cache_hits in ([], [str(analysis.analysisId)])
    return downloads, cache_hits


@pytest.mark.asyncio
async def test_completed_template_artifact_skips_template_download_and_uses_cached_features(tmp_path):
    downloads, cache_hits = await run_with_cached_template(tmp_path, (65, 1200))
    assert downloads == ["https://example.com/student.wav"]
    assert len(cache_hits) == 1


@pytest.mark.asyncio
async def test_template_cache_is_ignored_when_pitch_range_differs(tmp_path):
    downloads, cache_hits = await run_with_cached_template(tmp_path, (65, 1047))
    assert cache_hits == []
    assert downloads == ["https://example.com/student.wav", "https://example.com/teacher.wav"]


@pytest.mark.asyncio
async def test_missing_template_artifact_falls_back_to_the_legacy_pair_evaluation(tmp_path):
    service, sessions, _ = setup(tmp_path, 0.2)
    downloads = []
    async def download(url, directory):
        downloads.append(str(url))
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / ("student.wav" if "student" in str(url) else "teacher.wav")
        sf.write(path, 0.2 * np.sin(2 * np.pi * 220 * np.arange(8000) / 8000), 8000)
        return path
    async def analyze(path, instrument, config=None):
        return {"path": path, "instrumentId": instrument}
    async def compare(teacher, student):
        return {"overallScore": 80, "pitchAccuracyPercent": 80, "rhythmAccuracyPercent": 80}
    service.audio_service = SimpleNamespace(download=download)
    service.rhythm_analysis_service = SimpleNamespace(analyze=analyze, compare=compare, analyze_template=lambda *args: pytest.fail("unexpected cache hit"))
    await service.process_direct_task(task(templateAnalysisId=uuid4()))
    assert downloads == ["https://example.com/student.wav", "https://example.com/teacher.wav"]


@pytest.mark.asyncio
@pytest.mark.parametrize("amplitude,verdict,status", [(0, "REJECT", "failed"), (0.001, "WARN", "completed"), (0.2, "PASS", "completed")])
async def test_student_quality_is_persisted_and_only_reject_prevents_scoring(tmp_path, amplitude, verdict, status):
    service, sessions, calls = setup(tmp_path, amplitude)
    payload = task()
    await service.process_direct_task(payload)
    with sessions() as session:
        saved = service.repository.get(session, payload.evaluationId)
        assert saved.status == status
        assert saved.student_quality_report["verdict"] == verdict
        if status == "failed":
            assert saved.error_code == "AUDIO_SILENT"
            assert saved.result_json is None
            assert calls == []
        else:
            assert saved.result_json["overallScore"] == 88
            assert saved.result_json["studentAudioQuality"]["verdict"] == verdict
            assert calls == ["analyze", "analyze", "compare", "feedback"]


@pytest.mark.asyncio
async def test_template_queue_evaluation_queue_and_http_share_one_execution_gate(tmp_path):
    from app.schemas.template_messages import TemplateAnalysisTask
    from app.services.template_analysis_service import TemplateAnalysisService
    service, sessions, _ = setup(tmp_path, 0.2)
    template = TemplateAnalysisService(session_factory=sessions, settings=Settings(_env_file=None, temp_directory=str(tmp_path / "work"), artifact_root=str(tmp_path / "artifacts")))
    current = maximum = 0
    original_download = service.audio_service.download
    async def measured_download(url, directory):
        nonlocal current, maximum
        current += 1
        maximum = max(maximum, current)
        await asyncio.sleep(0.02)
        result = await original_download(url, directory)
        current -= 1
        return result
    service.audio_service = template.audio_service = SimpleNamespace(download=measured_download)
    async def extract(*args):
        return {"pitch": {"f0Hz": [220], "voicedFlags": [True]}, "rhythm": {"tempoBpm": 100}}
    template.feature_service = SimpleNamespace(extract=extract)
    main = TemplateAnalysisTask(messageId=uuid4(), analysisId=uuid4(), sheetMusicId=uuid4(), mediaFileId=uuid4(),
                                mediaUrl="https://example.com/template.wav", instrumentCode="dan_bau", requestedAt="2026-10-23T00:00:00Z",
                                    instrument=dict(name="Đàn bầu", fminHz=65, fmaxHz=1200, toleranceCents=70))
    await asyncio.gather(service.process_task(task()), service.process_direct_task(task()), template.process_task(main))
    assert maximum == 1


@pytest.mark.asyncio
async def test_cancelled_job_keeps_gate_until_running_operation_finishes():
    gate = JobExecutionService()
    started, release, second_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def first():
        started.set()
        await release.wait()
    async def second():
        second_started.set()
    running = asyncio.create_task(gate.run(first))
    await started.wait()
    running.cancel()
    following = asyncio.create_task(gate.run(second))
    await asyncio.sleep(0)
    assert not second_started.is_set()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await running
    await following
    assert second_started.is_set()


@pytest.mark.asyncio
async def test_http_student_rejection_preserves_null_result_error_contract(tmp_path, monkeypatch):
    import httpx
    from app import api
    from app.database import get_session
    service, sessions, calls = setup(tmp_path, 0)
    monkeypatch.setattr(api, "EvaluationService", lambda *args: service)
    monkeypatch.setattr(api, "get_settings", lambda: SimpleNamespace(internal_api_key="secret", temp_directory=str(tmp_path)))
    app = api.create_app()
    def dependency():
        with sessions() as session:
            yield session
    app.dependency_overrides[get_session] = dependency
    payload = task()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer secret"}) as client:
        created = await client.post("/api/v1/evaluations", json=payload.model_dump(mode="json"))
        assert created.status_code == 202
        response = await client.get(f"/api/v1/evaluations/{payload.evaluationId}")
        assert response.status_code == 200
        assert response.json()["status"] == "failed"
        assert response.json()["result"] is None
        assert response.json()["error"]["code"] == "AUDIO_SILENT"
    assert calls == []


@pytest.mark.asyncio
async def test_actual_http_request_and_both_queue_handlers_are_serialized(tmp_path, monkeypatch):
    import httpx
    from app import api
    from app.database import get_session
    from app.messaging import rabbitmq_worker as worker_module
    from app.schemas.template_messages import TemplateAnalysisTask
    from test_template_worker import Message
    service, sessions, _ = setup(tmp_path, 0.2)
    settings = Settings(_env_file=None, internal_api_key="secret", temp_directory=str(tmp_path / "work"), artifact_root=str(tmp_path / "artifacts"))
    monkeypatch.setattr(worker_module, "get_settings", lambda: settings)
    monkeypatch.setattr(worker_module, "SessionFactory", sessions)
    monkeypatch.setattr(api, "get_settings", lambda: settings)
    monkeypatch.setattr(api, "EvaluationService", lambda *args: service)
    worker = worker_module.RabbitMqWorker()
    worker.evaluation_service = service
    current = maximum = 0
    original = service.audio_service.download
    async def download(url, directory):
        nonlocal current, maximum
        current += 1
        maximum = max(maximum, current)
        await asyncio.sleep(0.02)
        path = await original(url, directory)
        current -= 1
        return path
    service.audio_service = worker.template_service.audio_service = SimpleNamespace(download=download)
    async def extract(*args):
        return {"pitch": {"f0Hz": [220], "voicedFlags": [True]}, "rhythm": {"tempoBpm": 100}}
    worker.template_service.feature_service = SimpleNamespace(extract=extract)
    app = api.create_app()
    def dependency():
        with sessions() as session:
            yield session
    app.dependency_overrides[get_session] = dependency
    queue_task, http_task = task(), task()
    main = TemplateAnalysisTask(messageId=uuid4(), analysisId=uuid4(), sheetMusicId=uuid4(), mediaFileId=uuid4(),
                                mediaUrl="https://example.com/template.wav", instrumentCode="dan_bau", requestedAt="2026-10-23T00:00:00Z",
                                    instrument=dict(name="Đàn bầu", fminHz=65, fmaxHz=1200, toleranceCents=70))
    evaluation_message, template_message = Message(queue_task), Message(main)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer secret"}) as client:
        _, _, response = await asyncio.gather(worker.handle_message(evaluation_message), worker.handle_template_message(template_message),
                                             client.post("/api/v1/evaluations", json=http_task.model_dump(mode="json")))
        assert response.status_code == 202
    assert maximum == 1 and evaluation_message.acked == 1 and template_message.acked == 1
    with sessions() as session:
        assert service.repository.get(session, queue_task.evaluationId).status == "completed"
        assert service.repository.get(session, http_task.evaluationId).status == "completed"
        assert worker.template_service.repository.get(session, str(main.analysisId)).status == "completed"
