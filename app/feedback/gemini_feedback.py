import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from cryptography.exceptions import InvalidTag
from google import genai
from google.genai import errors as genai_errors

from app.config import get_settings
from app.database import SessionFactory
from app.repositories.llm_api_key_repository import LlmApiKeyRepository
from app.security.key_cipher import KeyCipher, KeyCipherUnavailable

logger = logging.getLogger(__name__)

MAX_KEY_ATTEMPTS = 3
QUOTA_COOLDOWN = timedelta(seconds=60)

ErrorKind = Literal["quota", "invalid", "other"]

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


@dataclass
class PooledKey:
    id: str
    api_key: str

    def __repr__(self) -> str:  # never expose the key value through repr/logging
        return f"PooledKey(id={self.id!r}, api_key='***')"


def _classify_error(error: Exception) -> ErrorKind:
    if isinstance(error, genai_errors.APIError):
        if error.code == 429:
            return "quota"
        if error.code in (401, 403):
            return "invalid"
    return "other"


def _error_code(error: Exception) -> str:
    if isinstance(error, genai_errors.APIError) and error.code is not None:
        return f"HTTP_{error.code}"
    return type(error).__name__


def _acquire_key_sync(exclude_ids: set[str]) -> PooledKey | None:
    cipher = KeyCipher(get_settings().key_encryption_secret)
    repository = LlmApiKeyRepository()
    with SessionFactory() as session:
        while True:
            row = repository.acquire(session, datetime.now(timezone.utc), exclude_ids)
            if row is None:
                return None
            try:
                return PooledKey(id=row.id, api_key=cipher.decrypt(row.key_ciphertext, row.key_nonce))
            except (InvalidTag, ValueError):
                # Wrong secret or corrupt row: never hand this key out again in the same call.
                exclude_ids.add(row.id)
                logger.warning("Skipping LLM API key %s: cannot decrypt stored value", row.id)


async def _acquire_key(exclude_ids: set[str]) -> PooledKey | None:
    try:
        return await asyncio.to_thread(_acquire_key_sync, exclude_ids)
    except KeyCipherUnavailable:
        logger.warning("LLM API key pool unavailable: encryption secret is not configured correctly")
        return None
    except Exception as error:
        logger.warning("LLM API key pool unavailable: %s", type(error).__name__)
        return None


def _record_outcome_sync(key_id: str, error_code: str | None, kind: ErrorKind | None) -> None:
    repository = LlmApiKeyRepository()
    with SessionFactory() as session:
        if kind is None:
            repository.record_success(session, key_id)
            return
        cooldown_until = datetime.now(timezone.utc) + QUOTA_COOLDOWN if kind == "quota" else None
        repository.record_failure(session, key_id, error_code or "UNKNOWN", cooldown_until=cooldown_until,
                                  invalid=kind == "invalid")


async def _record_outcome(key_id: str, error_code: str | None, kind: ErrorKind | None) -> None:
    try:
        await asyncio.to_thread(_record_outcome_sync, key_id, error_code, kind)
    except Exception as error:
        logger.warning("Could not record outcome for LLM API key %s: %s", key_id, type(error).__name__)


def _generate_feedback_sync(result: dict[str, Any], api_key: str | None = None) -> str:
    settings = get_settings()
    client = genai.Client(api_key=api_key)
    try:
        response = client.models.generate_content(model=settings.gemini_model, contents=_build_prompt(result))
        return (response.text or "").strip()
    finally:
        client.close()


_FAILED_MESSAGE = "Chưa thể tạo nhận xét AI. Vui lòng thử lại."


async def _generate_with_pool(result: dict[str, Any], env_api_key: str) -> str | None:
    """Returns the generated text, or None when neither the pool nor the env key is available."""
    exclude_ids: set[str] = set()
    attempted = False
    for _ in range(MAX_KEY_ATTEMPTS):
        key = await _acquire_key(exclude_ids)
        if key is None:
            break
        exclude_ids.add(key.id)
        attempted = True
        try:
            text = await asyncio.to_thread(_generate_feedback_sync, result, key.api_key)
        except Exception as error:
            kind, code = _classify_error(error), _error_code(error)
            logger.warning("Gemini call with LLM API key %s failed (%s, %s)", key.id, kind, code)
            await _record_outcome(key.id, code, kind)
            continue
        await _record_outcome(key.id, None, None)
        return text
    if attempted:
        raise FeedbackGenerationError("AI_FEEDBACK_FAILED", _FAILED_MESSAGE)
    if not env_api_key:
        return None
    try:
        return await asyncio.to_thread(_generate_feedback_sync, result, env_api_key)
    except Exception as error:
        raise FeedbackGenerationError("AI_FEEDBACK_FAILED", _FAILED_MESSAGE) from error


async def generate_feedback(result: dict[str, Any]) -> str:
    settings = get_settings()
    try:
        text = await asyncio.wait_for(
            _generate_with_pool(result, settings.gemini_api_key),
            timeout=settings.gemini_timeout_seconds,
        )
    except TimeoutError as error:
        raise FeedbackGenerationError("AI_FEEDBACK_TIMEOUT", "Dịch vụ AI phản hồi quá thời gian chờ. Vui lòng thử lại.") from error
    except FeedbackGenerationError:
        raise
    except Exception as error:
        raise FeedbackGenerationError("AI_FEEDBACK_FAILED", _FAILED_MESSAGE) from error
    if text is None:
        return FALLBACK_FEEDBACK
    if not text.strip():
        raise FeedbackGenerationError("AI_FEEDBACK_FAILED", "Dịch vụ AI chưa trả về nhận xét. Vui lòng thử lại.")
    return text
