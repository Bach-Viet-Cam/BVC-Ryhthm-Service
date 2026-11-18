import base64
import json

import httpx
import pytest
from sqlalchemy import select

from app.models.llm_api_key import LlmApiKey
from test_template_api import setup as api_setup

SECRET = base64.b64encode(bytes(range(32))).decode()
URL = "/api/v1/admin/llm-keys"
KEY = "AIzaSy-test-key-0123456789"


def make(tmp_path, monkeypatch, secret=SECRET):
    service, sessions, app = api_setup(tmp_path, monkeypatch)
    service.settings.key_encryption_secret = secret
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                               headers={"Authorization": "Bearer secret"})
    return client, sessions


def rows(sessions):
    with sessions() as session:
        return list(session.scalars(select(LlmApiKey)))


@pytest.mark.asyncio
async def test_requires_internal_bearer(tmp_path, monkeypatch):
    client, _ = make(tmp_path, monkeypatch)
    async with client:
        for method, url in (("GET", URL), ("POST", URL), ("PATCH", URL + "/x"), ("DELETE", URL + "/x")):
            response = await client.request(method, url, json={"name": "a", "key": KEY}, headers={"Authorization": ""})
            assert response.status_code == 401
        assert (await client.request("GET", URL, headers={"Authorization": "Bearer wrong"})).status_code == 401


@pytest.mark.asyncio
async def test_create_list_update_delete_flow(tmp_path, monkeypatch):
    client, _ = make(tmp_path, monkeypatch)
    async with client:
        created = await client.post(URL, json={"name": "  main  ", "key": f"  {KEY}  "})
        assert created.status_code == 201
        body = created.json()
        assert body["name"] == "main" and body["status"] == "active"
        assert body["provider"] == "gemini"
        assert set(body) == {"id", "name", "provider", "status", "cooldownUntil", "lastUsedAt", "lastErrorCode",
                             "successCount", "failCount", "createdAt"}
        listed = await client.get(URL)
        assert listed.status_code == 200 and [i["id"] for i in listed.json()["items"]] == [body["id"]]
        assert (await client.delete(f"{URL}/{body['id']}")).status_code == 204
        assert (await client.get(URL)).json() == {"items": []}


@pytest.mark.asyncio
async def test_create_with_provider_and_reject_unknown_provider(tmp_path, monkeypatch):
    client, _ = make(tmp_path, monkeypatch)
    async with client:
        created = await client.post(URL, json={"name": "or", "key": KEY, "provider": "openrouter"})
        assert created.status_code == 201 and created.json()["provider"] == "openrouter"
        bad = await client.post(URL, json={"name": "x", "key": KEY + "2", "provider": "nope"})
        assert bad.status_code == 422


@pytest.mark.asyncio
async def test_response_never_contains_key_material(tmp_path, monkeypatch):
    client, sessions = make(tmp_path, monkeypatch)
    async with client:
        created = await client.post(URL, json={"name": "main", "key": KEY})
        listed = await client.get(URL)
        patched = await client.patch(f"{URL}/{created.json()['id']}", json={"name": "renamed"})
    row = rows(sessions)[0]
    forbidden = [KEY, KEY[-4:], row.key_hmac, base64.b64encode(row.key_ciphertext).decode(),
                 row.key_ciphertext.hex(), base64.b64encode(row.key_nonce).decode(), row.key_nonce.hex()]
    for response in (created, listed, patched):
        text = response.text
        for value in forbidden:
            assert value not in text
        assert "ciphertext" not in text.lower() and "hmac" not in text.lower()


@pytest.mark.asyncio
async def test_duplicate_name_409(tmp_path, monkeypatch):
    client, _ = make(tmp_path, monkeypatch)
    async with client:
        await client.post(URL, json={"name": "main", "key": KEY})
        response = await client.post(URL, json={"name": "main", "key": KEY + "other"})
        assert response.status_code == 409 and response.json()["detail"]["code"] == "LLM_KEY_DUPLICATE_NAME"


@pytest.mark.asyncio
async def test_duplicate_key_409_even_with_surrounding_whitespace(tmp_path, monkeypatch):
    client, sessions = make(tmp_path, monkeypatch)
    async with client:
        await client.post(URL, json={"name": "a", "key": KEY})
        response = await client.post(URL, json={"name": "b", "key": f"  {KEY}\n"})
        assert response.status_code == 409 and response.json()["detail"]["code"] == "LLM_KEY_DUPLICATE_VALUE"
    assert len(rows(sessions)) == 1


