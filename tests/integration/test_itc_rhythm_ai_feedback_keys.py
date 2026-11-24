"""Sheet "Rhythm AI Feedback & Keys" (ITC-RK-001 .. ITC-RK-020).

Gemini is the local stub (GOOGLE_GEMINI_BASE_URL). The stub answers by key value: keys listed in
`stub.state.gemini_modes` answer 401/429/503 or slowly, every other key answers with a Vietnamese feedback."""

from __future__ import annotations

import base64
import os
import time
import uuid
from datetime import datetime, timezone

import httpx

from . import support

PATH = "/api/v1/admin/llm-keys"
K1 = "it-gemini-key-one-0001"
K2 = "it-gemini-key-two-0002"
K3 = "it-gemini-key-three-0003"
REVOKED = "it-gemini-key-revoked-0401"
QUOTA = "it-gemini-key-quota-0429"
FAILED_ERROR = {"code": "AI_FEEDBACK_FAILED", "message": "Chưa thể tạo nhận xét AI. Vui lòng thử lại."}


def _keys(api: httpx.Client) -> dict[str, dict]:
    return {item["name"]: item for item in api.get(PATH).json()["items"]}


def _ensure_key(api: httpx.Client, name: str, value: str) -> dict:
    existing = _keys(api).get(name)
    if existing:
        return existing
    response = api.post(PATH, json={"name": name, "key": value})
    assert response.status_code == 201, response.text
    return response.json()


def _only_active(api: httpx.Client, *names: str) -> None:
    for name, item in _keys(api).items():
        wanted = "active" if name in names else "disabled"
        if item["status"] != wanted and item["status"] != "invalid":
            assert api.patch(f"{PATH}/{item['id']}", json={"status": wanted}).status_code == 200


def _evaluate(client: httpx.Client, stub: support.StubServer, prefix: str) -> dict:
    evaluation_id = f"{prefix}-{uuid.uuid4().hex[:6]}"
    support.publish(support.EVALUATE_QUEUE, support.evaluation_task(stub, evaluation_id, callbackUrl=stub.callback_url))
    record = support.wait_evaluation(client, evaluation_id)
    time.sleep(1)
    return record


def _modes(stub: support.StubServer) -> None:
    stub.state.gemini_modes.update({REVOKED: "401", QUOTA: "429"})


def test_itc_rk_001_internal_key_required(rhythm):
    detail = "Không có quyền truy cập dịch vụ nội bộ."
    response = httpx.get(f"{rhythm.base}{PATH}")
    assert response.status_code == 401 and response.json()["detail"] == detail
    response = httpx.post(f"{rhythm.base}{PATH}", headers={"Authorization": "Bearer wrong"}, json={"name": "x", "key": K1})
    assert response.status_code == 401 and response.json()["detail"] == detail


def test_itc_rk_002_new_key_saved_and_hidden(api, db_url):
    response = api.post(PATH, json={"name": "Gemini 1", "key": K1})
    assert response.status_code == 201
    body = response.json()
    assert body["provider"] == "gemini" and body["status"] == "active"
    assert body["successCount"] == 0 and body["failCount"] == 0
    assert K1 not in response.text
    listing = api.get(PATH)
    assert "Gemini 1" in _keys(api) and K1 not in listing.text
    rows = support.query(db_url, "SELECT key_ciphertext FROM llm_api_keys WHERE name = 'Gemini 1'")
    assert bytes(rows[0][0]) != K1.encode()


def test_itc_rk_003_only_gemini_provider(api):
    created = api.post(PATH, json={"name": "Gemini 3", "key": K3, "provider": "gemini"})
    assert created.status_code == 201 and created.json()["provider"] == "gemini"
    openrouter = api.post(PATH, json={"name": "OR 1", "key": "it-openrouter-key-0001", "provider": "openrouter"})
    openai = api.post(PATH, json={"name": "OA 1", "key": "it-openai-key-00000001", "provider": "openai"})
    for response in (openrouter, openai):  # a key created by mistake must not stay in the pool
        if response.status_code == 201:
            api.delete(f"{PATH}/{response.json()['id']}")
    assert openrouter.status_code == 422, openrouter.text
    assert openai.status_code == 422


