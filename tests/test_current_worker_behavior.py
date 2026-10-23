"""Characterize worker behavior before the MVC refactor.

Current repeated-ID behavior: repository creation retains the original task metadata.
RabbitMQ skips completed records but reprocesses queued, failed, and processing
records, using URLs and instrument from the newly received task. HTTP POST returns
the existing record and schedules work for queued/failed only. There is no atomic
claim for simultaneous deliveries of the same evaluationId.
"""

from types import SimpleNamespace

import pytest
import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.services.audio_quality_service import AudioQualityService
from app.database import Base
from app import api
from app.database import get_session
from app.messaging import rabbitmq_worker as module
from app.repositories.evaluation_repository import EvaluationRepository
from app.schemas.messages import EvaluationTask


PASS_QUALITY = {"verdict": "PASS"}


def analysis_stub(result):
    async def analyze(path, instrument):
        return {"path": path, "instrumentId": instrument}

    async def compare(teacher, student):
        return result

    return SimpleNamespace(analyze=analyze, compare=compare)


def task(suffix="one"):
    return EvaluationTask(
        messageId=f"message-{suffix}", evaluationId="evaluation-1",
        templateUrl=f"https://example.com/{suffix}-teacher.wav",
        studentUrl=f"https://example.com/{suffix}-student.wav", instrumentId="dan_bau",
    )


