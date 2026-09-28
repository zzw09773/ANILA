"""知識庫搜尋沒有平台嵌入角色時，不准猜第一個啟用的嵌入模型。"""
from __future__ import annotations

import os

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

from fastapi.testclient import TestClient

import app.api.ingestion.search as search_mod
from app.models.ingestion import IngestionCollection, IngestionDocument
from app.models.model_registry import ModelRegistry
from app.services.auth_service import create_tokens
from tests.conftest import make_user

UNSET = "平台嵌入模型尚未在治理中心設定"


def _bearer(user) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_tokens(user)['access_token']}"}


def _collection(db, owner) -> IngestionCollection:
    coll = IngestionCollection(
        name="role-unset-kb",
        chunking_config={"strategy": "semantic"},
        embedding_model="stored-on-the-collection",
        embedding_dim=8,
        status="active",
        created_by=owner.id,
    )
    db.add(coll)
    db.commit()
    db.refresh(coll)
    db.add(
        IngestionDocument(
            collection_id=coll.id,
            filename="note.pdf",
            sha256="c" * 64,
            mime_type="application/pdf",
            status="indexed",
        )
    )
    db.commit()
    return coll


def _active_embedding(db, name: str) -> None:
    db.add(
        ModelRegistry(
            name=name,
            display_name=name,
            model_type="embedding",
            endpoint_url="http://embed.test/v1",
            is_active=True,
            is_platform_embedding=False,
        )
    )
    db.commit()


class _HitChunk:
    def __init__(self) -> None:
        self.id = 1
        self.document_id = 1
        self.chunk_key = "chunk:1"
        self.content = "should-not-be-returned"
        self.metadata = {}
        self.parent_chunk_id = None
        self.chunk_type = "leaf"
        self.chunk_level = 0


class _Store:
    async def similarity_search(self, **_kwargs):
        return [type("Hit", (), {"chunk": _HitChunk(), "score": 0.9, "parent_content": None})()]


def test_chunk_search_refuses_when_role_unset_even_if_another_embedder_is_active(
    client: TestClient, db, monkeypatch,
):
    """有啟用的嵌入模型、知識庫上也寫了模型名，角色沒設仍是 409，而且不去嵌入。"""
    user = make_user(db, username="search-role", role="user")
    coll = _collection(db, user)
    _active_embedding(db, "first-active-embedder")
    called: list[str] = []

    async def fake_embed(db_, user_, model_name, dim, query):
        called.append(model_name)
        return [0.1] * dim

    monkeypatch.setattr(search_mod, "_embed_query", fake_embed)
    monkeypatch.setattr(search_mod, "get_pool", lambda: object())
    monkeypatch.setattr(
        search_mod,
        "CollectionScopedPgVectorStore",
        lambda pool, collection_id: _Store(),
    )

    resp = client.post(
        f"/api/ingestion/collections/{coll.id}/search",
        json={"query": "任何問題"},
        headers=_bearer(user),
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == UNSET
    assert called == []


def test_image_search_refuses_when_role_unset(client: TestClient, db, monkeypatch):
    user = make_user(db, username="image-role", role="user")
    coll = _collection(db, user)
    _active_embedding(db, "first-active-embedder")
    called: list[str] = []

    async def fake_embed(db_, user_, model_name, dim, query):
        called.append(model_name)
        return [0.1] * dim

    class _Txn:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class _Conn:
        async def execute(self, _sql):
            return None

        def transaction(self):
            return _Txn()

        async def fetch(self, _sql, *_args):
            return [
                {
                    "pk_id": 1,
                    "image_id": "img",
                    "document_id": 1,
                    "page": 1,
                    "storage_path": "x.png",
                    "mime": "image/png",
                    "caption": "should-not-be-returned",
                    "filename": "note.pdf",
                    "dist": 0.1,
                }
            ]

    class _Pool:
        def acquire(self):
            conn = _Conn()

            class _Acq:
                async def __aenter__(self):
                    return conn

                async def __aexit__(self, *exc):
                    return False

            return _Acq()

    monkeypatch.setattr(search_mod, "_embed_query", fake_embed)
    monkeypatch.setattr(search_mod, "get_pool", lambda: _Pool())

    resp = client.post(
        f"/api/ingestion/collections/{coll.id}/images/search",
        json={"query": "圖"},
        headers=_bearer(user),
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == UNSET
    assert called == []
