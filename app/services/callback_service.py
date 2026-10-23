import asyncio
import logging

import httpx

from app.config import get_settings
from app.schemas.messages import ProgressCallback

logger = logging.getLogger(__name__)


class CallbackService:
    """Best-effort push of job progress to the backend; the backend also reconciles by polling."""

    async def notify(self, url: str | None, payload: ProgressCallback) -> None:
        if not url:
            return
        settings = get_settings()
        headers = {"X-Callback-Secret": settings.callback_secret}
        body = payload.model_dump(mode="json")
        for attempt in range(1, settings.callback_attempts + 1):
            try:
                async with httpx.AsyncClient(follow_redirects=False, timeout=settings.callback_timeout_seconds) as client:
                    response = await client.post(url, json=body, headers=headers)
                if response.status_code < 500:
                    if response.status_code >= 400:
                        logger.warning("Callback %s bị từ chối với HTTP %s", payload.evaluationId, response.status_code)
                    return
            except httpx.HTTPError:
                logger.warning("Callback %s thất bại lần %s", payload.evaluationId, attempt)
            if attempt < settings.callback_attempts:
                await asyncio.sleep(attempt)
        logger.error("Không gửi được callback cho %s", payload.evaluationId)
