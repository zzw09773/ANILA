"""``trusted_host_service`` cache / backfill / hook behaviour.

DB-touching paths are exercised through the existing ``db`` fixture
(SQLite, table-by-table create — TrustedHost has no JSONB so SQLite
handles it cleanly, unlike platform_links which is what blocks
``client``-fixture tests).
"""
from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from app.services import trusted_host_service
from app.models.trusted_host import TrustedHost
from tests.conftest import make_user


@pytest.fixture(autouse=True)
def _reset_cache():
    """Drop cached state between tests so TTL math is deterministic."""
    trusted_host_service._invalidate_cache()
    yield
    trusted_host_service._invalidate_cache()


@pytest.fixture(autouse=True)
def _patch_session_local(db_engine, monkeypatch):
    """Point ``trusted_host_service.SessionLocal`` at the in-memory test
    engine so ``get_cached_hosts()`` reads the same tables that the ``db``
    fixture has created. Without this, the service opens sessions on the
    file-backed SQLite from ``DATABASE_URL`` env, which has no
    ``trusted_hosts`` table → cache reads silently fall back to empty.
    """
    from sqlalchemy.orm import sessionmaker

    SessionFactory = sessionmaker(bind=db_engine)
    monkeypatch.setattr(trusted_host_service, "SessionLocal", SessionFactory)
    yield


# ── normalize / CRUD ──────────────────────────────────────────────────────────


def test_normalize_lowercases_and_strips():
    assert trusted_host_service._normalize("  GEMMA4 ") == "gemma4"
    assert trusted_host_service._normalize("Host.Docker.Internal") == "host.docker.internal"


def test_add_host_idempotent(db):
    owner = make_user(db, username="owner", role="owner")
    row1 = trusted_host_service.add_host(db, host="gemma4", note="n1", actor=owner)
    row2 = trusted_host_service.add_host(db, host="gemma4", note="n2", actor=owner)
    assert row1.id == row2.id
    # 重複 add 不該炸 — note 會更新
    assert row2.note == "n2"


def test_add_host_normalises_case_to_lowercase(db):
    owner = make_user(db, username="owner", role="owner")
    row = trusted_host_service.add_host(db, host="GEMMA4", note=None, actor=owner)
    assert row.host == "gemma4"


def test_remove_host(db):
    owner = make_user(db, username="owner", role="owner")
    row = trusted_host_service.add_host(db, host="gemma4", note=None, actor=owner)
    assert trusted_host_service.remove_host(db, host_id=row.id, actor=owner) is True
    assert db.query(TrustedHost).filter_by(id=row.id).first() is None


def test_remove_nonexistent_returns_false(db):
    owner = make_user(db, username="owner", role="owner")
    assert trusted_host_service.remove_host(db, host_id=9999, actor=owner) is False


# ── cache TTL behaviour ───────────────────────────────────────────────────────


def test_get_cached_hosts_reads_db_first_call(db):
    owner = make_user(db, username="owner", role="owner")
    trusted_host_service.add_host(db, host="gemma4", note=None, actor=owner)
    # mutation 已 invalidate cache,下次 get_cached_hosts 應該抓到
    assert "gemma4" in trusted_host_service.get_cached_hosts()


