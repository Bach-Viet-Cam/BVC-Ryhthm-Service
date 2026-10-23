from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from app.messaging import rabbitmq_worker as module
from test_template_analysis import setup as template_setup, task


class Message:
    def __init__(self, payload=None, headers=None, body=None):
        self.body = body if body is not None else payload.model_dump_json().encode()
        self.headers = headers or {}
        self.acked = 0
        self.rejected = []
        self.processed = False

    async def ack(self):
        self.acked += 1
        self.processed = True

    async def reject(self, *, requeue):
        self.rejected.append(requeue)
        self.processed = True


def worker(tmp_path, monkeypatch):
    service, sessions = template_setup(tmp_path)
    monkeypatch.setattr(module, "get_settings", lambda: service.settings)
    monkeypatch.setattr(module, "SessionFactory", sessions)
    instance = module.RabbitMqWorker()
    instance.template_service = service
    return instance, sessions


class Broker:
    def __init__(self, confirmation=True):
        self.confirmation = confirmation
        self.published = []
        self.closed = False

    async def channel(self, **kwargs):
        async def publish(message, *, routing_key, mandatory=True):
            self.published.append((message, routing_key))
            return self.confirmation
        async def close():
            self.closed = True
        return SimpleNamespace(default_exchange=SimpleNamespace(publish=publish), close=close)


@pytest.mark.asyncio
async def test_template_ack_happens_only_after_database_and_json_are_ready(tmp_path, monkeypatch):
    instance, sessions = worker(tmp_path, monkeypatch)
    payload = task()
    message = Message(payload)
    async def ack():
        with sessions() as session:
            saved = instance.template_service.repository.get(session, str(payload.analysisId))
            assert saved.status == "completed"
            instance.template_service.storage.read(saved.artifact_key, saved.artifact_sha256)
        message.acked += 1
    message.ack = ack
    await instance.handle_template_message(message)
    assert message.acked == 1


@pytest.mark.asyncio
async def test_bad_template_payload_goes_to_dlq(tmp_path, monkeypatch):
    instance, _ = worker(tmp_path, monkeypatch)
    message = Message(body=b"not-json")
    await instance.handle_template_message(message)
    assert message.acked == 0 and message.rejected == [False]


@pytest.mark.asyncio
async def test_unsupported_instrument_is_saved_as_permanent_failure(tmp_path, monkeypatch):
    instance, sessions = worker(tmp_path, monkeypatch)
    payload = task(instrumentCode="unknown")
    message = Message(payload)
    await instance.handle_template_message(message)
    with sessions() as session:
        saved = instance.template_service.repository.get(session, str(payload.analysisId))
        assert saved.status == "failed" and saved.error_code == "UNSUPPORTED_INSTRUMENT"
    assert message.acked == 1


@pytest.mark.asyncio
async def test_template_conflict_goes_to_dlq_and_preserves_first_result(tmp_path, monkeypatch):
    instance, sessions = worker(tmp_path, monkeypatch)
    payload = task()
    await instance.handle_template_message(Message(payload))
    message = Message(task(analysisId=str(payload.analysisId)))
    await instance.handle_template_message(message)
    assert message.acked == 0 and message.rejected == [False]
    with sessions() as session:
        assert instance.template_service.repository.get(session, str(payload.analysisId)).status == "completed"


@pytest.mark.asyncio
async def test_template_download_retry_then_exhaustion(tmp_path, monkeypatch):
    instance, sessions = worker(tmp_path, monkeypatch)
    async def fail(*args):
        raise module.AudioDownloadError("AUDIO_DOWNLOAD_FAILED", "Tải file tạm thời thất bại.", retryable=True)
    instance.template_service.audio_service = SimpleNamespace(download=fail)
    instance.connection = Broker()
    payload = task()
    first = Message(payload)
    await instance.handle_template_message(first)
    assert first.acked == 1
    retry, queue = instance.connection.published[0]
    assert queue == instance.settings.template_task_queue and retry.headers["x-retry-count"] == 1
    last = Message(payload, {"x-retry-count": instance.settings.max_retries})
    await instance.handle_template_message(last)
    with sessions() as session:
        saved = instance.template_service.repository.get(session, str(payload.analysisId))
        assert saved.status == "failed" and saved.error_code == "AUDIO_DOWNLOAD_FAILED"
    assert last.acked == 1 and len(instance.connection.published) == 1


