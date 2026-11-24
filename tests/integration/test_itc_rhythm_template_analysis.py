"""Sheet "Rhythm Template Analysis" (ITC-RT-001 .. ITC-RT-017). ITC-RT-018 is a BE test (BVC-BE/tests/integration)."""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

import httpx
import pytest

from . import support

NOT_FOUND = {"code": "TEMPLATE_NOT_FOUND", "message": "Không tìm thấy yêu cầu phân tích template."}
LOOKUP_NOT_FOUND = {"code": "TEMPLATE_NOT_FOUND", "message": "Chưa có kết quả cho MAIN, audio và phiên bản phân tích này."}
STATE: dict[str, dict] = {}


def _settle(queue: str = support.TEMPLATE_QUEUE) -> None:
    support.wait_for(lambda: support.queue_count(queue) == 0, timeout=60, message="queue to empty")
    time.sleep(3)


def _lookup_params(task: dict, **changes) -> dict:
    params = {"sheetMusicId": task["sheetMusicId"], "mediaFileId": task["mediaFileId"],
              "instrumentCode": "TRANH", "analysisVersion": "template-pyin-v1"}
    params.update(changes)
    return {key: value for key, value in params.items() if value is not None}


@pytest.fixture()
def ta1(api, stub) -> dict:
    if "ta1" not in STATE:
        task = support.template_task(stub)
        support.publish(support.TEMPLATE_QUEUE, task)
        support.wait_template(api, task["analysisId"])
        STATE["ta1"] = task
    return STATE["ta1"]


@pytest.fixture()
def ta2(api, stub) -> dict:
    if "ta2" not in STATE:
        task = support.template_task(stub, media="silent.wav")
        support.publish(support.TEMPLATE_QUEUE, task)
        support.wait_template(api, task["analysisId"])
        STATE["ta2"] = task
    return STATE["ta2"]


def test_itc_rt_001_valid_template_is_analysed(api, stub):
    task = support.template_task(stub)
    support.publish(support.TEMPLATE_QUEUE, task)
    record = support.wait_template(api, task["analysisId"])
    STATE["ta1"] = task
    assert record["status"] == "completed", record
    assert record["analysisVersion"] == "template-pyin-v1"
    assert record["sourceSha256"]
    assert record["quality"]["verdict"] in {"PASS", "WARN"}
    assert record["error"] is None
    assert record["artifactUrl"] == f"/api/v1/template-analyses/{task['analysisId']}/artifact"


def test_itc_rt_002_result_file(api, ta1):
    response = api.get(f"/api/v1/template-analyses/{ta1['analysisId']}/artifact")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    document = response.json()
    assert document["schemaVersion"] == 1 and document["analysisId"] == ta1["analysisId"]
    assert document["source"]["sha256"]
    instrument = document["instrument"]
    assert instrument["code"] == "TRANH"
    assert {"fminHz", "fmaxHz", "toleranceCents"} <= set(instrument)
    assert document["pitch"]["f0Hz"]


def test_itc_rt_003_same_message_analysed_once(api, ta1, db_url):
    before = api.get(f"/api/v1/template-analyses/{ta1['analysisId']}").json()["completedAt"]
    support.publish(support.TEMPLATE_QUEUE, ta1)
    _settle()
    record = api.get(f"/api/v1/template-analyses/{ta1['analysisId']}").json()
    assert record["status"] == "completed" and record["completedAt"] == before
    rows = support.query(db_url, "SELECT COUNT(*) FROM template_analyses WHERE analysis_id = %s", (ta1["analysisId"],))
    assert rows == [(1,)]