def test_get_cached_hosts_within_ttl_doesnt_re_query(db, monkeypatch):
    """TTL 內第二次呼叫不該再打 DB(避免熱路徑爆 DB)。"""
    owner = make_user(db, username="owner", role="owner")
    trusted_host_service.add_host(db, host="gemma4", note=None, actor=owner)
    trusted_host_service.get_cached_hosts()  # warm cache

    call_count = {"n": 0}
    original = trusted_host_service._load_hosts_from_db

    def _counting(*args, **kwargs):
        call_count["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(trusted_host_service, "_load_hosts_from_db", _counting)
    for _ in range(5):
        trusted_host_service.get_cached_hosts()
    assert call_count["n"] == 0  # cache hit,沒打 DB


def test_mutation_invalidates_cache_immediately(db):
    """add_host 後立即 get_cached_hosts 應該看得到新 host(不用等 TTL)。"""
    owner = make_user(db, username="owner", role="owner")
    trusted_host_service.add_host(db, host="alpha", note=None, actor=owner)
    cached_before = trusted_host_service.get_cached_hosts()
    assert "alpha" in cached_before
    assert "beta" not in cached_before

    trusted_host_service.add_host(db, host="beta", note=None, actor=owner)
    cached_after = trusted_host_service.get_cached_hosts()
    assert "beta" in cached_after


def test_db_failure_returns_last_snapshot(db, monkeypatch):
    """DB 抖一下時返回 last good snapshot 而不是炸掉(fail-safe)。"""
    owner = make_user(db, username="owner", role="owner")
    trusted_host_service.add_host(db, host="cached-host", note=None, actor=owner)
    # 灌一次 cache
    first = trusted_host_service.get_cached_hosts()
    assert "cached-host" in first

    # 模擬 TTL 過 + DB session 失敗
    trusted_host_service._invalidate_cache()

    def _exploding_session():
        raise RuntimeError("DB connection failed")

    monkeypatch.setattr(trusted_host_service, "SessionLocal", _exploding_session)
    fallback = trusted_host_service.get_cached_hosts()
    # 不該 raise;應該返回上次成功 snapshot
    assert "cached-host" in fallback


# ── env backfill ──────────────────────────────────────────────────────────────


def test_backfill_from_env_inserts_missing(db, monkeypatch):
    monkeypatch.setenv(
        "ANILA_TRUSTED_HOSTS",
        "gemma4,gpt-oss-20b,nv-embed-proxy",
    )
    inserted = trusted_host_service.backfill_from_env(db)
    assert inserted == 3
    hosts = {r.host for r in db.query(TrustedHost).all()}
    assert hosts == {"gemma4", "gpt-oss-20b", "nv-embed-proxy"}


def test_backfill_from_env_idempotent(db, monkeypatch):
    """重跑 backfill 不該複製 — 第二次 inserted=0。"""
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "gemma4,gpt-oss-20b")
    first = trusted_host_service.backfill_from_env(db)
    second = trusted_host_service.backfill_from_env(db)
    assert first == 2
    assert second == 0


def test_backfill_empty_env_returns_zero(db, monkeypatch):
    monkeypatch.delenv("ANILA_TRUSTED_HOSTS", raising=False)
    assert trusted_host_service.backfill_from_env(db) == 0
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "")
    assert trusted_host_service.backfill_from_env(db) == 0
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", " , , ")
    assert trusted_host_service.backfill_from_env(db) == 0


def test_backfill_records_note_for_audit(db, monkeypatch):
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "gemma4")
    trusted_host_service.backfill_from_env(db)
    row = db.query(TrustedHost).filter_by(host="gemma4").first()
    assert row is not None
    assert "imported from ANILA_TRUSTED_HOSTS" in row.note
    assert row.created_by_user_id is None  # backfill 沒有 actor


# ── url_guard hook integration ────────────────────────────────────────────────


def test_register_with_url_guard_wires_provider(db, monkeypatch):
    """register_with_url_guard 應該把 cache provider 灌進 anila-core,
    讓 validate_outbound_url 走 DB 看到 host。"""
    from anila_core.security import (
        UnsafeEndpointError,
        clear_trusted_host_providers,
        validate_outbound_url,
    )

    clear_trusted_host_providers()
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.delenv("ANILA_TRUSTED_HOSTS", raising=False)
    trusted_host_service._invalidate_cache()

    # 沒註冊時 single-label 被擋
    with pytest.raises(UnsafeEndpointError):
        validate_outbound_url("http://hooked-host:8000/v1")

    owner = make_user(db, username="owner", role="owner")
    trusted_host_service.add_host(db, host="hooked-host", note=None, actor=owner)
    trusted_host_service.register_with_url_guard()

    # 註冊後就放行
    validate_outbound_url("http://hooked-host:8000/v1")

    clear_trusted_host_providers()
