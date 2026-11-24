"""UTC-RMQ: unit tests for app/messaging/rabbitmq_worker.py using fakes only (no broker)."""
import asyncio
import json
import uuid
from types import SimpleNamespace

import pytest

from app.messaging import rabbitmq_worker as module
from app.messaging.rabbitmq_worker import RabbitMqWorker
from app.services.audio_quality_service import AudioProcessingError, AudioQualityError
from app.services.evaluation_service import PermanentEvaluationError
from app.services.template_analysis_service import TemplateAnalysisError
from app.feedback.gemini_feedback import FeedbackGenerationError
from app.repositories.template_analysis_repository import TemplateTaskConflictError

MAX = 3
INSTR = dict(name="Dan bau", fminHz=65, fmaxHz=1200, toleranceCents=70)


class FakeMessage:
    def __init__(self, body=b"", headers=None):
        self.body = body
        self.headers = headers
        self.acked = 0
        self.rejected = []
        self.processed = False

    async def ack(self):
        self.acked += 1
        self.processed = True

    async def reject(self, *, requeue):
        self.rejected.append(requeue)
        self.processed = True


class FakeEvalService:
    def __init__(self, error=None):
        self.error = error
        self.tasks = []
        self.final = []
        self.failures = []

    async def process_task(self, task):
        self.tasks.append(task)
        if self.error:
            raise self.error

    async def report_final_failure(self, task, code, msg):
        self.final.append((code, msg))

    def record_failure(self, task, code, msg):
        self.failures.append((code, msg))


class FakeTemplateService:
    def __init__(self, error=None):
        self.error = error
        self.tasks = []
        self.failures = []

    async def process_task(self, task):
        self.tasks.append(task)
        if self.error:
            raise self.error

    def record_failure(self, task, code, msg, report=None):
        self.failures.append((code, msg, report))


class FakeConnection:
    def __init__(self, confirm=True):
        self.confirm = confirm
        self.published = []
        self.closed = False
        self.kwargs = None

    async def channel(self, **kwargs):
        self.kwargs = kwargs

        async def publish(msg, *, routing_key, mandatory=True):
            self.published.append((msg, routing_key, mandatory))
            return self.confirm

        async def close():
            self.closed = True

        return SimpleNamespace(default_exchange=SimpleNamespace(publish=publish), close=close)


def make_worker(eval_error=None, tmpl_error=None, confirm=True):
    w = object.__new__(RabbitMqWorker)
    w.settings = SimpleNamespace(
        max_retries=MAX, rhythm_task_queue="eval.q", rhythm_dead_letter_queue="eval.dlq",
        template_task_queue="tmpl.q", template_dead_letter_queue="tmpl.dlq")
    w.evaluation_service = FakeEvalService(eval_error)
    w.template_service = FakeTemplateService(tmpl_error)
    w.connection = FakeConnection(confirm)
    w._in_flight = set()
    return w


def eval_body():
    return json.dumps(dict(messageId="m1", evaluationId="e1", templateUrl="https://x.com/a.wav",
                           studentUrl="https://x.com/b.wav", instrumentId="dan_bau", instrument=INSTR)).encode()


def tmpl_body(**over):
    d = dict(messageId=str(uuid.uuid4()), analysisId=str(uuid.uuid4()), sheetMusicId=str(uuid.uuid4()),
             mediaFileId=str(uuid.uuid4()), mediaUrl="https://x.com/t.wav", instrumentCode="DBA103",
             instrument=INSTR, requestedAt="2026-01-01T00:00:00Z")
    d.update(over)
    return json.dumps(d).encode()


def quality_error():
    return AudioQualityError({"rejections": [{"code": "AUDIO_SILENT", "message": "Audio im lặng"}], "verdict": "REJECT"})


# ---------- handle_message ----------
@pytest.mark.asyncio
async def test_utc_rmq_001_handle_message_valid_acks():
    w = make_worker(); m = FakeMessage(eval_body())
    await w.handle_message(m)
    assert m.acked == 1 and m.rejected == [] and len(w.evaluation_service.tasks) == 1


@pytest.mark.asyncio
async def test_utc_rmq_002_handle_message_invalid_json_rejected_to_dlq():
    w = make_worker(); m = FakeMessage(b"not-json")
    await w.handle_message(m)
    assert m.rejected == [False] and m.acked == 0 and w.evaluation_service.tasks == []


@pytest.mark.asyncio
async def test_utc_rmq_003_handle_message_schema_invalid_rejected_to_dlq():
    w = make_worker(); m = FakeMessage(json.dumps({"messageId": "m1"}).encode())
    await w.handle_message(m)
    assert m.rejected == [False] and m.acked == 0