def test_itc_rt_004_analysis_id_cannot_be_reused(api, ta1):
    support.drain(support.TEMPLATE_DLQ)
    conflicting = {**ta1, "messageId": str(uuid.uuid4()), "mediaFileId": str(uuid.uuid4())}
    support.publish(support.TEMPLATE_QUEUE, conflicting)
    _settle()
    dead = [json.loads(body) for body in support.drain(support.TEMPLATE_DLQ)]
    assert any(item.get("mediaFileId") == conflicting["mediaFileId"] for item in dead), dead
    record = api.get(f"/api/v1/template-analyses/{ta1['analysisId']}").json()
    assert record["mediaFileId"] == ta1["mediaFileId"] and record["status"] == "completed"


def test_itc_rt_005_bad_messages_go_to_dead_letter(api, stub, db_url):
    support.drain(support.TEMPLATE_DLQ)
    before = support.query(db_url, "SELECT COUNT(*) FROM template_analyses")[0][0]
    bad = [
        {**support.template_task(stub), "foo": 1},
        {**support.template_task(stub), "requestedAt": "2026-11-19T08:00:00"},
        {**support.template_task(stub), "instrument": {**support.TRANH, "fminHz": 2000.0}},
        {**support.template_task(stub), "mediaUrl": "not-a-url"},
    ]
    for message in bad:
        support.publish(support.TEMPLATE_QUEUE, message)
    _settle()
    dead = {json.loads(body)["analysisId"] for body in support.drain(support.TEMPLATE_DLQ)}
    assert {message["analysisId"] for message in bad} <= dead
    assert support.query(db_url, "SELECT COUNT(*) FROM template_analyses")[0][0] == before


def test_itc_rt_006_silent_template_fails(api, stub):
    task = support.template_task(stub, media="silent.wav")
    support.publish(support.TEMPLATE_QUEUE, task)
    record = support.wait_template(api, task["analysisId"])
    STATE["ta2"] = task
    silent = {"code": "AUDIO_SILENT", "message": "Bản thu hoàn toàn im lặng. Vui lòng thu âm lại."}
    assert record["status"] == "failed"
    assert record["error"] == silent
    assert record["quality"]["verdict"] == "REJECT" and record["quality"]["rejections"] == [silent]
    assert record["artifactUrl"] is None


def _download_failure(api, stub, code: int) -> None:
    task = support.template_task(stub)
    name = f"rt-{code}-{uuid.uuid4().hex[:6]}.wav"
    task["mediaUrl"] = stub.status_url(code, name)
    support.publish(support.TEMPLATE_QUEUE, task)
    record = support.wait_template(api, task["analysisId"])
    # 5xx: 1 try + 3 retries (JOB-01). Round 1 decision: 404 is permanent and is not retried.
    expected = 1 if code == 404 else 4
    assert stub.state.hits[f"/status/{code}/{name}"] == expected, stub.state.hits
    assert record["status"] == "failed"
    assert record["error"] == {"code": "AUDIO_DOWNLOAD_FAILED", "message": f"Storage trả về HTTP {code}"}


def test_itc_rt_007_missing_template_fails(api, stub):
    _download_failure(api, stub, 404)


def test_itc_rt_008_storage_error_retried_three_times(api, stub):
    _download_failure(api, stub, 503)


def test_itc_rt_009_failed_analysis_not_run_again(api, stub, ta2):
    before = api.get(f"/api/v1/template-analyses/{ta2['analysisId']}").json()
    support.publish(support.TEMPLATE_QUEUE, ta2)
    _settle()
    after = api.get(f"/api/v1/template-analyses/{ta2['analysisId']}").json()
    assert after["status"] == "failed" and after["completedAt"] == before["completedAt"]
    assert stub.state.hits["/media/silent.wav"] == 0


def test_itc_rt_010_internal_key_required(rhythm, ta1):
    base = f"{rhythm.base}/api/v1/template-analyses"
    detail = "Không có quyền truy cập dịch vụ nội bộ."
    no_header = httpx.get(f"{base}/{ta1['analysisId']}")
    assert no_header.status_code == 401 and no_header.json()["detail"] == detail
    wrong = httpx.get(f"{base}/lookup", params=_lookup_params(ta1), headers={"Authorization": "Bearer wrong"})
    assert wrong.status_code == 401 and wrong.json()["detail"] == detail


