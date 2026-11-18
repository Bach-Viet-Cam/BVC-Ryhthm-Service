import asyncio
import logging
from types import SimpleNamespace

import pytest
from google.genai import errors as genai_errors

from app.feedback import gemini_feedback as feedback

SECRET_KEY = "AIza-SECRET-123"


def _api_error(code: int) -> genai_errors.APIError:
    return genai_errors.APIError(code, {"error": {"message": f"boom {code}", "status": "X"}})


def _settings(gemini_api_key: str = "", timeout: float = 5.0) -> SimpleNamespace:
    return SimpleNamespace(gemini_api_key=gemini_api_key, gemini_timeout_seconds=timeout,
                           gemini_model="gemini-test", key_encryption_secret="")


class FakePool:
    """Stands in for _acquire_key / _record_outcome and records what the feedback flow asked for."""

    def __init__(self, keys: list[tuple[str, str]]):
        self.keys = [feedback.PooledKey(id=key_id, api_key=api_key) for key_id, api_key in keys]
        self.acquire_calls: list[set[str]] = []
        self.outcomes: list[tuple[str, str | None, str | None]] = []

    async def acquire(self, exclude_ids: set[str]):
        self.acquire_calls.append(set(exclude_ids))
        for key in self.keys:
            if key.id not in exclude_ids:
                return key
        return None

    async def record(self, key_id, error_code, kind):
        self.outcomes.append((key_id, error_code, kind))


def _install(monkeypatch, pool: FakePool, generate, settings: SimpleNamespace | None = None) -> list[str | None]:
    used_keys: list[str | None] = []

    def fake_sync(result, api_key=None):
        used_keys.append(api_key)
        return generate(api_key)

    monkeypatch.setattr(feedback, "get_settings", lambda: settings or _settings())
    monkeypatch.setattr(feedback, "_acquire_key", pool.acquire)
    monkeypatch.setattr(feedback, "_record_outcome", pool.record)
    monkeypatch.setattr(feedback, "_generate_feedback_sync", fake_sync)
    return used_keys


@pytest.mark.asyncio
async def test_uses_pool_key_and_records_success(monkeypatch):
    pool = FakePool([("k1", "key-one")])
    used = _install(monkeypatch, pool, lambda api_key: "Nhận xét tốt")

    assert await feedback.generate_feedback({"overallScore": 80}) == "Nhận xét tốt"
    assert used == ["key-one"]
    assert pool.outcomes == [("k1", None, None)]


@pytest.mark.asyncio
async def test_quota_error_cools_down_and_switches_to_next_key(monkeypatch):
    pool = FakePool([("k1", "key-one"), ("k2", "key-two")])

    def generate(api_key):
        if api_key == "key-one":
            raise _api_error(429)
        return "ok"

    used = _install(monkeypatch, pool, generate)
    assert await feedback.generate_feedback({}) == "ok"
    assert used == ["key-one", "key-two"]
    assert pool.outcomes[0][0] == "k1" and pool.outcomes[0][2] == "quota"
    assert pool.outcomes[1] == ("k2", None, None)


@pytest.mark.asyncio
async def test_invalid_key_error_marks_invalid_and_switches(monkeypatch):
    pool = FakePool([("k1", "key-one"), ("k2", "key-two")])

    def generate(api_key):
        if api_key == "key-one":
            raise _api_error(401)
        return "ok"

    used = _install(monkeypatch, pool, generate)
    assert await feedback.generate_feedback({}) == "ok"
    assert used == ["key-one", "key-two"]
    assert pool.outcomes[0][0] == "k1" and pool.outcomes[0][2] == "invalid"
    assert pool.outcomes[1] == ("k2", None, None)


@pytest.mark.asyncio
async def test_other_error_switches_without_locking_key(monkeypatch):
    pool = FakePool([("k1", "key-one"), ("k2", "key-two")])

    def generate(api_key):
        if api_key == "key-one":
            raise _api_error(500)
        return "ok"

    used = _install(monkeypatch, pool, generate)
    assert await feedback.generate_feedback({}) == "ok"
    assert used == ["key-one", "key-two"]
    assert pool.outcomes[0][0] == "k1" and pool.outcomes[0][2] == "other"


@pytest.mark.asyncio
async def test_never_reuses_a_key_within_one_call(monkeypatch):
    pool = FakePool([("k1", "a"), ("k2", "b"), ("k3", "c")])

    def generate(api_key):
        if api_key != "c":
            raise RuntimeError("provider down")
        return "ok"

    used = _install(monkeypatch, pool, generate)
    assert await feedback.generate_feedback({}) == "ok"
    assert used == ["a", "b", "c"]
    assert pool.acquire_calls == [set(), {"k1"}, {"k1", "k2"}]


@pytest.mark.asyncio
async def test_stops_after_three_attempts_and_raises_failed(monkeypatch):
    pool = FakePool([(f"k{i}", f"key-{i}") for i in range(5)])

    def generate(api_key):
        raise RuntimeError("provider down")

    used = _install(monkeypatch, pool, generate, _settings(gemini_api_key="env-key"))
    with pytest.raises(feedback.FeedbackGenerationError) as caught:
        await feedback.generate_feedback({})
    assert caught.value.code == "AI_FEEDBACK_FAILED"
    assert str(caught.value) == "Chưa thể tạo nhận xét AI. Vui lòng thử lại."
    assert used == ["key-0", "key-1", "key-2"]
    assert len(pool.outcomes) == 3


