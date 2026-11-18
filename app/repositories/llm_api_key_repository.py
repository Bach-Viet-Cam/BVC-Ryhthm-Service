from datetime import datetime
from typing import Literal
from uuid import uuid4

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.llm_api_key import LlmApiKey


class DuplicateKeyError(Exception):
    def __init__(self, field: Literal["name", "value"]):
        super().__init__(f"Duplicate LLM API key {field}")
        self.field = field


class LlmApiKeyRepository:
    def list(self, session: Session) -> list[LlmApiKey]:
        return list(session.scalars(select(LlmApiKey).order_by(LlmApiKey.created_at, LlmApiKey.name)))

    def get(self, session: Session, key_id: str) -> LlmApiKey | None:
        return session.get(LlmApiKey, key_id)

    def _check_duplicates(self, session: Session, key_id: str | None, name: str | None, key_hmac: str | None) -> None:
        for column, value, field in ((LlmApiKey.name, name, "name"), (LlmApiKey.key_hmac, key_hmac, "value")):
            if value is None:
                continue
            query = select(LlmApiKey.id).where(column == value)
            if key_id is not None:
                query = query.where(LlmApiKey.id != key_id)
            if session.scalar(query.limit(1)) is not None:
                raise DuplicateKeyError(field)

    @staticmethod
    def _field_from_integrity_error(error: IntegrityError) -> Literal["name", "value"]:
        text = str(error.orig).lower()
        return "name" if "name" in text and "hmac" not in text else "value"

    def _commit(self, session: Session) -> None:
        try:
            session.commit()
        except IntegrityError as error:
            session.rollback()
            raise DuplicateKeyError(self._field_from_integrity_error(error)) from None

    def create(self, session: Session, *, name: str, ciphertext: bytes, nonce: bytes, key_hmac: str,
               provider: str = "gemini") -> LlmApiKey:
        self._check_duplicates(session, None, name, key_hmac)
        row = LlmApiKey(id=str(uuid4()), name=name, provider=provider, key_ciphertext=ciphertext,
                        key_nonce=nonce, key_hmac=key_hmac, status="active")
        session.add(row)
        self._commit(session)
        session.refresh(row)
        return row

    def update(self, session: Session, key_id: str, *, name: str | None = None, status: str | None = None,
               ciphertext: bytes | None = None, nonce: bytes | None = None,
               key_hmac: str | None = None) -> LlmApiKey | None:
        row = self.get(session, key_id)
        if row is None:
            return None
        self._check_duplicates(session, key_id, name, key_hmac)
        if name is not None:
            row.name = name
        if status is not None:
            row.status = status
        if ciphertext is not None and nonce is not None and key_hmac is not None:
            row.key_ciphertext, row.key_nonce, row.key_hmac = ciphertext, nonce, key_hmac
            row.status = "active"
            row.cooldown_until = None
            row.last_error_code = None
        self._commit(session)
        session.refresh(row)
        return row

    def delete(self, session: Session, key_id: str) -> bool:
        row = self.get(session, key_id)
        if row is None:
            return False
        session.delete(row)
        session.commit()
        return True

    def acquire(self, session: Session, now: datetime, exclude_ids: set[str],
                provider: str | None = None) -> LlmApiKey | None:
        query = select(LlmApiKey).where(
            LlmApiKey.status == "active",
            or_(LlmApiKey.cooldown_until.is_(None), LlmApiKey.cooldown_until <= now),
        )
        if provider is not None:
            query = query.where(LlmApiKey.provider == provider)
        if exclude_ids:
            query = query.where(LlmApiKey.id.not_in(exclude_ids))
        query = (query.order_by(LlmApiKey.last_used_at.asc().nulls_first(), LlmApiKey.created_at, LlmApiKey.id)
                 .limit(1).with_for_update(skip_locked=True))
        row = session.scalar(query)
        if row is None:
            session.rollback()
            return None
        row.last_used_at = now
        session.commit()
        session.refresh(row)
        return row

    def record_success(self, session: Session, key_id: str) -> None:
        row = self.get(session, key_id)
        if row is None:
            return
        row.success_count = (row.success_count or 0) + 1
        row.last_error_code = None
        session.commit()

    def record_failure(self, session: Session, key_id: str, error_code: str, *,
                       cooldown_until: datetime | None = None, invalid: bool = False) -> None:
        row = self.get(session, key_id)
        if row is None:
            return
        row.fail_count = (row.fail_count or 0) + 1
        row.last_error_code = error_code
        if cooldown_until is not None:
            row.cooldown_until = cooldown_until
        if invalid:
            row.status = "invalid"
        session.commit()
