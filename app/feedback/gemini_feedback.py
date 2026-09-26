import asyncio
import json
import logging
from typing import Any

from google import genai

from app.config import get_settings

logger = logging.getLogger(__name__)
FALLBACK_FEEDBACK = "Chưa thể tạo nhận xét AI lúc này. Bạn vẫn có thể xem các chỉ số phân tích để tiếp tục luyện tập."


def _build_prompt(result: dict[str, Any]) -> str:
    compact_result = {key: result.get(key) for key in (
        "instrument", "pitchAccuracyPercent", "rhythmAccuracyPercent", "overallScore",
        "meanDeviationCents", "teacherTempoBpm", "studentTempoBpm", "rhythmSegments", "pitchErrorSegments",
    )}
    return f"""Bạn là giáo viên dạy nhạc cụ truyền thống Việt Nam, chuyên nghiệp nhưng thân thiện.
Hãy nhận xét ngắn gọn bằng tiếng Việt cho học viên dựa trên kết quả phân tích dưới đây.

Kết quả phân tích:
{json.dumps(compact_result, ensure_ascii=False, indent=2)}

Yêu cầu: viết tối đa 4 câu, khích lệ học viên, nêu vấn đề cụ thể về cao độ hoặc nhịp nếu có,
giải thích các đoạn nhanh/chậm/mất nhịp theo mốc thời gian và đưa ra một gợi ý luyện tập thực tế.
Không giải thích dài về thuật toán, cents hoặc BPM.""".strip()


def _generate_feedback_sync(result: dict[str, Any]) -> str:
    settings = get_settings()
    client = genai.Client(api_key=settings.gemini_api_key)
    try:
        response = client.models.generate_content(model=settings.gemini_model, contents=_build_prompt(result))
        return (response.text or "").strip() or FALLBACK_FEEDBACK
    finally:
        client.close()


async def generate_feedback(result: dict[str, Any]) -> str:
    settings = get_settings()
    if not settings.gemini_api_key:
        logger.warning("GEMINI_API_KEY chưa được cấu hình")
        return FALLBACK_FEEDBACK
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_generate_feedback_sync, result),
            timeout=settings.gemini_timeout_seconds,
        )
    except Exception:
        logger.exception("Gemini feedback generation failed")
        return FALLBACK_FEEDBACK
