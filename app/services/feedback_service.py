from typing import Any

from app.feedback.gemini_feedback import generate_feedback


class FeedbackService:
    async def generate(self, result: dict[str, Any]) -> str:
        return await generate_feedback(result)
