"""UTC-RFB: unit tests for FeedbackService / gemini_feedback. No real LLM, DB or network is used."""
import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from google.genai import errors as genai_errors

from app.feedback import gemini_feedback as fb
from app.services.feedback_service import FeedbackService

SECRET = "AIza-SECRET-xyz"


def _api_error(code: int) -> genai_errors.APIError:
    return genai_errors.APIError(code, {"error": {"message": f"boom {code}", "status": "X"}})


def _settings(gemini_api_key="", openrouter_api_key="", timeout=5.0):
    return SimpleNamespace(gemini_api_key=gemini_api_key, openrouter_api_key=openrouter_api_key,
                           gemini_timeout_seconds=timeout, gemini_model="gemini-test", key_encryption_secret="")


class Env:
    """Fakes key acquisition, outcome recording and both provider calls."""

    def __init__(self, monkeypatch, gemini_keys=(), openrouter_keys=(), gemini=None, openrouter=None, settings=None):
        self.keys = {"gemini": [fb.PooledKey(i, k) for i, k in gemini_keys],
                     "openrouter": [fb.PooledKey(i, k) for i, k in openrouter_keys]}
        self.outcomes = []
        self.calls = []  # (provider, api_key, prompt)
        self.gemini = gemini or (lambda key: "gemini-ok")
        self.openrouter = openrouter or (lambda key: "openrouter-ok")

        async def acquire(exclude_ids, provider=None):
            for key in self.keys[provider or "gemini"]:
                if key.id not in exclude_ids:
                    return key
            return None

        async def record(key_id, code, kind):
            self.outcomes.append((key_id, code, kind))

        def gem(prompt, api_key):
            self.calls.append(("gemini", api_key, prompt))
            return self.gemini(api_key)

        def orr(prompt, api_key):
            self.calls.append(("openrouter", api_key, prompt))
            return self.openrouter(api_key)

        monkeypatch.setattr(fb, "get_settings", lambda: settings or _settings())
        monkeypatch.setattr(fb, "_acquire_key", acquire)
        monkeypatch.setattr(fb, "_record_outcome", record)
        monkeypatch.setattr(fb, "_generate_gemini_sync", gem)
        monkeypatch.setattr(fb, "_generate_openrouter_sync", orr)

    @property
    def used(self):
        return [(p, k) for p, k, _ in self.calls]


def _raise(exc):
    def f(key):
        raise exc
    return f


# ---------------- _format_time ----------------

def test_utc_rfb_001_format_time_minutes_seconds():
    assert fb._format_time(75) == "01:15"


def test_utc_rfb_002_format_time_zero():
    assert fb._format_time(0) == "00:00"


def test_utc_rfb_003_format_time_truncates_fraction():
    assert fb._format_time(59.99) == "00:59"


def test_utc_rfb_004_format_time_over_an_hour_keeps_minutes():
    assert fb._format_time(3600) == "60:00"


# ---------------- _build_prompt / _diagnosis_rules ----------------

def test_utc_rfb_005_prompt_without_diagnoses_uses_pitch_error_segments():
    prompt = fb._build_prompt({"instrument": "dan_tranh", "overallScore": 80, "pitchErrorSegments": [{"a": 1}]})
    assert '"pitchErrorSegments"' in prompt and '"overallScore": 80' in prompt
    assert "techniqueDiagnoses" not in prompt
    assert "WRONG_NOTE" not in prompt


def test_utc_rfb_006_prompt_with_diagnoses_adds_time_and_rules():
    diag = [{"startTime": 65, "type": "WRONG_NOTE", "advice": "x"}]
    prompt = fb._build_prompt({"techniqueDiagnoses": diag, "pitchErrorSegments": [{"a": 1}]})
    assert '"time": "01:05"' in prompt
    assert "WRONG_NOTE" in prompt and "STRING_TUNING" in prompt
    assert "pitchErrorSegments" not in prompt.split("Kết quả phân tích:")[1].split("Yêu cầu")[0]


def test_utc_rfb_007_prompt_with_segment_mentions_range():
    prompt = fb._build_prompt({"segment": {"startSec": 10, "endSec": 70}})
    assert "Đây là đoạn 00:10–01:10 của bài, chỉ nhận xét đoạn này." in prompt


