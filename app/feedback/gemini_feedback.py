import asyncio
import json
from typing import Any

from google import genai

from app.config import get_settings

FALLBACK_FEEDBACK = "Chưa thể tạo nhận xét AI lúc này. Bạn vẫn có thể xem các chỉ số phân tích để tiếp tục luyện tập."


class FeedbackGenerationError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _format_time(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 60:02d}:{total % 60:02d}"


def _build_prompt(result: dict[str, Any]) -> str:
    compact_result = {key: result.get(key) for key in (
        "instrument", "pitchAccuracyPercent", "rhythmAccuracyPercent", "overallScore",
        "meanDeviationCents", "teacherTempoBpm", "studentTempoBpm", "rhythmSegments",
    )}
    diagnoses = result.get("techniqueDiagnoses") or []
    if diagnoses:
        compact_result["techniqueDiagnoses"] = [{**item, "time": _format_time(item["startTime"])} for item in diagnoses]
    else:
        compact_result["pitchErrorSegments"] = result.get("pitchErrorSegments")
    segment = result.get("segment")
    segment_note = ""
    if segment:
        span = f"{_format_time(segment['startSec'])}–{_format_time(segment['endSec'])}"
        segment_note = "\n" + f"Đây là đoạn {span} của bài, chỉ nhận xét đoạn này." + "\n"
    return f"""Bạn là giáo viên dạy nhạc cụ truyền thống Việt Nam, chuyên nghiệp nhưng thân thiện.
Hãy nhận xét ngắn gọn bằng tiếng Việt cho học viên dựa trên kết quả phân tích dưới đây.{segment_note}

Kết quả phân tích:
{json.dumps(compact_result, ensure_ascii=False, indent=2)}

Yêu cầu: viết tối đa 5 câu, khích lệ học viên, nêu vấn đề cụ thể về cao độ hoặc nhịp nếu có,
giải thích các đoạn nhanh/chậm/mất nhịp theo mốc thời gian và đưa ra một gợi ý luyện tập thực tế.
Không giải thích dài về thuật toán, cents hoặc BPM.{_diagnosis_rules(bool(diagnoses))}""".strip()


def _diagnosis_rules(has_diagnoses: bool) -> str:
    if not has_diagnoses:
        return ""
    return """

Cách dùng "techniqueDiagnoses" (đã được hệ thống chẩn đoán theo từng nốt, mỗi mục có mốc thời gian, nốt bản mẫu và lời khuyên):
- Ưu tiên theo thứ tự: WRONG_NOTE (đánh sai nốt) > STRING_TUNING (dây lên sai) > TECHNIQUE (kỹ thuật tạo cao độ).
- Nếu có STRING_TUNING, nhắc học viên chỉnh lại dây đó trước khi luyện tiếp, vì mọi lần gảy dây đó sẽ lệch.
- Nêu tên nốt và mốc thời gian (mm:ss), dùng đúng nội dung "advice"; không tự đưa ra kỹ thuật khác ngoài "advice".
- Chỉ nhắc tối đa 3 lỗi cao độ quan trọng nhất."""


def _generate_feedback_sync(result: dict[str, Any]) -> str:
    settings = get_settings()
    client = genai.Client(api_key=settings.gemini_api_key)
    try:
        response = client.models.generate_content(model=settings.gemini_model, contents=_build_prompt(result))
        return (response.text or "").strip()
    finally:
        client.close()


async def generate_feedback(result: dict[str, Any]) -> str:
    settings = get_settings()
    if not settings.gemini_api_key:
        return FALLBACK_FEEDBACK
    try:
        text = await asyncio.wait_for(
            asyncio.to_thread(_generate_feedback_sync, result),
            timeout=settings.gemini_timeout_seconds,
        )
    except TimeoutError as error:
        raise FeedbackGenerationError("AI_FEEDBACK_TIMEOUT", "Dịch vụ AI phản hồi quá thời gian chờ. Vui lòng thử lại.") from error
    except Exception as error:
        raise FeedbackGenerationError("AI_FEEDBACK_FAILED", "Chưa thể tạo nhận xét AI. Vui lòng thử lại.") from error
    if not text.strip():
        raise FeedbackGenerationError("AI_FEEDBACK_FAILED", "Dịch vụ AI chưa trả về nhận xét. Vui lòng thử lại.")
    return text
