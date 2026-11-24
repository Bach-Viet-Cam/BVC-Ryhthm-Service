"""Sheet "Rhythm Worker & Callback" (ITC-RW-001 .. ITC-RW-017)."""

from __future__ import annotations

import time
import uuid

from . import support

STAGES = ["quality_check", "audio_analysis", "comparison", "feedback"]


def _job(stub: support.StubServer, evaluation_id: str, **extra) -> dict:
    extra.setdefault("callbackUrl", stub.callback_url)
    return support.evaluation_task(stub, evaluation_id, **extra)


def _events(stub: support.StubServer, evaluation_id: str) -> list[tuple[str, str]]:
    return [(item["body"]["job"], item["body"]["status"]) for item in stub.state.callbacks_for(evaluation_id)]


def _settle() -> None:
    support.wait_for(lambda: support.queue_count(support.EVALUATE_QUEUE) == 0, timeout=120, message="queue to empty")
    time.sleep(3)


def test_itc_rw_001_valid_job_reports_every_stage(api, stub):
    support.publish(support.EVALUATE_QUEUE, _job(stub, "wk-1"))
    record = support.wait_evaluation(api, "wk-1")
    time.sleep(1)
    expected = [(stage, status) for stage in STAGES for status in ("started", "done")]
    assert _events(stub, "wk-1") == expected
    assert "result" in stub.state.callbacks_for("wk-1")[-1]["body"]
    assert record["status"] == "completed"
    assert support.queue_count(support.EVALUATE_QUEUE) == 0


def test_itc_rw_002_bad_job_goes_to_dead_letter(api, stub, db_url):
    # The worker reads rhythm.evaluate.dlq itself (log consumer), so the messages are counted by the
    # broker statistics of the queue instead of being read back from it.
    routed_before = support.queue_published(support.EVALUATE_DLQ)
    before = support.query(db_url, "SELECT COUNT(*) FROM rhythm_evaluations")[0][0]
    no_id = _job(stub, "wk-noid")
    no_id.pop("evaluationId")
    bad_range = _job(stub, "wk-range", templateStartSec=6, templateEndSec=2)
    support.publish(support.EVALUATE_QUEUE, "hello")
    support.publish(support.EVALUATE_QUEUE, no_id)
    support.publish(support.EVALUATE_QUEUE, bad_range)
    _settle()
    routed = support.wait_for(lambda: (support.queue_published(support.EVALUATE_DLQ) - routed_before) >= 3 or None,
                              timeout=30, message="3 messages routed to the dead-letter queue")
    assert routed
    assert support.query(db_url, "SELECT COUNT(*) FROM rhythm_evaluations")[0][0] == before
    assert stub.state.callbacks == []


def _download_failure(api, stub, evaluation_id: str, code: int) -> dict:
    name = f"{evaluation_id}.wav"
    support.publish(support.EVALUATE_QUEUE, _job(stub, evaluation_id, studentUrl=stub.status_url(code, name)))
    record = support.wait_evaluation(api, evaluation_id)
    time.sleep(1)
    failed = [item["body"] for item in stub.state.callbacks_for(evaluation_id) if item["body"]["status"] == "failed"]
    # 5xx: 1 try + x-retry-count 1, 2, 3. Round 1 decision: 404 is permanent and is not retried.
    expected = 1 if code == 404 else 4
    assert stub.state.hits[f"/status/{code}/{name}"] == expected, stub.state.hits
    assert len(failed) == 1, failed
    assert failed[0]["job"] == "quality_check" and failed[0]["code"] == "AUDIO_DOWNLOAD_FAILED"
    assert failed[0]["reason"] == f"Storage trả về HTTP {code}"
    assert record["status"] == "failed" and record["error"]["code"] == "AUDIO_DOWNLOAD_FAILED"
    return record


def test_itc_rw_003_missing_student_file_fails(api, stub):
    _download_failure(api, stub, "wk-404", 404)


def test_itc_rw_004_storage_error_retried_three_times(api, stub):
    record = _download_failure(api, stub, "wk-503", 503)
    assert record["error"]["message"] == "Storage trả về HTTP 503"


