import asyncio
import logging
from typing import Any, Literal
from urllib.parse import urlparse

import httpx

from app.config import Settings, get_settings
from app.schemas.messages import EvaluationTask

logger = logging.getLogger(__name__)

ProgressJob = Literal["quality_check", "audio_analysis", "comparison", "feedback"]
ProgressStatus = Literal["started", "done", "failed", "rejected"]


class PracticeProgressCallbackClient:
    """Best-effort progress delivery; status polling remains the recovery path."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    async def send(self, task: EvaluationTask, job: ProgressJob, status: ProgressStatus,
                   *, code: str | None = None, reason: str | None = None,
                   result: dict[str, Any] | None = None) -> bool:
        if task.callbackUrl is None:
            return False
        if not self.settings.callback_secret:
            logger.warning("Skipping practice callback because RHYTHM_SERVICE_CALLBACK_SECRET is not configured.")
            return False
        url = str(task.callbackUrl)
        hostname = urlparse(url).hostname
        if not hostname or (self.settings.callback_allowed_host_values and hostname.lower() not in self.settings.callback_allowed_host_values):
            logger.warning("Skipping practice callback to a host outside the callback allowlist.")
            return False
        payload: dict[str, Any] = {"evaluationId": task.evaluationId, "attempt": task.attempt, "job": job, "status": status}
        if code is not None:
            payload["code"] = code
        if reason is not None:
            payload["reason"] = reason
        if result is not None:
            payload["result"] = result
        for attempt in range(self.settings.callback_max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self.settings.callback_timeout_seconds, follow_redirects=False) as client:
                    response = await client.post(url, json=payload, headers={"X-Callback-Secret": self.settings.callback_secret})
                if 200 <= response.status_code < 300:
                    return True
                if response.status_code < 500 and response.status_code != 429:
                    break
            except httpx.HTTPError:
                pass
            if attempt < self.settings.callback_max_retries:
                await asyncio.sleep(0.1 * (2 ** attempt))
        logger.warning("Practice callback delivery failed: evaluationId=%s attempt=%s job=%s status=%s", task.evaluationId, task.attempt, job, status)
        return False