@pytest.mark.asyncio
async def test_all_pool_keys_fail_before_three_attempts_raises_failed_without_env(monkeypatch):
    pool = FakePool([("k1", "key-one")])
    used = _install(monkeypatch, pool, lambda api_key: (_ for _ in ()).throw(_api_error(429)),
                    _settings(gemini_api_key="env-key"))
    with pytest.raises(feedback.FeedbackGenerationError) as caught:
        await feedback.generate_feedback({})
    assert caught.value.code == "AI_FEEDBACK_FAILED"
    assert used == ["key-one"]


@pytest.mark.asyncio
async def test_pool_unavailable_first_acquire_falls_back_to_env_key(monkeypatch):
    pool = FakePool([])
    used = _install(monkeypatch, pool, lambda api_key: "từ env", _settings(gemini_api_key="env-key"))

    assert await feedback.generate_feedback({}) == "từ env"
    assert used == ["env-key"]
    assert pool.outcomes == []


@pytest.mark.asyncio
async def test_no_pool_no_env_key_returns_fallback_feedback(monkeypatch):
    pool = FakePool([])
    used = _install(monkeypatch, pool, lambda api_key: "never")

    assert await feedback.generate_feedback({}) == feedback.FALLBACK_FEEDBACK
    assert used == []


@pytest.mark.asyncio
async def test_timeout_raises_ai_feedback_timeout(monkeypatch):
    pool = FakePool([("k1", "key-one")])

    def generate(api_key):
        import time
        time.sleep(0.3)
        return "late"

    _install(monkeypatch, pool, generate, _settings(timeout=0.05))
    with pytest.raises(feedback.FeedbackGenerationError) as caught:
        await feedback.generate_feedback({})
    assert caught.value.code == "AI_FEEDBACK_TIMEOUT"


@pytest.mark.asyncio
async def test_error_message_and_logs_never_contain_key(monkeypatch, caplog):
    pool = FakePool([("k1", SECRET_KEY), ("k2", SECRET_KEY + "-2")])

    def generate(api_key):
        raise RuntimeError(f"request failed for key {api_key}")

    _install(monkeypatch, pool, generate)
    caplog.set_level(logging.DEBUG)
    with pytest.raises(feedback.FeedbackGenerationError) as caught:
        await feedback.generate_feedback({})
    assert SECRET_KEY not in str(caught.value)
    assert SECRET_KEY not in caplog.text
    assert all(SECRET_KEY not in (code or "") for _, code, _ in pool.outcomes)


@pytest.mark.parametrize("error, expected", [
    (_api_error(429), "quota"),
    (_api_error(401), "invalid"),
    (_api_error(403), "invalid"),
    (_api_error(500), "other"),
    (ValueError("bad"), "other"),
])
def test_classify_error(error, expected):
    assert feedback._classify_error(error) == expected


# --- _acquire_key against the real repository/cipher path (DB session replaced by a fake) ---

class _FakeRow(SimpleNamespace):
    pass


@pytest.mark.asyncio
async def test_acquire_key_returns_none_when_cipher_unavailable(monkeypatch):
    monkeypatch.setattr(feedback, "get_settings", lambda: _settings())
    assert await feedback._acquire_key(set()) is None


@pytest.mark.asyncio
async def test_acquire_key_skips_key_that_fails_to_decrypt(monkeypatch, caplog):
    import base64
    from app.security.key_cipher import KeyCipher

    secret = base64.b64encode(b"s" * 32).decode()
    other = base64.b64encode(b"o" * 32).decode()
    good_ct, good_nonce = KeyCipher(secret).encrypt(SECRET_KEY)
    bad_ct, bad_nonce = KeyCipher(other).encrypt("AIza-OTHER-SECRET")
    rows = [_FakeRow(id="bad", key_ciphertext=bad_ct, key_nonce=bad_nonce),
            _FakeRow(id="good", key_ciphertext=good_ct, key_nonce=good_nonce)]
    seen: list[set[str]] = []

    class FakeRepository:
        def acquire(self, session, now, exclude_ids):
            assert now.tzinfo is not None
            seen.append(set(exclude_ids))
            return next((row for row in rows if row.id not in exclude_ids), None)

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(feedback, "get_settings", lambda: SimpleNamespace(key_encryption_secret=secret))
    monkeypatch.setattr(feedback, "SessionFactory", FakeSession)
    monkeypatch.setattr(feedback, "LlmApiKeyRepository", FakeRepository)
    caplog.set_level(logging.DEBUG)

    exclude: set[str] = set()
    key = await feedback._acquire_key(exclude)
    assert key == feedback.PooledKey(id="good", api_key=SECRET_KEY)
    assert seen == [set(), {"bad"}]
    assert "bad" in exclude
    assert SECRET_KEY not in caplog.text

    rows.pop()  # only the undecryptable key is left
    assert await feedback._acquire_key(set()) is None
