from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.llm_api_key import LlmApiKey  # noqa: F401  (registers table)
from app.repositories.llm_api_key_repository import DuplicateKeyError, LlmApiKeyRepository

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture()
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as s:
        yield s


repo = LlmApiKeyRepository()


def make(session, name, hmac=None):
    return repo.create(session, name=name, ciphertext=b"ct-" + name.encode(), nonce=b"n" * 12,
                       key_hmac=hmac or uuid4().hex)


def test_create_and_list_returns_rows_without_plaintext(session):
    a = make(session, "a")
    b = make(session, "b")
    rows = repo.list(session)
    assert [r.id for r in rows] == [a.id, b.id]
    assert rows[0].status == "active" and rows[0].key_ciphertext == b"ct-a"
    assert not hasattr(rows[0], "plaintext") and not hasattr(rows[0], "api_key")
    assert repo.get(session, a.id).name == "a"
    assert repo.get(session, "missing") is None


def test_duplicate_name_and_duplicate_hmac_raise_with_field(session):
    make(session, "a", "h1")
    with pytest.raises(DuplicateKeyError) as e:
        make(session, "a", "h2")
    assert e.value.field == "name"
    with pytest.raises(DuplicateKeyError) as e:
        make(session, "b", "h1")
    assert e.value.field == "value"
    b = make(session, "b", "h3")
    with pytest.raises(DuplicateKeyError) as e:
        repo.update(session, b.id, name="a")
    assert e.value.field == "name"
    with pytest.raises(DuplicateKeyError) as e:
        repo.update(session, b.id, key_hmac="h1")
    assert e.value.field == "value"
    assert len(repo.list(session)) == 2


def test_acquire_rotates_least_recently_used(session):
    ids = {make(session, n).id for n in "abc"}
    got = [repo.acquire(session, NOW + timedelta(seconds=i), set()).id for i in range(3)]
    assert set(got) == ids
    assert repo.acquire(session, NOW + timedelta(seconds=10), set()).id == got[0]


def test_acquire_skips_cooldown_disabled_invalid_and_excluded(session):
    cool, dis, inv, exc, ok = (make(session, n) for n in ("cool", "dis", "inv", "exc", "ok"))
    repo.record_failure(session, cool.id, "429", cooldown_until=NOW + timedelta(minutes=5))
    repo.update(session, dis.id, status="disabled")
    repo.record_failure(session, inv.id, "401", invalid=True)
    assert repo.acquire(session, NOW, {exc.id}).id == ok.id
    assert repo.get(session, ok.id).last_used_at is not None


def test_acquire_returns_none_when_pool_empty_or_all_unavailable(session):
    assert repo.acquire(session, NOW, set()) is None
    a = make(session, "a")
    assert repo.acquire(session, NOW, {a.id}) is None
    repo.update(session, a.id, status="disabled")
    assert repo.acquire(session, NOW, set()) is None


def test_record_failure_cooldown_then_acquirable_after_expiry(session):
    a = make(session, "a")
    until = NOW + timedelta(minutes=5)
    repo.record_failure(session, a.id, "429", cooldown_until=until)
    row = repo.get(session, a.id)
    assert row.fail_count == 1 and row.last_error_code == "429" and row.status == "active"
    assert repo.acquire(session, NOW, set()) is None
    assert repo.acquire(session, until + timedelta(seconds=1), set()).id == a.id
    repo.record_success(session, a.id)
    row = repo.get(session, a.id)
    assert row.success_count == 1 and row.last_error_code is None


def test_record_failure_invalid_removes_from_pool(session):
    a = make(session, "a")
    repo.record_failure(session, a.id, "401", invalid=True)
    assert repo.get(session, a.id).status == "invalid"
    assert repo.acquire(session, NOW, set()) is None


def test_update_with_new_key_reactivates_invalid_key(session):
    a = make(session, "a", "h1")
    repo.record_failure(session, a.id, "401", cooldown_until=NOW + timedelta(hours=1), invalid=True)
    updated = repo.update(session, a.id, ciphertext=b"new", nonce=b"m" * 12, key_hmac="h2")
    assert updated.status == "active" and updated.cooldown_until is None and updated.last_error_code is None
    assert updated.key_ciphertext == b"new" and updated.key_hmac == "h2"
    assert repo.acquire(session, NOW, set()).id == a.id
    assert repo.update(session, "missing", name="x") is None


def test_delete_returns_false_for_unknown_id(session):
    a = make(session, "a")
    assert repo.delete(session, "missing") is False
    assert repo.delete(session, a.id) is True
    assert repo.get(session, a.id) is None