def test_itc_rt_011_unknown_analysis(api):
    response = api.get(f"/api/v1/template-analyses/{uuid.uuid4()}")
    assert response.status_code == 404 and response.json()["detail"] == NOT_FOUND
    assert api.get("/api/v1/template-analyses/abc").status_code == 422


def test_itc_rt_012_lookup_finds_completed(api, ta1):
    response = api.get("/api/v1/template-analyses/lookup", params=_lookup_params(ta1))
    assert response.status_code == 200
    body = response.json()
    assert body["analysisId"] == ta1["analysisId"] and body["status"] == "completed" and body["artifactUrl"]


def test_itc_rt_013_lookup_without_analysis(api, ta1):
    for changes in ({"instrumentCode": "BAU"}, {"analysisVersion": "template-pyin-v0"}):
        response = api.get("/api/v1/template-analyses/lookup", params=_lookup_params(ta1, **changes))
        assert response.status_code == 404 and response.json()["detail"] == LOOKUP_NOT_FOUND


def test_itc_rt_014_lookup_not_ready_or_failed(api, stub, ta2):
    task = support.template_task(stub, media="long.wav")
    support.publish(support.TEMPLATE_QUEUE, task)
    # The record exists once the worker takes the message; ask the lookup while it is still running.
    response = support.wait_for(
        lambda: (lambda r: r if r.status_code != 404 else None)(
            api.get("/api/v1/template-analyses/lookup", params=_lookup_params(task))),
        timeout=60, message="template record")
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "TEMPLATE_NOT_READY"
    assert detail["message"] == "Kết quả phân tích template chưa sẵn sàng."
    assert detail["status"] in {"queued", "processing"}
    failed = api.get("/api/v1/template-analyses/lookup", params=_lookup_params(ta2))
    assert failed.status_code == 409
    assert failed.json()["detail"]["status"] == "failed"
    assert failed.json()["detail"]["error"]["code"] == "AUDIO_SILENT"
    support.wait_template(api, task["analysisId"], timeout=300)


def test_itc_rt_015_lookup_parameters_validated(api, ta1):
    path = "/api/v1/template-analyses/lookup"
    assert api.get(path, params=_lookup_params(ta1, instrumentCode=None)).status_code == 422
    assert api.get(path, params=_lookup_params(ta1, sheetMusicId="123")).status_code == 422
    assert api.get(path, params=_lookup_params(ta1, instrumentCode="X" * 65)).status_code == 422


def test_itc_rt_016_lookup_returns_newest(api, stub, ta1):
    newer = support.template_task(stub, sheet_music_id=ta1["sheetMusicId"], media_file_id=ta1["mediaFileId"])
    support.publish(support.TEMPLATE_QUEUE, newer)
    assert support.wait_template(api, newer["analysisId"])["status"] == "completed"
    response = api.get("/api/v1/template-analyses/lookup", params=_lookup_params(ta1))
    assert response.status_code == 200 and response.json()["analysisId"] == newer["analysisId"]


def test_itc_rt_017_result_file_not_ready_or_lost(api, ta1, ta2, db_url, work_dir: Path):
    failed = api.get(f"/api/v1/template-analyses/{ta2['analysisId']}/artifact")
    assert failed.status_code == 409 and failed.json()["detail"]["code"] == "TEMPLATE_NOT_READY"
    key = support.query(db_url, "SELECT artifact_key FROM template_analyses WHERE analysis_id = %s", (ta1["analysisId"],))[0][0]
    (work_dir / "artifacts" / key).unlink()
    lost = api.get(f"/api/v1/template-analyses/{ta1['analysisId']}/artifact")
    assert lost.status_code == 503
    assert lost.json()["detail"] == {"code": "ARTIFACT_UNAVAILABLE",
                                     "message": "Chưa thể đọc file kết quả phân tích. Vui lòng kiểm tra lưu trữ."}