@pytest.mark.asyncio
async def test_utc_rmq_004_handle_message_unexpected_failure_requeues(monkeypatch):
    w = make_worker(); m = FakeMessage(eval_body())
    async def boom(*a, **k): raise RuntimeError("db down")
    monkeypatch.setattr(w, "_process_evaluation", boom)
    await w.handle_message(m)
    assert m.rejected == [True] and m.acked == 0


@pytest.mark.asyncio
async def test_utc_rmq_005_handle_message_failure_after_processed_not_rejected(monkeypatch):
    w = make_worker(); m = FakeMessage(eval_body())
    async def boom(message, task):
        await message.ack(); raise RuntimeError("late")
    monkeypatch.setattr(w, "_process_evaluation", boom)
    await w.handle_message(m)
    assert m.rejected == [] and m.acked == 1


# ---------- _process_evaluation / _handle_error ----------
@pytest.mark.asyncio
async def test_utc_rmq_006_retryable_download_error_under_max_republishes():
    w = make_worker(eval_error=module.AudioDownloadError("AUDIO_DOWNLOAD_FAILED", "tai loi", retryable=True))
    m = FakeMessage(eval_body(), {"x-retry-count": MAX - 1})
    await w.handle_message(m)
    msg, rk, mandatory = w.connection.published[0]
    assert rk == "eval.q" and mandatory and msg.headers["x-retry-count"] == MAX
    assert m.acked == 1 and w.evaluation_service.failures == [] and w.connection.closed


@pytest.mark.asyncio
async def test_utc_rmq_007_retryable_error_at_max_retries_is_final():
    w = make_worker(eval_error=module.AudioDownloadError("AUDIO_DOWNLOAD_FAILED", "tai loi", retryable=True))
    m = FakeMessage(eval_body(), {"x-retry-count": MAX})
    await w.handle_message(m)
    assert w.connection.published == [] and m.acked == 1
    assert w.evaluation_service.final == [("AUDIO_DOWNLOAD_FAILED", "tai loi")]
    assert w.evaluation_service.failures == [("AUDIO_DOWNLOAD_FAILED", "tai loi")]


@pytest.mark.asyncio
async def test_utc_rmq_008_retryable_error_over_max_retries_is_final():
    w = make_worker(eval_error=AudioProcessingError("DECODE_FAILED", "giai ma loi"))
    m = FakeMessage(eval_body(), {"x-retry-count": MAX + 5})
    await w.handle_message(m)
    assert w.connection.published == [] and m.acked == 1 and len(w.evaluation_service.failures) == 1


@pytest.mark.asyncio
async def test_utc_rmq_009_first_attempt_without_header_retries_with_count_1():
    w = make_worker(eval_error=AudioProcessingError("DECODE_FAILED", "x"))
    m = FakeMessage(eval_body(), None)
    await w.handle_message(m)
    assert w.connection.published[0][0].headers["x-retry-count"] == 1 and m.acked == 1


@pytest.mark.asyncio
async def test_utc_rmq_010_non_retryable_download_error_is_final_at_zero_retries():
    w = make_worker(eval_error=module.AudioDownloadError("INVALID_AUDIO_URL", "url sai", retryable=False))
    m = FakeMessage(eval_body(), {"x-retry-count": 0})
    await w.handle_message(m)
    assert w.connection.published == [] and m.acked == 1
    assert w.evaluation_service.final == [("INVALID_AUDIO_URL", "url sai")]


@pytest.mark.asyncio
async def test_utc_rmq_011_permanent_evaluation_error_uses_its_code():
    w = make_worker(eval_error=PermanentEvaluationError("TEMPLATE_MISSING", "thieu mau"))
    m = FakeMessage(eval_body())
    await w.handle_message(m)
    assert w.evaluation_service.failures == [("TEMPLATE_MISSING", "thieu mau")] and m.acked == 1


@pytest.mark.asyncio
async def test_utc_rmq_012_value_error_maps_to_evaluation_failed():
    w = make_worker(eval_error=ValueError("bad value"))
    m = FakeMessage(eval_body())
    await w.handle_message(m)
    assert w.evaluation_service.failures == [("EVALUATION_FAILED", "bad value")] and m.acked == 1


@pytest.mark.asyncio
async def test_utc_rmq_013_audio_quality_error_final_without_callback():
    w = make_worker(eval_error=quality_error())
    m = FakeMessage(eval_body())
    await w.handle_message(m)
    assert w.evaluation_service.final == []
    assert w.evaluation_service.failures == [("AUDIO_SILENT", "Audio im lặng")] and m.acked == 1


@pytest.mark.asyncio
async def test_utc_rmq_014_feedback_generation_error_final_with_callback():
    w = make_worker(eval_error=FeedbackGenerationError("FEEDBACK_FAILED", "gemini loi"))
    m = FakeMessage(eval_body())
    await w.handle_message(m)
    assert w.evaluation_service.final == [("FEEDBACK_FAILED", "gemini loi")] and m.acked == 1