def test_utc_rfb_008_prompt_without_segment_has_no_segment_note():
    assert "Đây là đoạn" not in fb._build_prompt({"overallScore": 1})


def test_utc_rfb_009_prompt_empty_result_all_fields_null():
    prompt = fb._build_prompt({})
    body = prompt.split("Kết quả phân tích:\n")[1].split("\n\nYêu cầu")[0]
    data = json.loads(body)
    assert data["instrument"] is None and data["overallScore"] is None and data["pitchErrorSegments"] is None


def test_utc_rfb_010_diagnosis_rules_false_returns_empty():
    assert fb._diagnosis_rules(False) == ""


def test_utc_rfb_011_diagnosis_rules_true_contains_priority_rules():
    text = fb._diagnosis_rules(True)
    assert "WRONG_NOTE" in text and "STRING_TUNING" in text and "TECHNIQUE" in text and "tối đa 3 lỗi" in text


# ---------------- PooledKey / error helpers ----------------

def test_utc_rfb_012_pooled_key_repr_hides_key():
    key = fb.PooledKey(id="k1", api_key=SECRET)
    assert SECRET not in repr(key) and SECRET not in str(key)
    assert repr(key) == "PooledKey(id='k1', api_key='***')"


def test_utc_rfb_013_status_code_variants():
    assert fb._status_code(_api_error(429)) == 429
    assert fb._status_code(fb.ProviderError(502)) == 502
    assert fb._status_code(ValueError("x")) is None


@pytest.mark.parametrize("uid, error, expected", [
    ("UTC-RFB-014", _api_error(429), "quota"),
    ("UTC-RFB-015", fb.ProviderError(402), "quota"),
    ("UTC-RFB-016", _api_error(403), "invalid"),
    ("UTC-RFB-017", fb.ProviderError(599), "server"),
    ("UTC-RFB-018", fb.ProviderError(600), "other"),
    ("UTC-RFB-019", _api_error(404), "other"),
    ("UTC-RFB-020", RuntimeError("x"), "other"),
])
def test_utc_rfb_014_to_020_classify_error(uid, error, expected):
    assert fb._classify_error(error) == expected


def test_utc_rfb_021_error_code_http_and_class_name():
    assert fb._error_code(_api_error(429)) == "HTTP_429"
    assert fb._error_code(fb.ProviderError(None)) == "ProviderError"
    assert fb._error_code(ValueError("x")) == "ValueError"


def test_utc_rfb_022_provider_order_returns_independent_list():
    order = fb._provider_order()
    assert order == ["gemini", "openrouter"]
    order.append("x")
    assert fb._provider_order() == ["gemini", "openrouter"]


def test_utc_rfb_023_env_key_selects_by_provider(monkeypatch):
    monkeypatch.setattr(fb, "get_settings", lambda: _settings("g-env", "o-env"))
    assert fb._env_key("gemini") == "g-env"
    assert fb._env_key("openrouter") == "o-env"


# ---------------- _try_provider ----------------

@pytest.mark.asyncio
async def test_utc_rfb_024_try_provider_success_first_key(monkeypatch):
    env = Env(monkeypatch, gemini_keys=[("g1", "gk1"), ("g2", "gk2")])
    assert await fb._try_provider({}, "gemini") == (True, "gemini-ok")
    assert env.used == [("gemini", "gk1")]
    assert env.outcomes == [("g1", None, None)]


@pytest.mark.asyncio
async def test_utc_rfb_025_try_provider_429_rotates_to_next_key(monkeypatch):
    env = Env(monkeypatch, gemini_keys=[("g1", "gk1"), ("g2", "gk2")],
              gemini=lambda k: (_ for _ in ()).throw(_api_error(429)) if k == "gk1" else "ok2")
    assert await fb._try_provider({}, "gemini") == (True, "ok2")
    assert env.outcomes == [("g1", "HTTP_429", "quota"), ("g2", None, None)]


