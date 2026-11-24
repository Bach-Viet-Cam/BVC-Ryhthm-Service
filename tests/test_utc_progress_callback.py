import asyncio

import httpx
import pytest

from app.schemas.messages import EvaluationTask
from app.services import practice_progress_callback_client as module
from app.services.job_execution_service import JobExecutionService
from app.services.practice_progress_callback_client import PracticeProgressCallbackClient


class FakeSettings:
    def __init__(self, secret="s3cret", hosts=set(), retries=2, timeout=5.0):
        self.callback_secret = secret
        self.callback_allowed_host_values = hosts
        self.callback_max_retries = retries
        self.callback_timeout_seconds = timeout


class FakeHttp:
    """Replaces httpx.AsyncClient; `script` items are status codes or exceptions."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.client_kwargs = []
        self.sleeps = []

    def install(self, monkeypatch):
        outer = self

        class Client:
            def __init__(self, **kwargs):
                outer.client_kwargs.append(kwargs)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def post(self, url, json=None, headers=None):
                outer.calls.append({"url": url, "json": json, "headers": headers})
                item = outer.script.pop(0) if len(outer.script) > 1 else outer.script[0]
                if isinstance(item, Exception):
                    raise item
                return httpx.Response(item)

        async def fake_sleep(seconds):
            outer.sleeps.append(seconds)

        monkeypatch.setattr(module.httpx, "AsyncClient", Client)
        monkeypatch.setattr(module.asyncio, "sleep", fake_sleep)
        return self


def make_task(**changes):
    values = dict(messageId="m-1", evaluationId="eval-1", templateUrl="https://example.com/t.wav",
                  studentUrl="https://example.com/s.wav", instrumentId="dan_bau",
                  instrument=dict(name="Dan bau", fminHz=65, fmaxHz=1200, toleranceCents=70),
                  callbackUrl="https://api.example.com/cb", attempt=3)
    values.update(changes)
    return EvaluationTask(**values)


def run(coro):
    return asyncio.run(coro)


# ---------------- __init__ ----------------

def test_utc_rpc_001_init_uses_default_settings_when_none(monkeypatch):
    sentinel = FakeSettings()
    monkeypatch.setattr(module, "get_settings", lambda: sentinel)
    assert PracticeProgressCallbackClient().settings is sentinel


def test_utc_rpc_002_init_keeps_injected_settings():
    settings = FakeSettings()
    assert PracticeProgressCallbackClient(settings).settings is settings


# ---------------- send: skip conditions ----------------

def test_utc_rpc_003_no_callback_url_skips_without_http(monkeypatch):
    http = FakeHttp([200]).install(monkeypatch)
    assert run(PracticeProgressCallbackClient(FakeSettings()).send(make_task(callbackUrl=None), "quality_check", "started")) is False
    assert http.calls == []


def test_utc_rpc_004_empty_secret_skips_without_http(monkeypatch):
    http = FakeHttp([200]).install(monkeypatch)
    assert run(PracticeProgressCallbackClient(FakeSettings(secret="")).send(make_task(), "quality_check", "started")) is False
    assert http.calls == []


def test_utc_rpc_005_host_not_in_allowlist_skips(monkeypatch):
    http = FakeHttp([200]).install(monkeypatch)
    client = PracticeProgressCallbackClient(FakeSettings(hosts={"trusted.example.org"}))
    assert run(client.send(make_task(), "audio_analysis", "done")) is False
    assert http.calls == []


def test_utc_rpc_006_allowlist_match_is_case_insensitive(monkeypatch):
    http = FakeHttp([200]).install(monkeypatch)
    client = PracticeProgressCallbackClient(FakeSettings(hosts={"api.example.com"}))
    assert run(client.send(make_task(callbackUrl="https://API.Example.com/cb"), "audio_analysis", "done")) is True
    assert len(http.calls) == 1


def test_utc_rpc_007_empty_allowlist_allows_any_host(monkeypatch):
    http = FakeHttp([200]).install(monkeypatch)
    assert run(PracticeProgressCallbackClient(FakeSettings(hosts=set())).send(make_task(callbackUrl="http://anything.test:9000/x"), "comparison", "started")) is True
    assert http.calls[0]["url"] == "http://anything.test:9000/x"


# ---------------- send: request shape ----------------

def test_utc_rpc_008_posts_url_secret_header_and_minimal_body(monkeypatch):
    http = FakeHttp([200]).install(monkeypatch)
    assert run(PracticeProgressCallbackClient(FakeSettings()).send(make_task(), "quality_check", "started")) is True
    call = http.calls[0]
    assert call["url"] == "https://api.example.com/cb"
    assert call["headers"] == {"X-Callback-Secret": "s3cret"}
    assert call["json"] == {"evaluationId": "eval-1", "attempt": 3, "job": "quality_check", "status": "started"}


def test_utc_rpc_009_body_includes_code_reason_result_for_failed_feedback(monkeypatch):
    http = FakeHttp([200]).install(monkeypatch)
    result = {"score": 80}
    assert run(PracticeProgressCallbackClient(FakeSettings()).send(
        make_task(), "feedback", "failed", code="LLM_ERROR", reason="Het quota", result=result)) is True
    assert http.calls[0]["json"] == {"evaluationId": "eval-1", "attempt": 3, "job": "feedback", "status": "failed",
                                     "code": "LLM_ERROR", "reason": "Het quota", "result": result}


@pytest.mark.parametrize("job,status", [("audio_analysis", "done"), ("comparison", "rejected"), ("feedback", "started")])
def test_utc_rpc_010_body_carries_job_and_status(monkeypatch, job, status):
    http = FakeHttp([200]).install(monkeypatch)
    assert run(PracticeProgressCallbackClient(FakeSettings()).send(make_task(), job, status)) is True
    assert http.calls[0]["json"]["job"] == job and http.calls[0]["json"]["status"] == status


def test_utc_rpc_011_empty_string_and_empty_dict_are_still_sent(monkeypatch):
    http = FakeHttp([200]).install(monkeypatch)
    run(PracticeProgressCallbackClient(FakeSettings()).send(make_task(), "comparison", "done", code="", reason="", result={}))
    body = http.calls[0]["json"]
    assert body["code"] == "" and body["reason"] == "" and body["result"] == {}


def test_utc_rpc_012_client_uses_timeout_and_no_redirects(monkeypatch):
    http = FakeHttp([200]).install(monkeypatch)
    run(PracticeProgressCallbackClient(FakeSettings(timeout=1.5)).send(make_task(), "quality_check", "done"))
    assert http.client_kwargs == [{"timeout": 1.5, "follow_redirects": False}]


# ---------------- send: HTTP status handling ----------------

def test_utc_rpc_013_status_299_is_success_300_is_failure_without_retry(monkeypatch):
    http = FakeHttp([299]).install(monkeypatch)
    assert run(PracticeProgressCallbackClient(FakeSettings()).send(make_task(), "feedback", "done")) is True
    http = FakeHttp([300]).install(monkeypatch)
    assert run(PracticeProgressCallbackClient(FakeSettings()).send(make_task(), "feedback", "done")) is False
    assert len(http.calls) == 1


def test_utc_rpc_014_4xx_returns_false_without_retry(monkeypatch):
    http = FakeHttp([400]).install(monkeypatch)
    assert run(PracticeProgressCallbackClient(FakeSettings(retries=2)).send(make_task(), "feedback", "done")) is False
    assert len(http.calls) == 1 and http.sleeps == []


def test_utc_rpc_015_429_is_retried_until_success(monkeypatch):
    http = FakeHttp([429, 200]).install(monkeypatch)
    assert run(PracticeProgressCallbackClient(FakeSettings()).send(make_task(), "feedback", "done")) is True
    assert len(http.calls) == 2


def test_utc_rpc_016_5xx_retries_max_then_returns_false_with_backoff(monkeypatch):
    http = FakeHttp([503]).install(monkeypatch)
    assert run(PracticeProgressCallbackClient(FakeSettings(retries=2)).send(make_task(), "feedback", "done")) is False
    assert len(http.calls) == 3
    assert http.sleeps == [0.1, 0.2]


def test_utc_rpc_017_zero_retries_makes_single_attempt(monkeypatch):
    http = FakeHttp([500]).install(monkeypatch)
    assert run(PracticeProgressCallbackClient(FakeSettings(retries=0)).send(make_task(), "feedback", "done")) is False
    assert len(http.calls) == 1 and http.sleeps == []


# ---------------- send: network errors ----------------

def test_utc_rpc_018_network_error_then_success_returns_true(monkeypatch):
    http = FakeHttp([httpx.ConnectError("refused"), 200]).install(monkeypatch)
    assert run(PracticeProgressCallbackClient(FakeSettings()).send(make_task(), "quality_check", "started")) is True
    assert len(http.calls) == 2


def test_utc_rpc_019_timeout_on_all_attempts_returns_false_without_raising(monkeypatch):
    http = FakeHttp([httpx.ReadTimeout("slow")]).install(monkeypatch)
    assert run(PracticeProgressCallbackClient(FakeSettings(retries=2)).send(make_task(), "quality_check", "started")) is False
    assert len(http.calls) == 3


# ---------------- JobExecutionService.run ----------------

def test_utc_rpc_020_run_serialises_two_operations():
    events = []

    async def op(name):
        events.append(f"{name}-start")
        await asyncio.sleep(0.02)
        events.append(f"{name}-end")

    async def main():
        service = JobExecutionService()
        await asyncio.gather(service.run(lambda: op("a")), service.run(lambda: op("b")))

    run(main())
    assert events == ["a-start", "a-end", "b-start", "b-end"]


def test_utc_rpc_021_run_releases_lock_after_exception_and_propagates():
    async def boom():
        raise RuntimeError("fail")

    async def ok():
        done.append(True)

    done = []

    async def main():
        service = JobExecutionService()
        with pytest.raises(RuntimeError, match="fail"):
            await service.run(boom)
        assert not service.lock.locked()
        await asyncio.wait_for(service.run(ok), timeout=1)

    run(main())
    assert done == [True]


def test_utc_rpc_022_run_cancellation_waits_for_operation_before_releasing_lock():
    state = {"finished": False, "locked_at_cancel_return": None}

    async def slow():
        await asyncio.sleep(0.05)
        state["finished"] = True

    async def main():
        service = JobExecutionService()
        task = asyncio.create_task(service.run(slow))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        state["locked_at_cancel_return"] = service.lock.locked()

    run(main())
    assert state["finished"] is True
    assert state["locked_at_cancel_return"] is False