@pytest.mark.asyncio
async def test_patch_rename_and_disable(tmp_path, monkeypatch):
    client, _ = make(tmp_path, monkeypatch)
    async with client:
        key_id = (await client.post(URL, json={"name": "a", "key": KEY})).json()["id"]
        response = await client.patch(f"{URL}/{key_id}", json={"name": " b ", "status": "disabled"})
        assert response.status_code == 200
        assert response.json()["name"] == "b" and response.json()["status"] == "disabled"
        assert (await client.patch(f"{URL}/{key_id}", json={"status": "invalid"})).status_code == 422
        assert (await client.patch(f"{URL}/missing", json={"name": "z"})).status_code == 404
        await client.post(URL, json={"name": "c", "key": KEY + "2"})
        conflict = await client.patch(f"{URL}/{key_id}", json={"name": "c"})
        assert conflict.status_code == 409 and conflict.json()["detail"]["code"] == "LLM_KEY_DUPLICATE_NAME"
        conflict = await client.patch(f"{URL}/{key_id}", json={"key": KEY + "2"})
        assert conflict.status_code == 409 and conflict.json()["detail"]["code"] == "LLM_KEY_DUPLICATE_VALUE"


@pytest.mark.asyncio
async def test_patch_with_key_reactivates_invalid_key(tmp_path, monkeypatch):
    client, sessions = make(tmp_path, monkeypatch)
    async with client:
        key_id = (await client.post(URL, json={"name": "a", "key": KEY})).json()["id"]
        with sessions() as session:
            row = session.get(LlmApiKey, key_id)
            row.status, row.last_error_code = "invalid", "API_KEY_INVALID"
            old_hmac = row.key_hmac
            session.commit()
        response = await client.patch(f"{URL}/{key_id}", json={"key": KEY + "-new"})
        assert response.status_code == 200
        assert response.json()["status"] == "active" and response.json()["lastErrorCode"] is None
    assert rows(sessions)[0].key_hmac != old_hmac


@pytest.mark.asyncio
async def test_patch_empty_body_422(tmp_path, monkeypatch):
    client, _ = make(tmp_path, monkeypatch)
    async with client:
        key_id = (await client.post(URL, json={"name": "a", "key": KEY})).json()["id"]
        assert (await client.patch(f"{URL}/{key_id}", json={})).status_code == 422


@pytest.mark.asyncio
async def test_validation_error_does_not_echo_submitted_key(tmp_path, monkeypatch):
    client, _ = make(tmp_path, monkeypatch)
    async with client:
        response = await client.post(URL, json={"name": "a", "key": "short-key"})
        assert response.status_code == 422 and "short-key" not in response.text
        for entry in response.json()["detail"]:
            assert set(entry) == {"loc", "msg", "type"}
        patched = await client.patch(f"{URL}/x", json={"key": "short-key", "status": "bogus-secret"})
        assert patched.status_code == 422 and "short-key" not in patched.text and "bogus-secret" not in patched.text
        # Other paths keep FastAPI's default body.
        other = await client.get("/api/v1/template-analyses/lookup")
        assert other.status_code == 422 and "input" in other.json()["detail"][0]


@pytest.mark.asyncio
@pytest.mark.parametrize("secret", ["", "not-base64!!", base64.b64encode(b"short").decode()])
async def test_missing_encryption_secret_returns_503_and_stores_nothing(tmp_path, monkeypatch, secret):
    client, sessions = make(tmp_path, monkeypatch, secret=secret)
    async with client:
        response = await client.post(URL, json={"name": "a", "key": KEY})
        assert response.status_code == 503 and response.json()["detail"]["code"] == "LLM_KEY_ENCRYPTION_UNAVAILABLE"
        assert KEY not in response.text
    assert rows(sessions) == []


@pytest.mark.asyncio
async def test_patch_key_without_secret_returns_503_and_keeps_row(tmp_path, monkeypatch):
    client, sessions = make(tmp_path, monkeypatch)
    async with client:
        key_id = (await client.post(URL, json={"name": "a", "key": KEY})).json()["id"]
        before = rows(sessions)[0].key_hmac
        monkeypatch.setattr("app.config.get_settings", lambda: type("S", (), {"key_encryption_secret": ""})())
        response = await client.patch(f"{URL}/{key_id}", json={"key": KEY + "-new", "name": "z"})
        assert response.status_code == 503
    row = rows(sessions)[0]
    assert row.key_hmac == before and row.name == "a"


@pytest.mark.asyncio
async def test_delete_unknown_404(tmp_path, monkeypatch):
    client, _ = make(tmp_path, monkeypatch)
    async with client:
        response = await client.delete(f"{URL}/missing")
        assert response.status_code == 404 and response.json()["detail"]["code"] == "LLM_KEY_NOT_FOUND"


@pytest.mark.asyncio
async def test_stored_row_is_not_plaintext(tmp_path, monkeypatch):
    client, sessions = make(tmp_path, monkeypatch)
    async with client:
        await client.post(URL, json={"name": "a", "key": KEY})
    row = rows(sessions)[0]
    blob = json.dumps([row.name, row.key_hmac, row.key_ciphertext.hex(), row.key_nonce.hex()])
    assert KEY.encode() not in row.key_ciphertext and KEY not in blob
    assert row.key_hmac != KEY and len(row.key_nonce) == 12