@pytest.mark.asyncio
async def test_utc_rfb_026_try_provider_5xx_stops_rotation(monkeypatch):
    env = Env(monkeypatch, gemini_keys=[("g1", "gk1"), ("g2", "gk2")], gemini=_raise(_api_error(503)),
              settings=_settings(gemini_api_key="env"))
    assert await fb._try_provider({}, "gemini") == (True, None)
    assert env.used == [("gemini", "gk1")]
    assert env.outcomes == [("g1", "HTTP_503", "server")]


@pytest.mark.asyncio
async def test_utc_rfb_027_try_provider_auth_error_marks_invalid_and_rotates(monkeypatch):
    env = Env(monkeypatch, gemini_keys=[("g1", "gk1"), ("g2", "gk2")],
              gemini=lambda k: (_ for _ in ()).throw(_api_error(401)) if k == "gk1" else "ok2")
    assert await fb._try_provider({}, "gemini") == (True, "ok2")
    assert env.outcomes[0] == ("g1", "HTTP_401", "invalid")


@pytest.mark.asyncio
async def test_utc_rfb_028_try_provider_all_keys_exhausted_skips_env_key(monkeypatch):
    env = Env(monkeypatch, gemini_keys=[("g1", "a"), ("g2", "b")], gemini=_raise(_api_error(429)),
              settings=_settings(gemini_api_key="env"))
    assert await fb._try_provider({}, "gemini") == (True, None)
    assert [k for _, k in env.used] == ["a", "b"]


@pytest.mark.asyncio
async def test_utc_rfb_029_try_provider_stops_after_max_attempts(monkeypatch):
    env = Env(monkeypatch, gemini_keys=[(f"g{i}", f"k{i}") for i in range(5)], gemini=_raise(RuntimeError("down")))
    assert await fb._try_provider({}, "gemini") == (True, None)
    assert len(env.used) == fb.MAX_KEY_ATTEMPTS


@pytest.mark.asyncio
async def test_utc_rfb_030_try_provider_no_keys_no_env_not_attempted(monkeypatch):
    env = Env(monkeypatch)
    assert await fb._try_provider({}, "gemini") == (False, None)
    assert env.used == []


@pytest.mark.asyncio
async def test_utc_rfb_031_try_provider_env_fallback_key_used(monkeypatch):
    env = Env(monkeypatch, settings=_settings(gemini_api_key="env-key"))
    assert await fb._try_provider({}, "gemini") == (True, "gemini-ok")
    assert env.used == [("gemini", "env-key")]
    assert env.outcomes == []


@pytest.mark.asyncio
async def test_utc_rfb_032_try_provider_env_fallback_failure_returns_none(monkeypatch):
    Env(monkeypatch, gemini=_raise(_api_error(401)), settings=_settings(gemini_api_key="env-key"))
    assert await fb._try_provider({}, "gemini") == (True, None)


# ---------------- _generate_with_pool ----------------

@pytest.mark.asyncio
async def test_utc_rfb_033_pool_5xx_on_gemini_falls_to_openrouter(monkeypatch):
    env = Env(monkeypatch, gemini_keys=[("g1", "gk1"), ("g2", "gk2")], openrouter_keys=[("o1", "ok1")],
              gemini=_raise(_api_error(500)))
    assert await fb._generate_with_pool({}) == "openrouter-ok"
    assert env.used == [("gemini", "gk1"), ("openrouter", "ok1")]


@pytest.mark.asyncio
async def test_utc_rfb_034_pool_no_provider_has_keys_returns_none(monkeypatch):
    Env(monkeypatch)
    assert await fb._generate_with_pool({}) is None


@pytest.mark.asyncio
async def test_utc_rfb_035_pool_all_providers_fail_raises_failed(monkeypatch):
    Env(monkeypatch, gemini_keys=[("g1", "a")], openrouter_keys=[("o1", "b")],
        gemini=_raise(_api_error(503)), openrouter=_raise(fb.ProviderError(502)))
    with pytest.raises(fb.FeedbackGenerationError) as caught:
        await fb._generate_with_pool({})
    assert caught.value.code == "AI_FEEDBACK_FAILED"


# ---------------- generate_feedback / FeedbackGenerationError / FeedbackService ----------------

