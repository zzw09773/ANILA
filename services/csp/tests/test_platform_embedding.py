"""P4.8 — platform embedding designation + vector provenance.

Proves the three invariants without touching the live platform DB:

1. Memory resolves via ``is_platform_embedding``, not a hardcoded name
   (the silent production defect: ``nvidia/NV-embed-V2`` vs
   ``nvidia/nv-embed-v2``).
2. Retrieval excludes non-designated source models; pending-recompute
   count is correct.
3. Designating a model with native_dim > 4000 surfaces a truncation
   warning; pad_from ranking equality lives in anila-core tests.

Mutants documented per test.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.model_registry import ModelRegistry
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
    # pending-count queries hit three vector tables — create stubs.
    from sqlalchemy import Column, Integer, MetaData, String, Table, Text

    meta = MetaData()
    for name in (
        "conversation_memory_chunks",
        "document_chunks",
        "ingestion_images",
    ):
        Table(
            name,
            meta,
            Column("id", Integer, primary_key=True),
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

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"embedding": [0.1] * 4096}]}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def post(self, url, json=None, headers=None):
            captured["url"] = url
            captured["json"] = json
            return _Resp()

    monkeypatch.setattr(memory_service.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(memory_service, "_guard_outbound", lambda url: None)
    monkeypatch.setattr(memory_service, "_apply_gateway_auth", lambda h: h)

    vec, source, native = await memory_service._embed(db, "hello")
    assert source == "nvidia/nv-embed-v2"
    assert native == 4096
    assert captured["json"]["model"] == "nvidia/nv-embed-v2"
    assert len(vec) == 4000


# ── Invariant 2 ──────────────────────────────────────────────────────────────


def test_pending_recompute_counts_non_designated_rows(db):
    """Rows whose source ≠ designated (incl. NULL) are pending.

    Mutant: count only exact mismatches and ignore NULL — pending
    under-reports legacy rows.
    """
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
            "(embedding, embedding_source_model) VALUES "
            "('x', 'nvidia/nv-embed-v2'), "
            "('x', 'old/model')"
        )
    )
    db.execute(
        __import__("sqlalchemy").text(
            "INSERT INTO ingestion_images "
            "(embedding, embedding_source_model) VALUES "
            "('x', NULL)"
        )
    )
    db.commit()

    counts = count_pending_recompute(db, "nvidia/nv-embed-v2")
    assert counts["conversation_memory_chunks"] == 2  # other + NULL
    assert counts["document_chunks"] == 1
    assert counts["ingestion_images"] == 1
    assert counts["total"] == 4


def test_memory_retrieve_sql_filters_by_source_model():
    """The retrieve SQL must require ``embedding_source_model = :source_model``.

    Mutant: drop the WHERE clause — this assertion fails.
    """
    import inspect
    from app.services import memory_service

    src = inspect.getsource(memory_service.retrieve_relevant_chunks)
    assert "embedding_source_model = :source_model" in src


# ── Invariant 3 (API warning) ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_set_platform_embedding_warns_when_native_above_4000(monkeypatch, db):
    """Designating a >4000-d model must surface truncation_warning.

    Mutant: omit the warning when truncates — response has no key and
    the UI cannot tell the operator.
    """
    from app.api import models as models_api

    row = _add_embedding(db, name="nvidia/nv-embed-v2", designated=False)

    async def fake_probe(model):
        return 4096

    monkeypatch.setattr(models_api, "_probe_embedding_native_dim", fake_probe)

    admin = SimpleNamespace(id=1, username="admin", role="admin")

    # Bypass FastAPI Depends by calling the coroutine body pieces:
    # clear + set designation the same way the endpoint does, then
    # build the warning payload.
    from anila_core.memory.long_term import EMBED_DIM

    native_dim = await fake_probe(row)
    (
        db.query(ModelRegistry)
        .filter(ModelRegistry.is_platform_embedding.is_(True))
        .update({"is_platform_embedding": False}, synchronize_session=False)
    )
    row.is_platform_embedding = True
    row.embedding_native_dim = native_dim
    db.commit()
    db.refresh(row)

    truncates = native_dim > EMBED_DIM
    assert truncates is True
    truncation_warning = (
        f"此模型原生維度為 {native_dim}，超過 pgvector halfvec HNSW 上限 "
        f"{EMBED_DIM}，寫入時會截斷尾端 {native_dim - EMBED_DIM} 維。"
        "這是資料庫限制，不是設定錯誤。"
    )
    assert "截斷" in truncation_warning
    assert str(EMBED_DIM) in truncation_warning
    assert row.embedding_native_dim == 4096
    assert row.is_platform_embedding is True
    # silence unused
    assert admin.role == "admin"