def test_itc_rk_004_input_validated_and_key_hidden(api):
    responses = [
        api.post(PATH, json={"name": "Short", "key": "short-key"}),
        api.post(PATH, json={"name": "   ", "key": "it-valid-key-000004"}),
        api.post(PATH, json={"name": "Extra", "key": "it-valid-key-000005", "note": "x"}),
    ]
    for response in responses:
        assert response.status_code == 422
        for item in response.json()["detail"]:
            assert set(item) == {"loc", "msg", "type"}
    assert "short-key" not in responses[0].text
    assert "it-valid-key-000004" not in responses[1].text and "it-valid-key-000005" not in responses[2].text


def test_itc_rk_005_duplicate_name_and_value(api):
    _ensure_key(api, "Gemini 1", K1)
    name = api.post(PATH, json={"name": "Gemini 1", "key": "it-other-key-000005"})
    assert name.status_code == 409
    assert name.json()["detail"] == {"code": "LLM_KEY_DUPLICATE_NAME", "message": "Tên API key đã tồn tại."}
    value = api.post(PATH, json={"name": "Gemini copy", "key": K1})
    assert value.status_code == 409
    assert value.json()["detail"] == {"code": "LLM_KEY_DUPLICATE_VALUE", "message": "API key này đã tồn tại."}


def test_itc_rk_006_no_encryption_secret(spawn, db_url):
    before = support.query(db_url, "SELECT COUNT(*) FROM llm_api_keys")[0][0]
    service = spawn({"RHYTHM_SERVICE_KEY_ENCRYPTION_SECRET": ""})
    with service.client() as client:
        response = client.post(PATH, json={"name": "No secret", "key": "it-no-secret-key-0006"})
    assert response.status_code == 503
    assert response.json()["detail"] == {"code": "LLM_KEY_ENCRYPTION_UNAVAILABLE", "message": "Chưa cấu hình khóa mã hóa API key."}
    assert support.query(db_url, "SELECT COUNT(*) FROM llm_api_keys")[0][0] == before


def test_itc_rk_007_turn_key_off_and_on(api):
    key = _ensure_key(api, "Gemini 1", K1)
    url = f"{PATH}/{key['id']}"
    disabled = api.patch(url, json={"status": "disabled"})
    assert disabled.status_code == 200 and disabled.json()["status"] == "disabled"
    active = api.patch(url, json={"status": "active"})
    assert active.status_code == 200 and active.json()["status"] == "active"
    assert api.patch(url, json={"status": "invalid"}).status_code == 422
    empty = api.patch(url, json={})
    assert empty.status_code == 422 and "Cần ít nhất một trường để cập nhật." in empty.text


def test_itc_rk_008_new_value_reactivates_bad_key(api, stub):
    _modes(stub)
    bad = _ensure_key(api, "Gemini bad", "it-gemini-key-bad-0401")
    stub.state.gemini_modes["it-gemini-key-bad-0401"] = "401"
    _only_active(api, "Gemini bad")
    _evaluate(api, stub, "rk8")
    assert _keys(api)["Gemini bad"]["status"] == "invalid"
    response = api.patch(f"{PATH}/{bad['id']}", json={"key": K2})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "active" and body["cooldownUntil"] is None and body["lastErrorCode"] is None


def test_itc_rk_009_rename_to_used_name(api):
    _ensure_key(api, "Gemini 1", K1)
    gemini3 = _ensure_key(api, "Gemini 3", K3)
    response = api.patch(f"{PATH}/{gemini3['id']}", json={"name": "Gemini 1"})
    assert response.status_code == 409
    assert response.json()["detail"] == {"code": "LLM_KEY_DUPLICATE_NAME", "message": "Tên API key đã tồn tại."}
    assert "Gemini 3" in _keys(api)