@pytest.mark.asyncio
async def test_utc_rfb_036_generate_feedback_success_first_provider_and_prompt(monkeypatch):
    env = Env(monkeypatch, gemini_keys=[("g1", "gk1")], openrouter_keys=[("o1", "ok1")])
    assert await fb.generate_feedback({"overallScore": 90}) == "gemini-ok"
    assert env.used == [("gemini", "gk1")]
    assert '"overallScore": 90' in env.calls[0][2]


@pytest.mark.asyncio
async def test_utc_rfb_037_generate_feedback_empty_text_raises(monkeypatch):
    Env(monkeypatch, gemini_keys=[("g1", "gk1")], gemini=lambda k: "")
    with pytest.raises(fb.FeedbackGenerationError) as caught:
        await fb.generate_feedback({})
    assert caught.value.code == "AI_FEEDBACK_FAILED"
    assert str(caught.value) == "Dịch vụ AI chưa trả về nhận xét. Vui lòng thử lại."


@pytest.mark.asyncio
async def test_utc_rfb_038_generate_feedback_whitespace_text_raises(monkeypatch):
    Env(monkeypatch, gemini_keys=[("g1", "gk1")], gemini=lambda k: "   \n")
    with pytest.raises(fb.FeedbackGenerationError) as caught:
        await fb.generate_feedback({})
    assert caught.value.code == "AI_FEEDBACK_FAILED"


@pytest.mark.asyncio
async def test_utc_rfb_039_generate_feedback_all_failed_message(monkeypatch):
    Env(monkeypatch, gemini_keys=[("g1", "a")], openrouter_keys=[("o1", "b")],
        gemini=_raise(_api_error(429)), openrouter=_raise(_api_error(401)))
    with pytest.raises(fb.FeedbackGenerationError) as caught:
        await fb.generate_feedback({})
    assert caught.value.code == "AI_FEEDBACK_FAILED"
    assert str(caught.value) == "Chưa thể tạo nhận xét AI. Vui lòng thử lại."


@pytest.mark.asyncio
async def test_utc_rfb_040_generate_feedback_no_keys_returns_fallback(monkeypatch):
    Env(monkeypatch)
    assert await fb.generate_feedback({}) == fb.FALLBACK_FEEDBACK


@pytest.mark.asyncio
async def test_utc_rfb_041_generate_feedback_timeout(monkeypatch):
    def slow(key):
        time.sleep(0.3)
        return "late"
    Env(monkeypatch, gemini_keys=[("g1", "gk1")], gemini=slow, settings=_settings(timeout=0.05))
    with pytest.raises(fb.FeedbackGenerationError) as caught:
        await fb.generate_feedback({})
    assert caught.value.code == "AI_FEEDBACK_TIMEOUT"
    assert str(caught.value) == "Dịch vụ AI phản hồi quá thời gian chờ. Vui lòng thử lại."


@pytest.mark.asyncio
async def test_utc_rfb_042_generate_feedback_unexpected_error_wrapped(monkeypatch):
    Env(monkeypatch)

    async def boom(result):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(fb, "_generate_with_pool", boom)
    with pytest.raises(fb.FeedbackGenerationError) as caught:
        await fb.generate_feedback({})
    assert caught.value.code == "AI_FEEDBACK_FAILED"
    assert isinstance(caught.value.__cause__, RuntimeError)


def test_utc_rfb_043_feedback_generation_error_carries_code_and_message():
    err = fb.FeedbackGenerationError("CODE_X", "msg")
    assert err.code == "CODE_X" and str(err) == "msg"


@pytest.mark.asyncio
async def test_utc_rfb_044_service_generate_delegates(monkeypatch):
    seen = []

    async def fake(result):
        seen.append(result)
        return "delegated"

    import app.services.feedback_service as svc
    monkeypatch.setattr(svc, "generate_feedback", fake)
    assert await FeedbackService().generate({"a": 1}) == "delegated"
    assert seen == [{"a": 1}]


@pytest.mark.asyncio
async def test_utc_rfb_045_service_generate_propagates_error(monkeypatch):
    async def fake(result):
        raise fb.FeedbackGenerationError("AI_FEEDBACK_FAILED", "m")

    import app.services.feedback_service as svc
    monkeypatch.setattr(svc, "generate_feedback", fake)
    with pytest.raises(fb.FeedbackGenerationError):
        await FeedbackService().generate({})
