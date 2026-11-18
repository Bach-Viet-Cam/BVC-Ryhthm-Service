from collections.abc import Callable

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from sqlalchemy.orm import Session

from app import config
from app.database import get_session
from app.models.llm_api_key import LlmApiKey
from app.repositories.llm_api_key_repository import DuplicateKeyError, LlmApiKeyRepository
from app.schemas.llm_keys import LlmKeyCreate, LlmKeyListResponse, LlmKeyResponse, LlmKeyUpdate
from app.security.key_cipher import KeyCipher, KeyCipherUnavailable

PREFIX = "/api/v1/admin/llm-keys"


def _to_response(row: LlmApiKey) -> LlmKeyResponse:
    return LlmKeyResponse(
        id=row.id, name=row.name, provider=row.provider, status=row.status, cooldownUntil=row.cooldown_until,
        lastUsedAt=row.last_used_at, lastErrorCode=row.last_error_code,
        successCount=row.success_count or 0, failCount=row.fail_count or 0, createdAt=row.created_at)


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail={"code": "LLM_KEY_NOT_FOUND", "message": "Không tìm thấy API key."})


def _duplicate(error: DuplicateKeyError) -> HTTPException:
    if error.field == "name":
        return HTTPException(status_code=409, detail={"code": "LLM_KEY_DUPLICATE_NAME", "message": "Tên API key đã tồn tại."})
    return HTTPException(status_code=409, detail={"code": "LLM_KEY_DUPLICATE_VALUE", "message": "API key này đã tồn tại."})


def _encrypt(key: str) -> tuple[bytes, bytes, str]:
    try:
        cipher = KeyCipher(config.get_settings().key_encryption_secret)
    except KeyCipherUnavailable:
        raise HTTPException(status_code=503, detail={
            "code": "LLM_KEY_ENCRYPTION_UNAVAILABLE",
            "message": "Chưa cấu hình khóa mã hóa API key."}) from None
    ciphertext, nonce = cipher.encrypt(key)
    return ciphertext, nonce, cipher.fingerprint(key)


def create_llm_key_router(require_auth: Callable[[str | None], None]) -> APIRouter:
    def authenticated(authorization: str | None = Header(default=None)) -> None:
        require_auth(authorization)

    router = APIRouter(prefix=PREFIX, dependencies=[Depends(authenticated)])
    repository = LlmApiKeyRepository()

    @router.get("", response_model=LlmKeyListResponse)
    def list_keys(session: Session = Depends(get_session)) -> LlmKeyListResponse:
        return LlmKeyListResponse(items=[_to_response(row) for row in repository.list(session)])

    @router.post("", response_model=LlmKeyResponse, status_code=201)
    def create_key(body: LlmKeyCreate, session: Session = Depends(get_session)) -> LlmKeyResponse:
        ciphertext, nonce, key_hmac = _encrypt(body.key)
        try:
            row = repository.create(session, name=body.name, ciphertext=ciphertext, nonce=nonce, key_hmac=key_hmac,
                                       provider=body.provider)
        except DuplicateKeyError as error:
            raise _duplicate(error) from None
        return _to_response(row)

    @router.patch("/{key_id}", response_model=LlmKeyResponse)
    def update_key(key_id: str, body: LlmKeyUpdate, session: Session = Depends(get_session)) -> LlmKeyResponse:
        if repository.get(session, key_id) is None:
            raise _not_found()
        secret = {}
        if body.key is not None:
            ciphertext, nonce, key_hmac = _encrypt(body.key)
            secret = {"ciphertext": ciphertext, "nonce": nonce, "key_hmac": key_hmac}
        try:
            row = repository.update(session, key_id, name=body.name, status=body.status, **secret)
        except DuplicateKeyError as error:
            raise _duplicate(error) from None
        if row is None:
            raise _not_found()
        return _to_response(row)

    @router.delete("/{key_id}", status_code=204)
    def delete_key(key_id: str, session: Session = Depends(get_session)) -> Response:
        if not repository.delete(session, key_id):
            raise _not_found()
        return Response(status_code=204)

    return router