@pytest.mark.asyncio
async def test_utc_rmq_015_unknown_exception_is_internal_error_retryable():
    w = make_worker(eval_error=RuntimeError("boom"))
    m = FakeMessage(eval_body())
    await w.handle_message(m)
    assert len(w.connection.published) == 1 and m.acked == 1
    w2 = make_worker(eval_error=RuntimeError("boom"))
    m2 = FakeMessage(eval_body(), {"x-retry-count": MAX})
    await w2.handle_message(m2)
    assert w2.evaluation_service.failures == [("INTERNAL_ERROR", "Lỗi nội bộ khi phân tích audio.")]


@pytest.mark.asyncio
async def test_utc_rmq_016_retry_publish_without_connection_requeues_message():
    w = make_worker(eval_error=RuntimeError("boom")); w.connection = None
    m = FakeMessage(eval_body())
    await w.handle_message(m)
    assert m.acked == 0 and m.rejected == [True]


@pytest.mark.asyncio
async def test_utc_rmq_017_unconfirmed_retry_requeues_and_closes_channel():
    w = make_worker(eval_error=RuntimeError("boom"), confirm=False)
    m = FakeMessage(eval_body())
    await w.handle_message(m)
    assert m.acked == 0 and m.rejected == [True] and w.connection.closed


# ---------- templates ----------
@pytest.mark.asyncio
async def test_utc_rmq_018_template_valid_acks():
    w = make_worker(); m = FakeMessage(tmpl_body())
    await w.handle_template_message(m)
    assert m.acked == 1 and len(w.template_service.tasks) == 1


@pytest.mark.asyncio
async def test_utc_rmq_019_template_invalid_payload_rejected_to_dlq():
    w = make_worker()
    for body in (b"{{", tmpl_body(extra="x")):
        m = FakeMessage(body)
        await w.handle_template_message(m)
        assert m.rejected == [False] and m.acked == 0


@pytest.mark.asyncio
async def test_utc_rmq_020_template_conflict_rejected_without_ack():
    w = make_worker(tmpl_error=TemplateTaskConflictError("conflict")); m = FakeMessage(tmpl_body())
    await w.handle_template_message(m)
    assert m.rejected == [False] and m.acked == 0 and w.template_service.failures == []


@pytest.mark.asyncio
async def test_utc_rmq_021_template_retryable_under_max_republishes_to_template_queue():
    w = make_worker(tmpl_error=module.AudioDownloadError("AUDIO_DOWNLOAD_FAILED", "x", retryable=True))
    m = FakeMessage(tmpl_body(), {"x-retry-count": MAX - 1})
    await w.handle_template_message(m)
    assert w.connection.published[0][1] == "tmpl.q" and w.connection.published[0][0].headers["x-retry-count"] == MAX
    assert m.acked == 1 and w.template_service.failures == []


@pytest.mark.asyncio
async def test_utc_rmq_022_template_retryable_at_max_records_failure():
    w = make_worker(tmpl_error=AudioProcessingError("DECODE_FAILED", "loi"))
    m = FakeMessage(tmpl_body(), {"x-retry-count": MAX})
    await w.handle_template_message(m)
    assert w.connection.published == [] and m.acked == 1
    assert w.template_service.failures == [("DECODE_FAILED", "loi", None)]


@pytest.mark.asyncio
async def test_utc_rmq_023_template_quality_error_passes_report_no_retry():
    err = quality_error(); w = make_worker(tmpl_error=err)
    m = FakeMessage(tmpl_body())
    await w.handle_template_message(m)
    assert w.connection.published == [] and w.template_service.failures == [("AUDIO_SILENT", "Audio im lặng", err.report)]


@pytest.mark.asyncio
async def test_utc_rmq_024_template_analysis_error_and_value_error_permanent():
    w = make_worker(tmpl_error=TemplateAnalysisError("NO_NOTES", "khong co not")); m = FakeMessage(tmpl_body())
    await w.handle_template_message(m)
    assert w.template_service.failures == [("NO_NOTES", "khong co not", None)] and m.acked == 1
    w2 = make_worker(tmpl_error=ValueError("x")); m2 = FakeMessage(tmpl_body())
    await w2.handle_template_message(m2)
    assert w2.template_service.failures == [("TEMPLATE_ANALYSIS_FAILED", "Không thể phân tích nội dung audio template.", None)]
    assert w2.connection.published == []


@pytest.mark.asyncio
async def test_utc_rmq_025_template_unknown_exception_internal_error_retryable():
    w = make_worker(tmpl_error=RuntimeError("boom")); m = FakeMessage(tmpl_body())
    await w.handle_template_message(m)
    assert len(w.connection.published) == 1 and m.acked == 1
    w2 = make_worker(tmpl_error=RuntimeError("boom")); m2 = FakeMessage(tmpl_body(), {"x-retry-count": MAX})
    await w2.handle_template_message(m2)
    assert w2.template_service.failures == [("INTERNAL_ERROR", "Lỗi nội bộ khi phân tích template.", None)]


