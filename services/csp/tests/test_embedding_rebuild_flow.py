"""Blue-green designation, rollback, retention, and keyword fallback."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import app.api.ingestion.search as search_mod
from app.models.embedding_activation import EmbeddingActivation
from app.models.ingestion import IngestionCollection, IngestionDocument
from app.models.model_registry import ModelRegistry
from app.services.auth_service import create_tokens
from app.services.embedding_swap import note_designation, purge_expired, rollback

from tests.conftest import make_user


def _model(db, name: str, *, designated: bool = False) -> ModelRegistry:
    row = ModelRegistry(
        name=name,
        display_name=name,
        model_type="embedding",
        endpoint_url="http://embed.test/v1",
        is_active=True,
        is_platform_embedding=designated,
        embedding_native_dim=768,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _collection(db, user, *, embedding_model: str) -> IngestionCollection:
    coll = IngestionCollection(
        name="法規知識庫",
        chunking_config={"strategy": "semantic"},
        embedding_model=embedding_model,
        embedding_dim=768,
        status="active",
        created_by=user.id,
    )
    db.add(coll)
    db.commit()
    db.refresh(coll)
    return coll


def test_designation_does_not_switch_search(db):
    user = make_user(db, username="swap-owner", role="admin")
    old = _model(db, "old/embed", designated=True)
    new = _model(db, "new/embed")
    _collection(db, user, embedding_model="old/embed")

    started = note_designation(
        db, target_id=new.id, target_name=new.name, previous_name=old.name,
    )
    db.commit()

    row = db.query(EmbeddingActivation).filter(EmbeddingActivation.id == 1).one()
    assert started["rebuild_started"] is True
    assert row.active_model_id == old.id
    assert row.rebuild_target_model_id == new.id
    assert row.rebuild_status == "pending"


def test_rollback_cancels_an_open_rebuild(db):
    user = make_user(db, username="swap-cancel", role="admin")
    old = _model(db, "old/embed", designated=True)
    new = _model(db, "new/embed")
    _collection(db, user, embedding_model="old/embed")
    note_designation(db, target_id=new.id, target_name=new.name, previous_name=old.name)
    db.commit()

    rollback(db)
    db.commit()
    row = db.query(EmbeddingActivation).filter(EmbeddingActivation.id == 1).one()
    assert row.rebuild_status == "cancelled"
    assert row.active_model_id == old.id
    assert row.rebuild_target_model_id is None


def test_rollback_after_switch_restores_the_previous_model(db):
    old = _model(db, "old/embed", designated=False)
    new = _model(db, "new/embed", designated=True)
    now = datetime.now(timezone.utc)
    db.add(
        EmbeddingActivation(
            id=1,
            active_model_id=new.id,
            previous_model_id=old.id,
            switched_at=now,
            rebuild_status="complete",
            rebuild_done=0,
            rebuild_total=0,
            rebuild_errors=0,
        )
    )
    db.commit()

    rollback(db)
    db.commit()
    row = db.query(EmbeddingActivation).filter(EmbeddingActivation.id == 1).one()
    assert row.active_model_id == old.id
    assert row.previous_model_id == new.id
    new_row = db.query(ModelRegistry).filter(ModelRegistry.id == old.id).one()
    assert new_row.is_platform_embedding is True


def test_old_vectors_are_retired_after_seven_days(db):
    old = _model(db, "old/embed")
    new = _model(db, "new/embed", designated=True)
    now = datetime.now(timezone.utc)
    db.add(
        EmbeddingActivation(
            id=1,
            active_model_id=new.id,
            previous_model_id=old.id,
            switched_at=now - timedelta(days=7),
            rebuild_status="complete",
            rebuild_done=1,
            rebuild_total=1,
            rebuild_errors=0,
        )
    )
    db.commit()

    assert purge_expired(db, now=now - timedelta(hours=1)) is None
    purged = purge_expired(db, now=now)
    db.commit()
    assert purged == old.id
    row = db.query(EmbeddingActivation).filter(EmbeddingActivation.id == 1).one()
    assert row.previous_model_id is None


def test_cleanup_waits_while_a_rebuild_is_running(db):
    old = _model(db, "old/embed")
    new = _model(db, "new/embed", designated=True)
    now = datetime.now(timezone.utc)
    db.add(
        EmbeddingActivation(
            id=1,
            active_model_id=old.id,
            previous_model_id=new.id,
            switched_at=now - timedelta(days=8),
            rebuild_target_model_id=new.id,
            rebuild_status="running",
            rebuild_done=0,
            rebuild_total=0,
            rebuild_errors=0,
        )
    )
    db.commit()
    assert purge_expired(db, now=now) is None


def test_keyword_fallback_sets_the_flag_and_ignores_cosine_floor(
    client: TestClient, db, monkeypatch,
):
    user = make_user(db, username="kw-user", role="user")
    _model(db, "nv-embed", designated=True)
    coll = _collection(db, user, embedding_model="nv-embed")
    doc = IngestionDocument(
        collection_id=coll.id,
        filename="rules.pdf",
        sha256="b" * 64,
        mime_type="application/pdf",
        status="indexed",
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)

    class _Chunk:
        def __init__(self):
            self.id = 9
            self.document_id = doc.id
            self.chunk_key = "c1"
            self.content = "差勤管理要事先請假"
            self.metadata = {}
            self.parent_chunk_id = None
            self.chunk_type = "leaf"
            self.chunk_level = 0

    class _Hit:
        def __init__(self):
            self.chunk = _Chunk()
            self.score = 0.2
            self.parent_content = None

    class _Store:
        def __init__(self, *_args, **_kwargs):
            pass

        async def embedding_coverage(self, _model_id):
            return "missing"

        async def keyword_fallback_search(self, _query, top_k=30):
            assert top_k >= 1
            return [_Hit()]

        async def similarity_search(self, **_kwargs):
            raise AssertionError("keyword fallback must not use vector search")

    async def _down(*_args, **_kwargs):
        raise HTTPException(status_code=503, detail="embed down")

    monkeypatch.setattr(search_mod, "_embed_query", _down)
    monkeypatch.setattr(search_mod, "get_pool", lambda: object())
    monkeypatch.setattr(search_mod, "CollectionScopedPgVectorStore", _Store)
    monkeypatch.setattr(search_mod, "maybe_rerank", None, raising=False)

    token = create_tokens(user)["access_token"]
    resp = client.post(
        f"/api/ingestion/collections/{coll.id}/search",
        json={"query": "差勤管理", "top_k": 5, "min_score": 0.9},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["keyword_fallback"] is True
    assert body["keyword_fallback_notice"] == (
        "嵌入模型暫時無法使用，本次用關鍵字搜尋，結果可能較不準"
    )
    assert len(body["results"]) == 1
    assert body["results"][0]["content"] == "差勤管理要事先請假"
