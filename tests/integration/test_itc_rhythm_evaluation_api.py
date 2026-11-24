"""Sheet "Rhythm Evaluation API" (ITC-RE-001 .. ITC-RE-021)."""

from __future__ import annotations

import http.client
import json
import uuid
from pathlib import Path

import httpx

from . import support

UNAUTHORIZED = "Không có quyền truy cập dịch vụ nội bộ."


def _post(api: httpx.Client, stub: support.StubServer, evaluation_id: str, **extra) -> httpx.Response:
    return api.post("/api/v1/evaluations", json=support.evaluation_task(stub, evaluation_id, **extra))


def _ensure_completed(api: httpx.Client, stub: support.StubServer, evaluation_id: str, **extra) -> dict:
    if api.get(f"/api/v1/evaluations/{evaluation_id}").status_code == 404:
        assert _post(api, stub, evaluation_id, **extra).status_code == 202
    record = support.wait_evaluation(api, evaluation_id)
    assert record["status"] == "completed", record
    return record


def test_itc_re_001_health_without_key(rhythm):
    response = httpx.get(f"{rhythm.base}/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_itc_re_002_internal_key_required(rhythm, spawn):
    no_header = httpx.get(f"{rhythm.base}/api/v1/evaluations/ev-1")
    assert no_header.status_code == 401 and no_header.json()["detail"] == UNAUTHORIZED
    wrong = httpx.get(f"{rhythm.base}/api/v1/evaluations/ev-1", headers={"Authorization": "Bearer wrong-key"})
    assert wrong.status_code == 401 and wrong.json()["detail"] == UNAUTHORIZED
    empty_key = spawn({"RHYTHM_SERVICE_INTERNAL_API_KEY": ""})
    # httpx refuses a header value with a trailing space, so the raw request goes through http.client.
    connection = http.client.HTTPConnection("127.0.0.1", empty_key.port, timeout=10)
    connection.request("GET", "/api/v1/evaluations/ev-1", headers={"Authorization": "Bearer "})
    response = connection.getresponse()
    assert response.status == 401 and json.loads(response.read())["detail"] == UNAUTHORIZED
    connection.close()


def test_itc_re_003_valid_request_is_scored(api, stub):
    response = _post(api, stub, "ev-1")
    assert response.status_code == 202
    assert response.json() == {"evaluationId": "ev-1", "status": "queued", "result": None, "error": None}
    record = support.wait_evaluation(api, "ev-1")
    assert record["status"] == "completed", record
    for key in ("overallScore", "pitchAccuracyPercent", "rhythmAccuracyPercent", "aiFeedback",
                "studentAudioQuality", "analysisArtifactUrl"):
        assert key in record["result"], key


def test_itc_re_004_same_evaluation_is_not_scored_twice(api, stub, db_url):
    first = _ensure_completed(api, stub, "ev-1")
    again = _post(api, stub, "ev-1")
    assert again.status_code == 202
    assert again.json()["status"] == "completed"
    assert again.json()["result"] == first["result"]
    rows = support.query(db_url, "SELECT COUNT(*), MAX(status) FROM rhythm_evaluations WHERE evaluation_id = 'ev-1'")
    assert rows == [(1, "completed")]


def test_itc_re_005_request_body_is_validated(api, stub):
    def send(**changes) -> httpx.Response:
        body = support.evaluation_task(stub, f"ev-val-{uuid.uuid4().hex[:6]}")
        for key, value in changes.items():
            if value is None:
                body.pop(key, None)
            else:
                body[key] = value
        return api.post("/api/v1/evaluations", json=body)

    def locations(response: httpx.Response) -> list[str]:
        return [str(part) for item in response.json()["detail"] for part in item["loc"]]

    def messages(response: httpx.Response) -> str:
        return " ".join(item["msg"] for item in response.json()["detail"])

    response = send(studentUrl=None)
    assert response.status_code == 422 and "studentUrl" in locations(response)
    response = send(templateUrl="ftp://a/b.wav")
    assert response.status_code == 422 and "templateUrl" in locations(response)
    response = send(attempt=0)
    assert response.status_code == 422 and "attempt" in locations(response)
    response = send(templateStartSec=2)
    assert response.status_code == 422 and "templateStartSec and templateEndSec must be provided together" in messages(response)
    response = send(templateStartSec=6, templateEndSec=2)
    assert response.status_code == 422 and "templateEndSec must be greater than templateStartSec" in messages(response)
    response = send(baselineEvaluationId="ev-1")
    assert response.status_code == 422 and "baselineEvaluationId requires a template range" in messages(response)


def test_itc_re_006_missing_student_file_fails(api, stub):
    url = stub.status_url(404, "ev-404.wav")
    assert _post(api, stub, "ev-404", studentUrl=url).status_code == 202
    record = support.wait_evaluation(api, "ev-404")
    # Round 1 decision: HTTP 404 is permanent, so the file is downloaded once (only 5xx/429 are retried)
    assert stub.state.hits["/status/404/ev-404.wav"] == 1, stub.state.hits
    assert record["status"] == "failed"
    assert record["error"] == {"code": "AUDIO_DOWNLOAD_FAILED", "message": "Storage trả về HTTP 404"}


def test_itc_re_007_silent_recording_is_rejected(api, stub):
    assert _post(api, stub, "ev-silent", student="silent.wav").status_code == 202
    record = support.wait_evaluation(api, "ev-silent")
    # Round 1 decision: a rejected recording is stored with status "failed" and its rejection code
    assert record["status"] == "failed", record
    assert record["error"] == {"code": "AUDIO_SILENT", "message": "Bản thu hoàn toàn im lặng. Vui lòng thu âm lại."}
    assert stub.state.hits["/media/silent.wav"] == 1


def test_itc_re_008_text_file_is_rejected(api, stub):
    assert _post(api, stub, "ev-text", student="fake.wav").status_code == 202
    record = support.wait_evaluation(api, "ev-text")
    assert record["status"] == "failed", record
    assert record["error"] == {"code": "AUDIO_DECODE_FAILED",
                               "message": "Không thể giải mã file audio. Vui lòng kiểm tra bản thu."}
    assert stub.state.hits["/media/fake.wav"] == 1


def test_itc_re_009_host_outside_allowlist_is_blocked(spawn, stub):
    service = spawn({"RHYTHM_SERVICE_ALLOWED_HOSTS": "localhost"})
    with service.client() as client:
        task = support.evaluation_task(stub, "ev-host", studentUrl="http://example.com/a.wav")
        task["templateUrl"] = task["templateUrl"].replace("127.0.0.1", "localhost")
        assert client.post("/api/v1/evaluations", json=task).status_code == 202
        record = support.wait_evaluation(client, "ev-host", timeout=60)
    assert record["status"] == "failed"
    assert record["error"] == {"code": "AUDIO_HOST_NOT_ALLOWED", "message": "Host audio không nằm trong allowlist"}


def test_itc_re_010_file_over_25_mb_is_refused(api, stub):
    assert _post(api, stub, "ev-big", student="big.wav").status_code == 202
    record = support.wait_evaluation(api, "ev-big")
    assert record["status"] == "failed"
    assert record["error"] == {"code": "AUDIO_TOO_LARGE", "message": "File audio vượt quá giới hạn kích thước"}


def _quality(api, stub, evaluation_id: str, student: str) -> dict:
    record = _ensure_completed(api, stub, evaluation_id, student=student)
    return record["result"]["studentAudioQuality"]


def test_itc_re_011_quiet_recording_warning(api, stub):
    quality = _quality(api, stub, "ev-quiet", "quiet.wav")
    assert quality["verdict"] == "WARN"
    assert {"code": "AUDIO_LOW_LEVEL", "message": "Âm lượng bản thu nhỏ, hãy kiểm tra khoảng cách tới micro."} in quality["warnings"]


def test_itc_re_012_mostly_silent_warning(api, stub):
    quality = _quality(api, stub, "ev-mostly-silent", "mostly-silent.wav")
    assert quality["verdict"] == "WARN"
    assert {"code": "AUDIO_MOSTLY_SILENT", "message": "Bản thu có nhiều khoảng lặng."} in quality["warnings"]


def test_itc_re_013_clipping_warning(api, stub):
    quality = _quality(api, stub, "ev-clipped", "clipped.wav")
    assert quality["verdict"] == "WARN"
    assert {"code": "AUDIO_POSSIBLE_CLIPPING", "message": "Bản thu có dấu hiệu clipping, hãy giảm mức thu âm."} in quality["warnings"]


def test_itc_re_014_clean_recording_passes(api, stub):
    quality = _quality(api, stub, "ev-clean", "student.wav")
    assert quality["verdict"] == "PASS"
    assert quality["warnings"] == [] and quality["rejections"] == []
    assert quality["policyVersion"] == "audio-quality-v1"
    assert quality["audio"] and quality["metrics"]


def test_itc_re_015_unknown_evaluation(api):
    for path in ("/api/v1/evaluations/ev-none", "/api/v1/evaluations/ev-none/analysis"):
        response = api.get(path)
        assert response.status_code == 404 and response.json()["detail"] == "Evaluation not found"


def test_itc_re_016_pitch_data_file(api, stub):
    _ensure_completed(api, stub, "ev-1")
    response = api.get("/api/v1/evaluations/ev-1/analysis")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    document = response.json()
    assert document["schemaVersion"] == 1 and document["evaluationId"] == "ev-1"
    assert "teacherF0Hz" in document["pitch"] and "studentF0Hz" in document["pitch"]
    assert "rhythm" in document and "alignment" in document


def test_itc_re_017_pitch_data_not_ready_or_lost(api, stub, db_url, work_dir: Path):
    # Step 2 uses its own completed evaluation so that ev-1 keeps its file for ITC-RE-020.
    _ensure_completed(api, stub, "ev-lost")
    assert _post(api, stub, "ev-long", student="long.wav", template="long.wav").status_code == 202
    not_ready = api.get("/api/v1/evaluations/ev-long/analysis")
    assert not_ready.status_code == 409
    assert not_ready.json()["detail"] == {"code": "EVALUATION_NOT_READY", "message": "Kết quả đánh giá chưa sẵn sàng."}
    key = support.query(db_url, "SELECT analysis_artifact_key FROM rhythm_evaluations WHERE evaluation_id = 'ev-lost'")[0][0]
    (work_dir / "artifacts" / key).unlink()
    lost = api.get("/api/v1/evaluations/ev-lost/analysis")
    assert lost.status_code == 503
    assert lost.json()["detail"] == {"code": "ARTIFACT_UNAVAILABLE", "message": "Chưa thể đọc file phân tích của bản thu."}
    support.wait_evaluation(api, "ev-long", timeout=300)


def test_itc_re_018_same_file_scores_close_to_100(api, stub):
    result = _ensure_completed(api, stub, "ev-same", student="template.wav")["result"]
    assert result["pitchAccuracyPercent"] >= 95, result["pitchAccuracyPercent"]
    assert result["rhythmAccuracyPercent"] >= 95, result["rhythmAccuracyPercent"]
    assert abs(result["teacherTempoBpm"] - result["studentTempoBpm"]) <= 1


def test_itc_re_019_part_of_the_song(api, stub):
    result = _ensure_completed(api, stub, "ev-part", student="part.wav", templateStartSec=2, templateEndSec=6.5)["result"]
    assert result["segment"] == {"startSec": 2, "endSec": 6.5}
    assert result.get("pitch")
    assert result["baseline"] is None
    assert "analysisArtifactUrl" not in result


def test_itc_re_020_part_compared_with_baseline(api, stub):
    _ensure_completed(api, stub, "ev-1")
    result = _ensure_completed(api, stub, "ev-part-base", student="part.wav", templateStartSec=2,
                               templateEndSec=6.5, baselineEvaluationId="ev-1")["result"]
    baseline = result["baseline"]
    assert baseline is not None
    for key in ("overallScore", "pitchAccuracyPercent", "rhythmAccuracyPercent"):
        assert baseline.get(key) is not None, key
    missing = _ensure_completed(api, stub, "ev-part-nobase", student="part.wav", templateStartSec=2,
                                templateEndSec=6.5, baselineEvaluationId="ev-none")["result"]
    assert missing["baseline"] is None


def test_itc_re_021_template_analysis_is_reused(api, stub):
    analysis_id = str(uuid.uuid4())
    support.publish(support.TEMPLATE_QUEUE, support.template_task(stub, analysis_id=analysis_id))
    assert support.wait_template(api, analysis_id)["status"] == "completed"
    _ensure_completed(api, stub, "ev-cache", templateAnalysisId=analysis_id)
    document = api.get("/api/v1/evaluations/ev-cache/analysis").json()
    assert document["templateAnalysisId"] == analysis_id
    other_range = {**support.TRANH, "fminHz": 100.0}
    _ensure_completed(api, stub, "ev-cache-other", templateAnalysisId=analysis_id, instrument=other_range)
    document = api.get("/api/v1/evaluations/ev-cache-other/analysis").json()
    assert document["templateAnalysisId"] is None
