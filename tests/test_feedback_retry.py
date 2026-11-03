from types import SimpleNamespace

import pytest

from app.feedback import gemini_feedback as feedback


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "unavailable", "empty"])
async def test_configured_ai_failure_does_not_complete_with_fallback(monkeypatch, failure):
    monkeypatch.setattr(feedback, "get_settings", lambda: SimpleNamespace(gemini_api_key="test", gemini_timeout_seconds=0.01))

    async def generate(*args):
        if failure == "timeout":
            import asyncio
            await asyncio.sleep(1)
        if failure == "unavailable":
            raise RuntimeError("provider unavailable")
        return ""

    monkeypatch.setattr(feedback.asyncio, "to_thread", generate)
    with pytest.raises(Exception) as caught:
        await feedback.generate_feedback({"overallScore": 80})
    assert getattr(caught.value, "code", None) == ("AI_FEEDBACK_TIMEOUT" if failure == "timeout" else "AI_FEEDBACK_FAILED")


@pytest.mark.asyncio
async def test_unconfigured_ai_keeps_existing_optional_feedback_behavior(monkeypatch):
    monkeypatch.setattr(feedback, "get_settings", lambda: SimpleNamespace(gemini_api_key=""))
    assert await feedback.generate_feedback({}) == feedback.FALLBACK_FEEDBACK
