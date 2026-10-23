"""Job-by-job progress: stage order, callbacks, external JOB-01, retries and timeouts."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import api
from app.database import Base, get_session
from app.repositories.evaluation_repository import EvaluationRepository
from app.schemas.messages import EvaluationTask
from app.services import evaluation_service as service_module
from app.services.evaluation_service import EvaluationService

HEADERS = {"Authorization": "Bearer secret"}


def practice_task():
    return EvaluationTask(
        messageId="message-1", evaluationId="evaluation-1",
        templateUrl="https://example.com/teacher.wav", studentUrl="https://example.com/student.wav",
        instrumentId="dan_bau", callbackUrl="https://api.example.com/progress",
    )


class FakeCallbacks:
    def __init__(self):
        self.sent = []

    async def notify(self, url, payload):
        self.sent.append((url, payload))


@pytest.fixture
def env(monkeypatch, tmp_path):
    engine = create_engine("sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    settings = SimpleNamespace(quality_check_mode="bypass", job_timeout_seconds=60.0)
    monkeypatch.setattr(service_module, "get_settings", lambda: settings)
    callbacks = FakeCallbacks()
    calls = {"evaluate": 0, "feedback": 0}

    async def download(url, directory):
        return directory / url.rsplit("/", 1)[-1]

    async def evaluate(teacher, student, instrument):
        calls["evaluate"] += 1
        return {"overallScore": 80}

    async def feedback(scores):
        calls["feedback"] += 1
        return "Tốt"

    service = EvaluationService(
        EvaluationRepository(), sessions, str(tmp_path),
        audio_service=SimpleNamespace(download=download),
        rhythm_analysis_service=SimpleNamespace(evaluate=evaluate),
        feedback_service=SimpleNamespace(generate=feedback),
        callback_service=callbacks,
    )
    yield SimpleNamespace(service=service, sessions=sessions, settings=settings, callbacks=callbacks, calls=calls)
    engine.dispose()


def saved(env):
    with env.sessions() as session:
        return EvaluationRepository().get(session, "evaluation-1")


def events(env):
    return [(payload.job, payload.status) for _, payload in env.callbacks.sent]


@pytest.mark.asyncio
async def test_jobs_run_in_order_and_each_start_is_reported(env):
    await env.service.process_task(practice_task())

    assert events(env) == [
        ("quality_check", "started"), ("quality_check", "done"),
        ("audio_analysis", "started"), ("audio_analysis", "done"),
        ("comparison", "started"), ("comparison", "done"),
        ("feedback", "started"), ("feedback", "done"),
    ]
    assert all(url == "https://api.example.com/progress" for url, _ in env.callbacks.sent)
    assert env.callbacks.sent[-1][1].result == {"overallScore": 80, "aiFeedback": "Tốt"}
    assert all(payload.result is None for _, payload in env.callbacks.sent[:-1])
    record = saved(env)
    assert (record.status, record.stage) == ("completed", "feedback")
    assert record.job_state == {"scores": {"overallScore": 80}}
    assert env.calls == {"evaluate": 1, "feedback": 1}


@pytest.mark.asyncio
async def test_failed_job_is_reported_with_its_stage_and_not_retried_automatically(env):
    async def broken_feedback(scores):
        raise ValueError("Gemini lỗi")

    env.service.feedback_service = SimpleNamespace(generate=broken_feedback)
    await env.service.process_direct_task(practice_task())

    record = saved(env)
    assert (record.status, record.stage, record.error_code) == ("failed", "feedback", "EVALUATION_FAILED")
    assert events(env)[-1] == ("feedback", "failed")
    assert env.callbacks.sent[-1][1].reason == "Gemini lỗi"


@pytest.mark.asyncio
async def test_job_over_the_time_limit_fails_with_job_timeout(env):
    env.settings.job_timeout_seconds = 0.01

    async def slow_evaluate(*args):
        await asyncio.sleep(1)

    env.service.rhythm_analysis_service = SimpleNamespace(evaluate=slow_evaluate)
    await env.service.process_direct_task(practice_task())

    record = saved(env)
    assert (record.status, record.stage, record.error_code) == ("failed", "audio_analysis", "JOB_TIMEOUT")


@pytest.mark.asyncio
async def test_retry_from_feedback_reuses_saved_scores(env):
    async def broken_feedback(scores):
        raise ValueError("Gemini lỗi")

    env.service.feedback_service = SimpleNamespace(generate=broken_feedback)
    await env.service.process_direct_task(practice_task())
    assert env.calls["evaluate"] == 1

    async def feedback(scores):
        assert scores == {"overallScore": 80}
        return "Lần hai"

    env.service.feedback_service = SimpleNamespace(generate=feedback)
    with env.sessions() as session:
        repository = EvaluationRepository()
        record = repository.get(session, "evaluation-1")
        repository.start_attempt(session, record, 2)
        task = repository.to_task(record)
    await env.service.run_guarded(task, "feedback", 2)

    assert env.calls["evaluate"] == 1
    record = saved(env)
    assert (record.status, record.attempt) == ("completed", 2)
    assert record.result_json == {"overallScore": 80, "aiFeedback": "Lần hai"}
    assert [(p.job, p.status, p.attempt) for _, p in env.callbacks.sent[-2:]] == [
        ("feedback", "started", 2), ("feedback", "done", 2)]


@pytest.mark.asyncio
async def test_stale_attempt_report_is_ignored(env):
    await env.service.process_task(practice_task())
    with env.sessions() as session:
        repository = EvaluationRepository()
        repository.start_attempt(session, repository.get(session, "evaluation-1"), 2)

    from app.schemas.messages import JobReport
    outcome = await env.service.progress.report("evaluation-1", "audio_analysis", JobReport(status="started", attempt=1))
    assert outcome is None
    assert saved(env).stage == "feedback"


@pytest.mark.asyncio
async def test_callback_failure_does_not_fail_the_job(env):
    class BrokenCallbacks:
        async def notify(self, url, payload):
            return None  # CallbackService swallows delivery errors after its retries

    env.service.callback_service = BrokenCallbacks()
    await env.service.process_task(practice_task())
    assert saved(env).status == "completed"


def client_for(env, monkeypatch):
    monkeypatch.setattr(api, "SessionFactory", env.sessions)
    monkeypatch.setattr(api, "get_settings", lambda: SimpleNamespace(internal_api_key="secret", temp_directory="unused"))
    created = []

    def build_service(repository, session_factory, temp_directory):
        created.append(env.service)
        return env.service

    monkeypatch.setattr(api, "EvaluationService", build_service)
    app = api.create_app()

    def session_dependency():
        with env.sessions() as session:
            yield session

    app.dependency_overrides[get_session] = session_dependency
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_external_quality_check_waits_then_continues_when_reported_done(env, monkeypatch):
    env.settings.quality_check_mode = "external"
    await env.service.process_task(practice_task())
    assert (saved(env).status, saved(env).stage) == ("waiting", "quality_check")
    assert env.calls["evaluate"] == 0

    async with client_for(env, monkeypatch) as client:
        response = await client.post(
            "/api/v1/evaluations/evaluation-1/jobs/quality_check", json={"status": "done", "attempt": 1}, headers=HEADERS)
    assert response.status_code == 200
    assert saved(env).status == "completed"
    assert env.calls == {"evaluate": 1, "feedback": 1}
    assert ("audio_analysis", "started") in events(env)


@pytest.mark.asyncio
async def test_external_quality_check_can_reject_the_recording(env, monkeypatch):
    env.settings.quality_check_mode = "external"
    await env.service.process_task(practice_task())

    async with client_for(env, monkeypatch) as client:
        response = await client.post(
            "/api/v1/evaluations/evaluation-1/jobs/quality_check",
            json={"status": "rejected", "attempt": 1, "code": "LOW_VOLUME", "reason": "Âm lượng quá nhỏ"}, headers=HEADERS)
        stale = await client.post(
            "/api/v1/evaluations/evaluation-1/jobs/quality_check", json={"status": "done", "attempt": 1}, headers=HEADERS)
        wrong_job = await client.post(
            "/api/v1/evaluations/evaluation-1/jobs/comparison", json={"status": "rejected", "attempt": 1}, headers=HEADERS)

    assert response.status_code == 200 and response.json()["status"] == "rejected"
    assert response.json()["error"] == {"code": "LOW_VOLUME", "message": "Âm lượng quá nhỏ"}
    assert stale.status_code == 409
    assert wrong_job.status_code == 422
    assert env.calls["evaluate"] == 0
    assert events(env)[-1] == ("quality_check", "rejected")


@pytest.mark.asyncio
async def test_retry_route_starts_a_new_attempt_from_the_failed_job(env, monkeypatch):
    async def broken_feedback(scores):
        raise ValueError("Gemini lỗi")

    env.service.feedback_service = SimpleNamespace(generate=broken_feedback)
    await env.service.process_direct_task(practice_task())

    async def feedback(scores):
        return "Lần hai"

    env.service.feedback_service = SimpleNamespace(generate=feedback)
    async with client_for(env, monkeypatch) as client:
        same_attempt = await client.post(
            "/api/v1/evaluations/evaluation-1/retry", json={"fromJob": "feedback", "attempt": 1}, headers=HEADERS)
        retried = await client.post(
            "/api/v1/evaluations/evaluation-1/retry", json={"fromJob": "feedback", "attempt": 2}, headers=HEADERS)
        unauthorized = await client.post(
            "/api/v1/evaluations/evaluation-1/retry", json={"fromJob": "feedback", "attempt": 3})

    assert same_attempt.status_code == 422  # attempt must be >= 2
    assert retried.status_code == 202
    assert unauthorized.status_code == 401
    record = saved(env)
    assert (record.status, record.attempt, record.result_json["aiFeedback"]) == ("completed", 2, "Lần hai")
    assert env.calls["evaluate"] == 1