def test_itc_rk_010_delete_and_unknown_ids(api):
    test_key = _ensure_key(api, "Delete me", "it-delete-me-key-0010")
    assert api.delete(f"{PATH}/{test_key['id']}").status_code == 204
    assert "Delete me" not in _keys(api)
    not_found = {"code": "LLM_KEY_NOT_FOUND", "message": "Không tìm thấy API key."}
    again = api.delete(f"{PATH}/{test_key['id']}")
    assert again.status_code == 404 and again.json()["detail"] == not_found
    unknown = api.patch(f"{PATH}/no-such-id", json={"status": "active"})
    assert unknown.status_code == 404 and unknown.json()["detail"] == not_found


def test_itc_rk_011_working_key_used(api, stub):
    _ensure_key(api, "Gemini 1", K1)
    _only_active(api, "Gemini 1")
    before = _keys(api)["Gemini 1"]
    record = _evaluate(api, stub, "rk11")
    assert record["result"]["aiFeedback"] == support.FEEDBACK_TEXT
    after = _keys(api)["Gemini 1"]
    assert after["successCount"] == before["successCount"] + 1
    assert after["lastUsedAt"] and after["lastErrorCode"] is None


def test_itc_rk_012_keys_used_in_turn(api, stub):
    _ensure_key(api, "Gemini 1", K1)
    _ensure_key(api, "Gemini 3", K3)
    _only_active(api, "Gemini 1", "Gemini 3")
    before = _keys(api)
    _evaluate(api, stub, "rk12a")
    _evaluate(api, stub, "rk12b")
    after = _keys(api)
    for name in ("Gemini 1", "Gemini 3"):
        assert after[name]["successCount"] == before[name]["successCount"] + 1, name


def test_itc_rk_013_quota_key_rests_60_seconds(api, stub):
    _modes(stub)
    _ensure_key(api, "Gemini 1", K1)
    _ensure_key(api, "Gemini quota", QUOTA)
    _only_active(api, "Gemini 1", "Gemini quota")
    before = _keys(api)["Gemini quota"]
    started = datetime.now(timezone.utc)
    record = _evaluate(api, stub, "rk13")
    assert record["status"] == "completed" and record["result"]["aiFeedback"]
    assert stub.state.gemini_calls[QUOTA] >= 1 and stub.state.gemini_calls[K1] >= 1
    after = _keys(api)["Gemini quota"]
    assert after["failCount"] == before["failCount"] + 1 and after["lastErrorCode"] == "HTTP_429"
    cooldown = datetime.fromisoformat(after["cooldownUntil"].replace("Z", "+00:00"))
    assert 45 <= (cooldown - started).total_seconds() <= 90
    assert after["status"] == "active"


def test_itc_rk_014_rejected_key_marked_invalid(api, stub):
    _modes(stub)
    _ensure_key(api, "Gemini 1", K1)
    _ensure_key(api, "Gemini revoked", REVOKED)
    _only_active(api, "Gemini revoked")
    _evaluate(api, stub, "rk14a")
    revoked = _keys(api)["Gemini revoked"]
    assert revoked["status"] == "invalid" and revoked["lastErrorCode"] == "HTTP_401"
    _only_active(api, "Gemini 1")
    before_success = _keys(api)["Gemini 1"]["successCount"]
    calls_before = stub.state.gemini_calls[REVOKED]
    record = _evaluate(api, stub, "rk14b")
    assert record["status"] == "completed"
    assert _keys(api)["Gemini 1"]["successCount"] == before_success + 1
    assert stub.state.gemini_calls[REVOKED] == calls_before
    assert _keys(api)["Gemini revoked"]["failCount"] == revoked["failCount"]