@pytest.mark.asyncio
async def test_unconfirmed_retry_is_requeued_without_ack(tmp_path, monkeypatch):
    instance, _ = worker(tmp_path, monkeypatch)
    async def fail(*args):
        raise module.AudioDownloadError("AUDIO_DOWNLOAD_FAILED", "Tải file thất bại.", retryable=True)
    instance.template_service.audio_service = SimpleNamespace(download=fail)
    instance.connection = Broker(confirmation=False)
    message = Message(task())
    await instance.handle_template_message(message)
    assert message.acked == 0 and message.rejected == [True]
    assert instance.connection.closed


@pytest.mark.asyncio
async def test_database_failure_never_acks_success(tmp_path, monkeypatch):
    instance, _ = worker(tmp_path, monkeypatch)
    def fail(*args):
        raise RuntimeError("Database chưa sẵn sàng.")
    instance.template_service.repository.mark_completed = fail
    instance.template_service.record_failure = fail
    message = Message(task(), {"x-retry-count": instance.settings.max_retries})
    await instance.handle_template_message(message)
    assert message.acked == 0 and message.rejected == [True]


@pytest.mark.asyncio
async def test_silent_template_is_not_retried(tmp_path, monkeypatch):
    instance, sessions = worker(tmp_path, monkeypatch)
    async def silent(url, directory):
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "silent.wav"
        sf.write(path, np.zeros(800), 8000)
        return path
    instance.template_service.audio_service = SimpleNamespace(download=silent)
    payload = task()
    message = Message(payload)
    await instance.handle_template_message(message)
    with sessions() as session:
        saved = instance.template_service.repository.get(session, str(payload.analysisId))
        assert saved.error_code == "AUDIO_SILENT" and saved.quality_report["verdict"] == "REJECT"
    assert message.acked == 1


@pytest.mark.asyncio
async def test_template_dlq_is_declared_without_a_consumer(tmp_path, monkeypatch):
    instance, _ = worker(tmp_path, monkeypatch)
    declared = []
    async def declare(name, **kwargs):
        declared.append((name, kwargs))
        return SimpleNamespace()  # no consume method: DLQ must remain untouched
    await instance._declare_template_queue(SimpleNamespace(declare_queue=declare))
    assert declared[0] == (instance.settings.template_dead_letter_queue, {"durable": True})
    assert declared[1][1]["arguments"]["x-dead-letter-routing-key"] == instance.settings.template_dead_letter_queue


@pytest.mark.asyncio
async def test_decoder_timeout_sends_retry_instead_of_permanent_quality_failure(tmp_path, monkeypatch):
    import subprocess
    from app.services import audio_quality_service as quality_module
    instance, sessions = worker(tmp_path, monkeypatch)
    async def fallback_audio(url, directory):
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "source.m4a"
        path.write_bytes(b"force fallback decoder")
        return path
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("ffmpeg", 60)
    instance.template_service.audio_service = SimpleNamespace(download=fallback_audio)
    instance.connection = Broker()
    monkeypatch.setattr(quality_module.subprocess, "run", timeout)
    payload = task()
    message = Message(payload)
    await instance.handle_template_message(message)
    assert len(instance.connection.published) == 1 and message.acked == 1
    with sessions() as session:
        saved = instance.template_service.repository.get(session, str(payload.analysisId))
        assert saved.status == "processing" and saved.error_code is None


@pytest.mark.asyncio
async def test_retry_recovers_when_json_exists_but_completion_commit_failed(tmp_path, monkeypatch):
    instance, sessions = worker(tmp_path, monkeypatch)
    payload = task()
    complete = instance.template_service.repository.mark_completed
    failed = False
    def fail_once(*args, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("Commit tạm thời thất bại.")
        return complete(*args, **kwargs)
    instance.template_service.repository.mark_completed = fail_once
    instance.connection = Broker()
    first = Message(payload)
    await instance.handle_template_message(first)
    key = f"template-analyses/{payload.sheetMusicId}/{payload.mediaFileId}/{payload.analysisId}/result.json"
    assert (instance.template_service.storage.root / key).is_file()
    with sessions() as session:
        assert instance.template_service.repository.get(session, str(payload.analysisId)).status == "processing"
    assert first.acked == 1 and len(instance.connection.published) == 1
    second = Message(payload, {"x-retry-count": 1})
    await instance.handle_template_message(second)
    with sessions() as session:
        saved = instance.template_service.repository.get(session, str(payload.analysisId))
        assert saved.status == "completed"
        instance.template_service.storage.read(saved.artifact_key, saved.artifact_sha256)
    assert second.acked == 1