@pytest.mark.asyncio
async def test_utc_rmq_026_template_failure_in_record_requeues(monkeypatch):
    w = make_worker(tmpl_error=TemplateAnalysisError("X", "y")); m = FakeMessage(tmpl_body())
    def bad(*a, **k): raise RuntimeError("db")
    w.template_service.record_failure = bad
    await w.handle_template_message(m)
    assert m.acked == 0 and m.rejected == [True]


# ---------- _retry_count ----------
def test_utc_rmq_027_retry_count_parsing():
    cases = [(None, 0), ({}, 0), ({"x-retry-count": 2}, 2), ({"x-retry-count": "4"}, 4),
             ({"x-retry-count": -5}, 0), ({"x-retry-count": "abc"}, MAX), ({"x-retry-count": None}, MAX)]
    w = make_worker()
    for headers, expected in cases:
        assert w._retry_count(FakeMessage(b"", headers)) == expected, headers


# ---------- _publish_retry ----------
@pytest.mark.asyncio
async def test_utc_rmq_028_publish_retry_builds_persistent_message_and_keeps_headers():
    w = make_worker()
    m = FakeMessage(b'{"a":1}', {"x-retry-count": 1, "trace": "t"})
    m.message_id = "mid"; m.correlation_id = "cid"
    await w._publish_retry(m, "q")
    msg, rk, mandatory = w.connection.published[0]
    assert rk == "q" and mandatory and msg.body == b'{"a":1}' and msg.content_type == "application/json"
    assert msg.headers == {"x-retry-count": 2, "trace": "t"} and msg.delivery_mode == 2
    assert msg.message_id == "mid" and msg.correlation_id == "cid"
    assert w.connection.kwargs == {"publisher_confirms": True, "on_return_raises": True}


# ---------- _requeue_after_failure ----------
@pytest.mark.asyncio
async def test_utc_rmq_029_requeue_after_failure_skips_processed_message():
    w = make_worker()
    m = FakeMessage(); m.processed = True
    await w._requeue_after_failure(m)
    assert m.rejected == []
    m2 = FakeMessage()
    await w._requeue_after_failure(m2)
    assert m2.rejected == [True]


# ---------- queue declarations ----------
class FakeQueue:
    def __init__(self):
        self.consumers = []

    async def consume(self, cb, no_ack=False):
        self.consumers.append((cb, no_ack))


def fake_channel():
    declared, queues = [], {}
    async def declare(name, **kwargs):
        declared.append((name, kwargs)); queues[name] = FakeQueue(); return queues[name]
    return SimpleNamespace(declare_queue=declare), declared, queues


@pytest.mark.asyncio
async def test_utc_rmq_030_declare_queues_dlq_consumed_and_main_has_dead_letter_args():
    w = make_worker(); ch, declared, queues = fake_channel()
    main = await w._declare_queues(ch)
    assert declared[0] == ("eval.dlq", {"durable": True})
    assert declared[1] == ("eval.q", {"durable": True, "arguments": {
        "x-dead-letter-exchange": "", "x-dead-letter-routing-key": "eval.dlq"}})
    assert queues["eval.dlq"].consumers == [(w._log_dead_letter, True)] and main is queues["eval.q"]


@pytest.mark.asyncio
async def test_utc_rmq_031_declare_template_queue_args_and_no_dlq_consumer():
    w = make_worker(); ch, declared, queues = fake_channel()
    main = await w._declare_template_queue(ch)
    assert declared[0] == ("tmpl.dlq", {"durable": True})
    assert declared[1][1]["arguments"] == {"x-dead-letter-exchange": "", "x-dead-letter-routing-key": "tmpl.dlq"}
    assert queues["tmpl.dlq"].consumers == [] and main is queues["tmpl.q"]


@pytest.mark.asyncio
async def test_utc_rmq_032_log_dead_letter_returns_none():
    assert await make_worker()._log_dead_letter(FakeMessage(b"x")) is None


# ---------- _tracked ----------
@pytest.mark.asyncio
async def test_utc_rmq_033_tracked_registers_task_during_handler_and_removes_after():
    w = make_worker(); seen = []
    async def handler(m): seen.append(set(w._in_flight))
    await w._tracked(handler, FakeMessage())
    assert seen[0] == {asyncio.current_task()} and w._in_flight == set()


@pytest.mark.asyncio
async def test_utc_rmq_034_tracked_removes_task_even_if_handler_raises():
    w = make_worker()
    async def handler(m): raise RuntimeError("x")
    with pytest.raises(RuntimeError):
        await w._tracked(handler, FakeMessage())
    assert w._in_flight == set()
