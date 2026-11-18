import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import httpx
from cryptography.exceptions import InvalidTag
from google import genai
from google.genai import errors as genai_errors

from app.config import get_settings
from app.constants.llm_providers import PROVIDER_ORDER
from app.database import SessionFactory
from app.repositories.llm_api_key_repository import LlmApiKeyRepository
from app.security.key_cipher import KeyCipher, KeyCipherUnavailable

logger = logging.getLogger(__name__)

MAX_KEY_ATTEMPTS = 3
QUOTA_COOLDOWN = timedelta(seconds=60)

ErrorKind = Literal["quota", "invalid", "server", "other"]

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


class ProviderError(Exception):
    """An HTTP-level failure from a provider. Never carries the request, so the key cannot leak."""

    def __init__(self, status_code: int | None) -> None:
        super().__init__(f"Provider returned HTTP {status_code}")
        self.status_code = status_code


def _status_code(error: Exception) -> int | None:
    if isinstance(error, genai_errors.APIError):
        return error.code
    if isinstance(error, ProviderError):
        return error.status_code
    return None


def _classify_error(error: Exception) -> ErrorKind:
    code = _status_code(error)
    if code in (429, 402):
        return "quota"
    if code in (401, 403):
        return "invalid"
    if code is not None and 500 <= code <= 599:
        return "server"
    return "other"


def _error_code(error: Exception) -> str:
    code = _status_code(error)
    if code is not None:
        return f"HTTP_{code}"
    return type(error).__name__


def _provider_order() -> list[str]:
    return list(PROVIDER_ORDER)


def _env_key(provider: str) -> str:
    settings = get_settings()
    return settings.gemini_api_key if provider == "gemini" else settings.openrouter_api_key


def _acquire_key_sync(exclude_ids: set[str], provider: str | None = None) -> PooledKey | None:
    cipher = KeyCipher(get_settings().key_encryption_secret)
    repository = LlmApiKeyRepository()
    with SessionFactory() as session:
        while True:
            row = repository.acquire(session, datetime.now(timezone.utc), exclude_ids, provider)
            if row is None:
                return None
            try:
                return PooledKey(id=row.id, api_key=cipher.decrypt(row.key_ciphertext, row.key_nonce))
            except (InvalidTag, ValueError):
                # Wrong secret or corrupt row: never hand this key out again in the same call.
                exclude_ids.add(row.id)
                logger.warning("Skipping LLM API key %s: cannot decrypt stored value", row.id)


async def _acquire_key(exclude_ids: set[str], provider: str | None = None) -> PooledKey | None:
    try:
        return await asyncio.to_thread(_acquire_key_sync, exclude_ids, provider)
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


def _generate_gemini_sync(prompt: str, api_key: str | None) -> str:
    client = genai.Client(api_key=api_key)
    try:
        response = client.models.generate_content(model=get_settings().gemini_model, contents=prompt)
        return (response.text or "").strip()
    finally:
        client.close()


def _generate_openrouter_sync(prompt: str, api_key: str | None) -> str:
    settings = get_settings()
    response = httpx.post(
        f"{settings.openrouter_base_url.rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": settings.openrouter_model, "messages": [{"role": "user", "content": prompt}]},
        timeout=settings.gemini_timeout_seconds,
    )
    if response.status_code >= 400:
        raise ProviderError(response.status_code)
    body = response.json()
    if isinstance(body, dict) and body.get("error"):
        # OpenRouter can answer HTTP 200 with an error object; its `code` is an HTTP-like status.
        code = body["error"].get("code") if isinstance(body["error"], dict) else None
        raise ProviderError(code if isinstance(code, int) else None)
    return (body["choices"][0]["message"]["content"] or "").strip()


def _generate_feedback_sync(result: dict[str, Any], api_key: str | None = None, provider: str = "gemini") -> str:
    prompt = _build_prompt(result)
    if provider == "openrouter":
        return _generate_openrouter_sync(prompt, api_key)
    return _generate_gemini_sync(prompt, api_key)


_FAILED_MESSAGE = "Chưa thể tạo nhận xét AI. Vui lòng thử lại."


async def _try_provider(result: dict[str, Any], provider: str) -> tuple[bool, str | None]:
    """Returns (attempted, text). A 5xx stops key rotation for this provider so the caller can switch provider."""
    exclude_ids: set[str] = set()
    attempted = False
    for _ in range(MAX_KEY_ATTEMPTS):
        key = await _acquire_key(exclude_ids, provider)
        if key is None:
            break
        exclude_ids.add(key.id)
        attempted = True
        try:
            text = await asyncio.to_thread(_generate_feedback_sync, result, key.api_key, provider)
        except Exception as error:
            kind, code = _classify_error(error), _error_code(error)
            logger.warning("%s call with LLM API key %s failed (%s, %s)", provider, key.id, kind, code)
            await _record_outcome(key.id, code, kind)
            if kind == "server":
                return True, None  # the provider is down: another key will not help
            continue
        await _record_outcome(key.id, None, None)
        return True, text
    if attempted:
        return True, None
    env_key = _env_key(provider)
    if not env_key:
        return False, None
    try:
        return True, await asyncio.to_thread(_generate_feedback_sync, result, env_key, provider)
    except Exception as error:
        logger.warning("%s call with env API key failed (%s, %s)", provider, _classify_error(error), _error_code(error))
        return True, None


async def _generate_with_pool(result: dict[str, Any]) -> str | None:
    """Returns the generated text, or None when no provider has any key configured."""
    attempted = False
    for provider in _provider_order():
        provider_attempted, text = await _try_provider(result, provider)
        attempted = attempted or provider_attempted
        if text is not None:
            return text
    if attempted:
        raise FeedbackGenerationError("AI_FEEDBACK_FAILED", _FAILED_MESSAGE)
    return None


async def generate_feedback(result: dict[str, Any]) -> str:
    settings = get_settings()
    try:
        text = await asyncio.wait_for(
            _generate_with_pool(result),
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