@pytest.fixture
def setup(monkeypatch, tmp_path):
    engine = create_engine("sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(module, "SessionFactory", sessions)
    monkeypatch.setattr(module, "get_settings", lambda: Settings(
        _env_file=None, temp_directory=str(tmp_path), max_retries=2, rhythm_task_queue="tasks", artifact_root=str(tmp_path / "artifacts")))
    async def quality(self, path, policy=None):
        return dict(PASS_QUALITY)
    monkeypatch.setattr(AudioQualityService, "inspect", quality)
    yield module.RabbitMqWorker(), sessions
    engine.dispose()


def record(sessions):
    with sessions() as session:
        return EvaluationRepository().get(session, "evaluation-1")


@pytest.mark.asyncio
async def test_success_pipeline_and_persistence(setup, monkeypatch):
    worker, sessions = setup
    downloads = []
    analyses = []

    async def download(url, directory):
        downloads.append((url, directory))
        return directory / f"{len(downloads)}.audio"

    async def analyze(path, instrument):
        analyses.append((path, instrument))
        return {"path": path, "instrumentId": instrument}

    async def compare(teacher, student):
        assert teacher["path"] == downloads[1][1] / "2.audio"
        assert student["path"] == downloads[0][1] / "1.audio"
        return {"overallScore": 87}

    async def feedback(result):
        assert result == {"overallScore": 87}
        return "Good practice"

    worker.evaluation_service.audio_service = SimpleNamespace(download=download)
    worker.evaluation_service.rhythm_analysis_service = SimpleNamespace(analyze=analyze, compare=compare)
    worker.evaluation_service.feedback_service = SimpleNamespace(generate=feedback)
    await worker.evaluation_service.process_task(task())

    saved = record(sessions)
    assert saved.status == "completed"
    assert saved.result_json == {"overallScore": 87, "aiFeedback": "Good practice", "studentAudioQuality": PASS_QUALITY}
    assert saved.started_at is not None and saved.completed_at is not None
    assert [url for url, _ in downloads] == [str(task().studentUrl), str(task().templateUrl)]
    assert analyses == [(downloads[0][1] / "1.audio", "dan_bau"), (downloads[1][1] / "2.audio", "dan_bau")]
    assert downloads[0][1] == downloads[1][1]
    assert not downloads[0][1].exists()


@pytest.mark.asyncio
async def test_direct_download_failure_is_persisted(setup, monkeypatch):
    worker, sessions = setup
    calls = []

    async def fail(url, directory):
        calls.append(url)
        raise module.AudioDownloadError("AUDIO_DOWNLOAD_FAILED", "download failed", retryable=True)

    async def unexpected_evaluation(*args):
        pytest.fail("evaluation must not run")

    worker.evaluation_service.audio_service = SimpleNamespace(download=fail)
    worker.evaluation_service.rhythm_analysis_service = analysis_stub({"overallScore": 0})
    await worker.evaluation_service.process_direct_task(task())

    saved = record(sessions)
    assert (saved.status, saved.error_code, saved.error_message) == (
        "failed", "AUDIO_DOWNLOAD_FAILED", "download failed")
    assert saved.result_json is None
    assert calls == [str(task().studentUrl)]


class FakeMessage:
    def __init__(self, payload, headers=None):
        self.body = payload.model_dump_json().encode()
        self.headers = headers or {}
        self.acked = 0

    async def ack(self):
        self.acked += 1


@pytest.mark.asyncio
async def test_rabbitmq_retry_then_failure_at_limit(setup, monkeypatch):
    worker, sessions = setup
    published = []

    async def fail(*args):
        raise module.AudioDownloadError("AUDIO_DOWNLOAD_FAILED", "temporary", retryable=True)

    class Connection:
        async def channel(self, **kwargs):
            async def publish(message, *, routing_key, mandatory=True):
                published.append((message, routing_key))

            async def close():
                pass

            return SimpleNamespace(default_exchange=SimpleNamespace(publish=publish), close=close)

    worker.evaluation_service.audio_service = SimpleNamespace(download=fail)
    worker.connection = Connection()
    first = FakeMessage(task())
    await worker.handle_message(first)
    assert first.acked == 1
    assert len(published) == 1
    assert published[0][0].body == first.body
    assert published[0][0].headers == {"x-retry-count": 1}
    assert published[0][1] == "tasks"
    assert record(sessions).status == "processing"

    exhausted = FakeMessage(task(), {"x-retry-count": 2})
    await worker.handle_message(exhausted)
    assert exhausted.acked == 1
    assert len(published) == 1
    assert (record(sessions).status, record(sessions).error_code) == ("failed", "AUDIO_DOWNLOAD_FAILED")


@pytest.mark.asyncio
async def test_repeated_id_status_behavior_and_original_metadata(setup, monkeypatch):
    worker, sessions = setup
    downloaded = []

    async def download(url, directory):
        downloaded.append(url)
        return directory / "audio"

    async def feedback(result):
        return "feedback"

    async def evaluate(*args):
        return {"overallScore": 88}

    worker.evaluation_service.audio_service = SimpleNamespace(download=download)
    worker.evaluation_service.rhythm_analysis_service = analysis_stub({"overallScore": 88})
    worker.evaluation_service.feedback_service = SimpleNamespace(generate=feedback)
    with sessions() as session:
        saved = worker.repository.create_if_missing(session, task())
        worker.repository.mark_failed(session, saved, "OLD_ERROR", "old failure")

    await worker.evaluation_service.process_task(task("two"))
    assert downloaded == [str(task("two").studentUrl), str(task("two").templateUrl)]
    saved = record(sessions)
    assert saved.status == "completed"
    assert saved.template_url == str(task().templateUrl)
    assert saved.student_url == str(task().studentUrl)
    assert saved.error_code is None

    await worker.evaluation_service.process_task(task())
    assert len(downloaded) == 2  # completed ID is skipped

    with sessions() as session:
        worker.repository.mark_processing(session, worker.repository.get(session, "evaluation-1"))
    await worker.evaluation_service.process_task(task("two"))
    assert len(downloaded) == 4  # processing ID is not skipped
    assert record(sessions).status == "completed"


@pytest.mark.asyncio
async def test_http_repeated_id_scheduling_depends_on_status(setup, monkeypatch):
    _, sessions = setup
    scheduled = []

    class FakeService:
        def __init__(self, repository, session_factory, temp_directory):
            pass

        async def process_direct_task(self, payload):
            scheduled.append(payload.evaluationId)

    monkeypatch.setattr(api, "EvaluationService", FakeService)
    monkeypatch.setattr(api, "get_settings", lambda: SimpleNamespace(internal_api_key="secret", temp_directory="unused"))
    app = api.create_app()

    def session_dependency():
        with sessions() as session:
            yield session

    app.dependency_overrides[get_session] = session_dependency
    transport = httpx.ASGITransport(app=app)
    headers = {"Authorization": "Bearer secret"}
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post("/api/v1/evaluations", json=task().model_dump(mode="json"), headers=headers)
        assert first.status_code == 202 and first.json()["status"] == "queued"
        assert scheduled == ["evaluation-1"]

        with sessions() as session:
            saved = EvaluationRepository().get(session, "evaluation-1")
            EvaluationRepository().mark_processing(session, saved)
        processing = await client.post("/api/v1/evaluations", json=task("two").model_dump(mode="json"), headers=headers)
        assert processing.json()["status"] == "processing"
        assert scheduled == ["evaluation-1"]

        with sessions() as session:
            saved = EvaluationRepository().get(session, "evaluation-1")
            EvaluationRepository().mark_completed(session, saved, {"overallScore": 80})
        completed = await client.post("/api/v1/evaluations", json=task("two").model_dump(mode="json"), headers=headers)
        assert completed.json()["status"] == "completed"
        assert scheduled == ["evaluation-1"]

        with sessions() as session:
            saved = EvaluationRepository().get(session, "evaluation-1")
            EvaluationRepository().mark_failed(session, saved, "ERR", "failure")
        failed = await client.post("/api/v1/evaluations", json=task("two").model_dump(mode="json"), headers=headers)
        assert failed.json()["status"] == "failed"
        assert scheduled == ["evaluation-1", "evaluation-1"]


@pytest.mark.asyncio
async def test_processing_closes_sessions_during_external_work_and_isolates_temp_dirs(setup):
    worker, sessions = setup
    opened = []
    directories = []

    class TrackedSession:
        def __init__(self):
            self.inner = sessions()
            self.closed = False
            opened.append(self)

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def close(self):
            self.closed = True
            self.inner.close()

    worker.evaluation_service.session_factory = TrackedSession

    async def download(url, directory):
        assert all(session.closed for session in opened)
        directory.mkdir(parents=True, exist_ok=True)
        directories.append(directory)
        return directory / "audio"

    async def evaluate(*args):
        assert all(session.closed for session in opened)
        return {"overallScore": 90}

    async def feedback(result):
        assert all(session.closed for session in opened)
        return "done"

    worker.evaluation_service.audio_service = SimpleNamespace(download=download)
    worker.evaluation_service.rhythm_analysis_service = analysis_stub({"overallScore": 90})
    worker.evaluation_service.feedback_service = SimpleNamespace(generate=feedback)

    await worker.evaluation_service.process_task(task())
    with sessions() as session:
        saved = EvaluationRepository().get(session, "evaluation-1")
        EvaluationRepository().mark_processing(session, saved)
    await worker.evaluation_service.process_task(task())

    assert len(directories) == 4
    assert directories[0] == directories[1]
    assert directories[2] == directories[3]
    assert directories[0] != directories[2]
    assert all(not directory.exists() for directory in set(directories))
    assert all(session.closed for session in opened)
    assert record(sessions).result_json == {"overallScore": 90, "aiFeedback": "done", "studentAudioQuality": PASS_QUALITY}


@pytest.mark.asyncio
async def test_rabbitmq_success_acks_after_result_is_stored(setup):
    worker, sessions = setup

    async def download(url, directory):
        return directory / "audio"

    async def evaluate(*args):
        return {"overallScore": 91}

    async def feedback(result):
        return "done"

    worker.evaluation_service.audio_service = SimpleNamespace(download=download)
    worker.evaluation_service.rhythm_analysis_service = analysis_stub({"overallScore": 91})
    worker.evaluation_service.feedback_service = SimpleNamespace(generate=feedback)
    message = FakeMessage(task())

    async def ack():
        assert record(sessions).status == "completed"
        assert record(sessions).result_json == {"overallScore": 91, "aiFeedback": "done", "studentAudioQuality": PASS_QUALITY}
        message.acked += 1

    message.ack = ack
    await worker.handle_message(message)
    assert message.acked == 1


@pytest.mark.asyncio
async def test_rabbitmq_invalid_payload_rejects_without_creating_record(setup):
    worker, sessions = setup
    rejected = []

    async def reject(*, requeue):
        rejected.append(requeue)

    await worker.handle_message(SimpleNamespace(body=b"not-json", reject=reject))
    assert rejected == [False]
    assert record(sessions) is None


@pytest.mark.asyncio
async def test_http_create_process_and_get_result_with_actual_service(setup, monkeypatch):
    worker, sessions = setup

    async def download(url, directory):
        return directory / "audio"

    async def evaluate(*args):
        return {"overallScore": 92}

    async def feedback(result):
        return "finished"

    worker.evaluation_service.audio_service = SimpleNamespace(download=download)
    worker.evaluation_service.rhythm_analysis_service = analysis_stub({"overallScore": 92})
    worker.evaluation_service.feedback_service = SimpleNamespace(generate=feedback)
    monkeypatch.setattr(api, "EvaluationService", lambda *args: worker.evaluation_service)
    monkeypatch.setattr(api, "get_settings", lambda: SimpleNamespace(internal_api_key="secret", temp_directory="unused"))
    app = api.create_app()

    def session_dependency():
        with sessions() as session:
            yield session

    app.dependency_overrides[get_session] = session_dependency
    transport = httpx.ASGITransport(app=app)
    headers = {"Authorization": "Bearer secret"}
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post("/api/v1/evaluations", json=task().model_dump(mode="json"), headers=headers)
        assert created.status_code == 202
        assert created.json()["evaluationId"] == "evaluation-1"
        fetched = await client.get("/api/v1/evaluations/evaluation-1", headers=headers)
        assert fetched.status_code == 200
        assert fetched.json() == {
            "evaluationId": "evaluation-1", "status": "completed",
            "result": {"overallScore": 92, "aiFeedback": "finished", "studentAudioQuality": PASS_QUALITY}, "error": None,
        }