def test_itc_rw_005_bad_retry_header_is_not_retried(api, stub):
    name = "wk-hdr.wav"
    support.publish(support.EVALUATE_QUEUE, _job(stub, "wk-hdr", studentUrl=stub.status_url(503, name)),
                    headers={"x-retry-count": "abc"})
    record = support.wait_evaluation(api, "wk-hdr")
    assert record["status"] == "failed"
    assert stub.state.hits[f"/status/503/{name}"] == 1


def test_itc_rw_006_silent_recording_rejected(api, stub):
    support.publish(support.EVALUATE_QUEUE, _job(stub, "wk-silent", student="silent.wav"))
    record = support.wait_evaluation(api, "wk-silent")
    time.sleep(1)
    bodies = [item["body"] for item in stub.state.callbacks_for("wk-silent")]
    assert {"evaluationId": "wk-silent", "attempt": 1, "job": "quality_check", "status": "rejected",
            "code": "AUDIO_SILENT", "reason": "Bản thu hoàn toàn im lặng. Vui lòng thu âm lại."} in bodies
    assert not [body for body in bodies if body["status"] == "failed"]
    # Round 1 decision: the evaluation is stored with status "failed" and the rejection code
    assert record["status"] == "failed", record
    assert record["error"]["code"] == "AUDIO_SILENT"
    assert stub.state.hits["/media/silent.wav"] == 1


def test_itc_rw_007_completed_job_not_scored_again(api, stub, db_url):
    if api.get("/api/v1/evaluations/wk-1").status_code == 404:
        support.publish(support.EVALUATE_QUEUE, _job(stub, "wk-1"))
        support.wait_evaluation(api, "wk-1")
        time.sleep(1)
        stub.state.reset()
    before = support.query(db_url, "SELECT result_json::text, completed_at FROM rhythm_evaluations WHERE evaluation_id = 'wk-1'")
    support.publish(support.EVALUATE_QUEUE, _job(stub, "wk-1"))
    _settle()
    assert stub.state.callbacks_for("wk-1") == []
    after = support.query(db_url, "SELECT result_json::text, completed_at FROM rhythm_evaluations WHERE evaluation_id = 'wk-1'")
    assert after == before
    assert support.queue_count(support.EVALUATE_QUEUE) == 0


def test_itc_rw_008_many_evaluations_are_queued_and_all_complete(api, stub, db_url):
    # Round 1 decision: one worker runs one evaluation at a time (prefetch 1); the others wait in the
    # queue and every job still completes. The 50-at-once target of the NFR is not checked here.
    ids = [f"wk-p{index:02d}-{support.RUN_ID}" for index in range(1, 56)]
    for evaluation_id in ids:
        support.publish(support.EVALUATE_QUEUE, _job(stub, evaluation_id, callbackUrl=None))
    pattern = f"wk-p%-{support.RUN_ID}"
    peak = 0
    deadline = time.monotonic() + 1800

    def counts() -> dict[str, int]:
        rows = support.query(db_url, "SELECT status, COUNT(*) FROM rhythm_evaluations WHERE evaluation_id LIKE %s GROUP BY status", (pattern,))
        return dict(rows)

    while time.monotonic() < deadline:
        current = counts()
        peak = max(peak, current.get("processing", 0))
        if current.get("completed", 0) + current.get("failed", 0) == len(ids):
            break
        time.sleep(0.5)
    assert counts() == {"completed": len(ids)}, counts()
    assert peak <= 1, f"peak of evaluations processing at the same time: {peak}"
    assert support.queue_count(support.EVALUATE_QUEUE) == 0
    assert support.queue_count(support.EVALUATE_DLQ) == 0


def test_itc_rw_009_job_not_lost_when_worker_stops(api, stub, shared, db_url):
    support.publish(support.EVALUATE_QUEUE, _job(stub, "wk-stop", student="very-long.wav", template="long.wav"))
    support.wait_for(lambda: support.query(db_url, "SELECT status FROM rhythm_evaluations WHERE evaluation_id = 'wk-stop'") == [("processing",)],
                     timeout=120, message="wk-stop processing")
    shared.pause()
    support.wait_for(lambda: support.queue_count(support.EVALUATE_QUEUE) == 1, timeout=60, message="message back in the queue")
    with shared.ensure().client() as client:
        assert support.wait_evaluation(client, "wk-stop", timeout=900)["status"] == "completed"