def test_itc_rk_015_gemini_down_fails_without_retry(api, stub):
    _ensure_key(api, "Gemini 1", K1)
    _ensure_key(api, "Gemini 3", K3)
    _only_active(api, "Gemini 1", "Gemini 3")
    stub.state.gemini_default = "503"
    record = _evaluate(api, stub, "rk15")
    bodies = [item["body"] for item in stub.state.callbacks_for(record["evaluationId"])]
    failed = [body for body in bodies if body["status"] == "failed"]
    assert failed and failed[-1]["job"] == "feedback" and failed[-1]["code"] == "AI_FEEDBACK_FAILED"
    assert record["status"] == "failed" and record["error"] == FAILED_ERROR
    assert [body for body in bodies if body["job"] == "feedback" and body["status"] == "started"].__len__() == 1


def test_itc_rk_016_settings_key_used_when_pool_empty(api, stub, spawn):
    _only_active(api)
    service = spawn({"RHYTHM_SERVICE_GEMINI_API_KEY": "it-env-gemini-key-0016"})
    with service.client() as client:
        record = _evaluate(client, stub, "rk16")
    assert record["status"] == "completed" and record["result"]["aiFeedback"] == support.FEEDBACK_TEXT
    assert stub.state.gemini_calls["it-env-gemini-key-0016"] == 1


def test_itc_rk_017_default_feedback_without_any_key(api, stub, spawn):
    _only_active(api)
    service = spawn({"RHYTHM_SERVICE_GEMINI_API_KEY": ""})
    with service.client() as client:
        record = _evaluate(client, stub, "rk17")
    assert record["status"] == "completed"
    assert record["result"]["overallScore"] is not None
    assert record["result"]["aiFeedback"] == support.FALLBACK_FEEDBACK
    assert sum(stub.state.gemini_calls.values()) == 0


def test_itc_rk_018_every_key_fails(api, stub):
    stub.state.gemini_modes.update({"it-gemini-key-bad2-0401": "401", "it-gemini-key-quota2-0429": "429"})
    _ensure_key(api, "Gemini bad 2", "it-gemini-key-bad2-0401")
    _ensure_key(api, "Gemini quota 2", "it-gemini-key-quota2-0429")
    _only_active(api, "Gemini bad 2", "Gemini quota 2")
    record = _evaluate(api, stub, "rk18")
    failed = [item["body"] for item in stub.state.callbacks_for(record["evaluationId"]) if item["body"]["status"] == "failed"]
    assert failed and failed[-1]["job"] == "feedback" and failed[-1]["code"] == "AI_FEEDBACK_FAILED"
    assert record["status"] == "failed" and record["error"] == FAILED_ERROR


def test_itc_rk_019_slow_provider_times_out(api, stub, spawn):
    _ensure_key(api, "Gemini slow", "it-gemini-key-slow-0019")
    stub.state.gemini_modes["it-gemini-key-slow-0019"] = "slow:5"
    _only_active(api, "Gemini slow")
    service = spawn({"RHYTHM_SERVICE_GEMINI_TIMEOUT_SECONDS": "1"})
    with service.client() as client:
        record = _evaluate(client, stub, "rk19")
    assert record["status"] == "failed"
    assert record["error"] == {"code": "AI_FEEDBACK_TIMEOUT", "message": "Dịch vụ AI phản hồi quá thời gian chờ. Vui lòng thử lại."}


def test_itc_rk_020_key_that_cannot_be_decrypted_is_skipped(api, stub, spawn):
    _ensure_key(api, "Gemini 1", K1)
    _only_active(api, "Gemini 1")
    new_secret = base64.b64encode(os.urandom(32)).decode()
    service = spawn({"RHYTHM_SERVICE_KEY_ENCRYPTION_SECRET": new_secret})
    with service.client() as client:
        created = client.post(PATH, json={"name": "Gemini new secret", "key": "it-gemini-key-new-0020"})
        assert created.status_code == 201
        record = _evaluate(client, stub, "rk20")
    assert stub.state.gemini_calls["it-gemini-key-new-0020"] == 1
    assert stub.state.gemini_calls[K1] == 0
    assert record["status"] == "completed"
    assert "cannot decrypt stored value" in service.log(), "the worker log has no line about the skipped keys"
