"""P4.8 — platform embedding designation + vector provenance.

Proves the three invariants without touching the live platform DB:

1. Memory resolves via ``is_platform_embedding``, not a hardcoded name
   (the silent production defect: ``nvidia/NV-embed-V2`` vs
   ``nvidia/nv-embed-v2``).
2. Retrieval excludes non-designated source models; pending-recompute
   count is correct.
3. Designating a model with native_dim > 4000 surfaces a truncation
   warning — now proved against the live endpoint in
   ``test_platform_embedding_designation.py``; pad_from ranking equality
   lives in anila-core tests.

Mutants documented per test.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.model_registry import ModelRegistry
from app.models.platform_setting import PlatformSetting
from app.models.user import User
from app.services.platform_embedding import (
    count_pending_recompute,
    resolve_platform_embedding,
)


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # Minimal tables for these unit tests.
    ModelRegistry.__table__.create(bind=engine, checkfirst=True)
    # ``_embed`` 現在會在出向之前解一次逾時／重試（``platform_settings`` → env →
    # 程式預設），所以那張表也要在最小 schema 裡；沒有列時走的就是後兩層。
    PlatformSetting.__table__.create(bind=engine, checkfirst=True)
    # pending-count queries hit three vector tables — create stubs.
    from sqlalchemy import Column, Integer, MetaData, String, Table, Text

    meta = MetaData()
    Table(
        "ingestion_collections",
        meta,
        Column("id", Integer, primary_key=True),
    )
    for name in (
        "conversation_memory_chunks",
        "document_chunks",
        "ingestion_images",
    ):
        Table(
            name,
            meta,
            Column("id", Integer, primary_key=True),
            Column("collection_id", Integer),
            Column("embedding", Text),
            Column("embedding_source_model", String(200)),
            Column("embedding_native_dim", Integer),
        )
    meta.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _add_embedding(db, *, name: str, designated: bool = False, native: int | None = None):
    row = ModelRegistry(
        name=name,
        display_name=name,
        model_type="embedding",
        endpoint_url="http://embed.test/v1",
        is_active=True,
        is_platform_embedding=designated,
        embedding_native_dim=native,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


# ── Invariant 1 ──────────────────────────────────────────────────────────────


def test_resolve_uses_designation_not_hardcoded_name(db):
    """The production defect: hardcoded ``nvidia/NV-embed-V2`` never
    matched registered ``nvidia/nv-embed-v2``. Designation must win
    regardless of case.

    Mutant: fall back to ``DEFAULT_EMBED_MODEL`` exact-name lookup —
    this test stays green only if the designated lowercase row is what
    resolve returns (revert the designation filter → fails).
    """
    _add_embedding(db, name="nvidia/nv-embed-v2", designated=True, native=4096)
    # A differently-cased decoy that the old hardcoded path would prefer.
    _add_embedding(db, name="nvidia/NV-embed-V2", designated=False, native=4096)

    resolved = resolve_platform_embedding(db)
    assert resolved is not None
    assert resolved.name == "nvidia/nv-embed-v2"
    assert resolved.native_dim == 4096
    assert resolved.truncates is True


def test_resolve_falls_back_to_first_active_embedding(db):
    """No designation yet → first active embedding (no new gate)."""
    _add_embedding(db, name="other/embed", designated=False, native=2048)
    resolved = resolve_platform_embedding(db)
    assert resolved is not None
    assert resolved.name == "other/embed"
    assert resolved.pad_from == 2048


@pytest.mark.asyncio
async def test_memory_embed_uses_designated_name_ignoring_case_env(db, monkeypatch):
    """End-to-end of the original defect path: even if env still holds
    the old hardcoded casing, ``_embed`` must POST the designated
    registry name.

    Mutant: restore ``_EMBED_MODEL_NAME`` exact lookup in ``_embed`` —
    the captured request model becomes the env string and this fails.
    """
    from app.services import memory_service

    _add_embedding(db, name="nvidia/nv-embed-v2", designated=True, native=4096)
    monkeypatch.setenv("MEMORY_EMBEDDING_MODEL", "nvidia/NV-embed-V2")
    # Re-read would not matter — _embed no longer consults the env.

    captured: dict = {}

    async def fake_proxy_request(**kwargs):
        captured["json"] = kwargs.get("request_body")
        captured["role"] = kwargs.get("embedding_input_role")
        return {"data": [{"embedding": [0.1] * 4096}]}

    import app.services.proxy.service as proxy_svc

    monkeypatch.setattr(proxy_svc, "proxy_request", fake_proxy_request)

    vec, source, native = await memory_service._embed(db, "hello")
    assert source == "nvidia/nv-embed-v2"
    assert native == 4096
    assert captured["json"]["model"] == "nvidia/nv-embed-v2"
    assert captured["role"] == "query"
    assert len(vec) == 4000


# ── Invariant 2 ──────────────────────────────────────────────────────────────


def test_pending_recompute_counts_non_designated_rows(db):
    """Rows whose source ≠ designated (incl. NULL) are pending.

    Mutant: count only exact mismatches and ignore NULL — pending
    under-reports legacy rows.
    """
    db.execute(
        __import__("sqlalchemy").text(
            "INSERT INTO ingestion_collections (id) VALUES (1), (2)"
        )
    )
    db.execute(
        __import__("sqlalchemy").text(
            "INSERT INTO conversation_memory_chunks "
            "(embedding, embedding_source_model) VALUES "
            "('x', 'nvidia/nv-embed-v2'), "
            "('x', 'other/model'), "
            "('x', NULL)"
        )
    )
    db.execute(
        __import__("sqlalchemy").text(
            "INSERT INTO document_chunks "
            "(collection_id, embedding, embedding_source_model) VALUES "
            "(1, 'x', 'nvidia/nv-embed-v2'), "
            "(2, 'x', 'old/model')"
        )
    )
    db.execute(
        __import__("sqlalchemy").text(
            "INSERT INTO ingestion_images "
            "(collection_id, embedding, embedding_source_model) VALUES "
            "(2, 'x', NULL)"
        )
    )
    db.commit()

    counts = count_pending_recompute(db, "nvidia/nv-embed-v2")
    assert counts["conversation_memory_chunks"] == 2  # other + NULL
    assert counts["document_chunks"] == 1
    assert counts["ingestion_images"] == 1
    assert counts["total"] == 4


class _RlsAwareSession:
    """Small DB double that models FORCE-RLS visibility by collection GUC."""

    _rows = {
        7: {"document_chunks": 2, "ingestion_images": 0},
        9: {"document_chunks": 0, "ingestion_images": 3},
    }

    def __init__(self, rows=None):
        self.rows = self._rows if rows is None else rows
        self.collection_scope = None
        self.execute_calls = 0

    def get_bind(self):
        from types import SimpleNamespace

        return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    def execute(self, statement, params=None):
        self.execute_calls += 1
        sql = str(statement)
        if "FROM ingestion_collections" in sql:
            return _ScalarRows(sorted(self.rows))
        if "set_config('anila.collection_id'" in sql:
            self.collection_scope = (
                int(params["cid"])
                if params and "cid" in params
                else None
            )
            return _ScalarRows([])
        if "FROM conversation_memory_chunks" in sql:
            return _ScalarRows([], scalar=1)
        for table in ("document_chunks", "ingestion_images"):
            if "FROM " + table in sql:
                visible = self.rows.get(self.collection_scope, {}).get(table, 0)
                return _ScalarRows([], scalar=visible)
        raise AssertionError("unexpected SQL: " + sql)


class _ScalarRows:
    def __init__(self, rows, scalar=None):
        self._rows = rows
        self._scalar = scalar

    def scalars(self):
        return self

    def all(self):
        return self._rows

    def scalar(self):
        return self._scalar


def test_pending_recompute_sets_each_collection_scope_before_counting():
    """FORCE-RLS rows must be counted, not silently reported as zero.

    Mutant: omit scope_collection_rls — both scoped table counts become zero
    in this RLS-aware double while the test still exercises values.
    """
    session = _RlsAwareSession()
    counts = count_pending_recompute(session, "new/embedder")
    assert counts == {
        "conversation_memory_chunks": 1,
        "document_chunks": 2,
        "ingestion_images": 3,
        "total": 6,
    }
    assert session.collection_scope is None


def test_pending_recompute_round_trip_cost_at_100_collections():
    """Keep the measured N+1 cost visible while the count remains exact."""
    session = _RlsAwareSession(
        {
            collection_id: {"document_chunks": 0, "ingestion_images": 0}
            for collection_id in range(1, 101)
        }
    )

    count_pending_recompute(session, "new/embedder")

    # 1 memory count + 1 collection list + (1 scope + 2 counts) * 100 + 1 cleanup.
    assert session.execute_calls == 303


class _OriginalAndCleanupFailSession(_RlsAwareSession):
    """DB double for the exception-preservation control-flow guard."""

    def execute(self, statement, params=None):
        sql = str(statement)
        if "set_config('anila.collection_id'" in sql:
            if params and "cid" in params:
                return super().execute(statement, params)
            raise RuntimeError("cleanup failed")
        if "FROM document_chunks" in sql:
            raise RuntimeError("original query failed")
        return super().execute(statement, params)


def test_pending_recompute_preserves_original_error_if_cleanup_fails():
    """Best-effort PostgreSQL cleanup must not replace the query error.

    The double reports a PostgreSQL dialect only to enter the cleanup branch;
    it is not a PostgreSQL transaction. Tests run on SQLite, so the real
    transaction-aborted cleanup failure cannot be reproduced here. This guard
    proves only that the cleanup exception is swallowed instead of shadowing
    the original exception.
    """
    with pytest.raises(RuntimeError, match="original query failed"):
        count_pending_recompute(_OriginalAndCleanupFailSession(), "new/embedder")


def test_memory_retrieve_sql_filters_by_source_model():
    """The retrieve SQL must require ``embedding_source_model = :source_model``.

    Mutant: drop the WHERE clause — this assertion fails.
    """
    import inspect
    from app.services import memory_service

    src = inspect.getsource(memory_service.retrieve_relevant_chunks)
    assert "embedding_source_model = :source_model" in src


# ── Invariant 3 (API warning) ────────────────────────────────────────────────
#
# Lives in ``test_platform_embedding_designation.py`` now, against the real
# ``POST /api/models/{id}/set-platform-embedding`` endpoint.
#
# What used to be here was a tautology: it never called the endpoint. It
# re-ran the designation UPDATE by hand, then built the truncation warning
# string itself and asserted on its own literal. Replacing the production
# ``set_platform_embedding`` with a function that only raises left this
# file entirely green — the test could not observe the endpoint at all,
# so no change to it could ever be caught here.