def test_itc_rw_010_callback_has_secret_and_fields(api, stub):
    support.publish(support.EVALUATE_QUEUE, _job(stub, "wk-att2", attempt=2))
    support.wait_evaluation(api, "wk-att2")
    time.sleep(1)
    first = stub.state.callbacks_for("wk-att2")[0]
    headers = {key.lower(): value for key, value in first["headers"].items()}
    assert headers["x-callback-secret"] == support.CALLBACK_SECRET
    assert first["body"]["evaluationId"] == "wk-att2" and first["body"]["attempt"] == 2
    assert "job" in first["body"] and "status" in first["body"]


def test_itc_rw_011_no_callback_without_secret(spawn, stub):
    service = spawn({"RHYTHM_SERVICE_CALLBACK_SECRET": ""})
    support.publish(support.EVALUATE_QUEUE, _job(stub, "wk-nosecret"))
    with service.client() as client:
        assert support.wait_evaluation(client, "wk-nosecret")["status"] == "completed"
    time.sleep(1)
    assert stub.state.callbacks_for("wk-nosecret") == []


def test_itc_rw_012_no_callback_outside_allowlist(spawn, stub):
    service = spawn({"RHYTHM_SERVICE_CALLBACK_ALLOWED_HOSTS": "be.local"})
    callback = f"http://localhost:{stub.port}/callback"
    support.publish(support.EVALUATE_QUEUE, _job(stub, "wk-allow", callbackUrl=callback))
    with service.client() as client:
        assert support.wait_evaluation(client, "wk-allow")["status"] == "completed"
    time.sleep(1)
    assert stub.state.callbacks_for("wk-allow") == []


def test_itc_rw_013_callback_retried_on_server_error(api, stub):
    stub.state.callback_status = 500
    support.publish(support.EVALUATE_QUEUE, _job(stub, "wk-cb500"))
    assert support.wait_evaluation(api, "wk-cb500")["status"] == "completed"
    time.sleep(2)
    assert _events(stub, "wk-cb500").count(("quality_check", "started")) == 3


def test_itc_rw_014_callback_not_retried_on_client_error(api, stub):
    stub.state.callback_status = 400
    support.publish(support.EVALUATE_QUEUE, _job(stub, "wk-cb400"))
    assert support.wait_evaluation(api, "wk-cb400")["status"] == "completed"
    time.sleep(1)
    events = _events(stub, "wk-cb400")
    assert all(events.count(event) == 1 for event in events), events
    assert len(events) == 8


def test_itc_rw_015_scoring_goes_on_when_callback_server_is_down(api, stub):
    closed = f"http://127.0.0.1:{support.free_port()}/callback"
    support.publish(support.EVALUATE_QUEUE, _job(stub, "wk-cbdown", callbackUrl=closed))
    record = support.wait_evaluation(api, "wk-cbdown")
    assert record["status"] == "completed" and record["result"]


def test_itc_rw_016_job_without_callback_url(api, stub):
    support.publish(support.EVALUATE_QUEUE, _job(stub, "wk-nocb", callbackUrl=None))
    assert support.wait_evaluation(api, "wk-nocb")["status"] == "completed"
    assert stub.state.callbacks_for("wk-nocb") == []


def test_itc_rw_017_wrong_queue_settings_stop_start(spawn):
    blank = spawn({"RHYTHM_SERVICE_RHYTHM_TASK_QUEUE": " "}, wait=False)
    assert blank.wait_exit() != 0
    assert "Tên hàng đợi và thư mục lưu trữ không được để trống." in blank.log()
    same = spawn({"RHYTHM_SERVICE_TEMPLATE_TASK_QUEUE": support.EVALUATE_QUEUE}, wait=False)
    assert same.wait_exit() != 0
    assert "Các hàng đợi so sánh, template và dead-letter phải có tên khác nhau." in same.log()
