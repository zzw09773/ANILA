"""Contract tests for ``POST /api/ingestion/collections/{id}/search`` (chunk search).

This endpoint is the second public HTTP surface ``anila-studio`` will call
after extraction (alongside the new image search + image blob endpoints).
The contract here is **frozen** — anila-studio's ``csp_client.search_chunks``
will hard-code the same request/response shape.

Locks:
- request body shape (query / top_k / min_score / document_ids)
- response shape (query / embedding_model / embedding_dim / results[] with
  chunk_id / document_id / filename / chunk_key / content / score /
  metadata / parent_chunk_id / parent_content / chunk_type / chunk_level)
- auth (owner OR admin; 403 cross-user; 404 if collection missing)
- 409 if collection.status != "active"

The pgvector layer is monkeypatched (asyncpg unavailable under SQLite test
fixture); ``_embed_query`` is stubbed; ``similarity_search`` returns fixture
hits so the endpoint glue itself is what gets exercised.
"""
from __future__ import annotations

import os

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import pytest
from fastapi.testclient import TestClient

import app.api.ingestion.search as search_mod
from app.models.ingestion import IngestionCollection, IngestionDocument
from app.services.auth_service import create_tokens

from tests.conftest import make_user


def _bearer(user) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_tokens(user)['access_token']}"}


@pytest.fixture
def alice(db):
    return make_user(db, username="alice", role="user")


@pytest.fixture
def bob(db):
    return make_user(db, username="bob", role="user")


@pytest.fixture
def alice_collection(db, alice) -> IngestionCollection:
    coll = IngestionCollection(
        name="alice-collection",
        chunking_config={"strategy": "semantic"},
        embedding_model="nv-embed",
        embedding_dim=4096,
        status="active",
        created_by=alice.id,
    )
    db.add(coll)
    db.commit()
    db.refresh(coll)

    doc = IngestionDocument(
        collection_id=coll.id,
        filename="paper.pdf",
        sha256="a" * 64,
        mime_type="application/pdf",
        status="indexed",
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    # Stash doc id on the collection so stubs can reference it.
    coll._test_doc_id = doc.id
    return coll


@pytest.fixture
def alice_archived_collection(db, alice) -> IngestionCollection:
    coll = IngestionCollection(
        name="alice-archived",
        chunking_config={"strategy": "semantic"},
        embedding_model="nv-embed",
        embedding_dim=4096,
        status="archived",
        created_by=alice.id,
    )
    db.add(coll)
    db.commit()
    db.refresh(coll)
    return coll


# ── Stubs for the pgvector layer ──────────────────────────────────────────


class _StubChunk:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


class _StubHit:
    """Mimics anila_core's SearchHit dataclass shape consumed by the route."""

    def __init__(self, *, chunk, score, parent_content=None):
        self.chunk = chunk
        self.score = score
        self.parent_content = parent_content


class _StubStore:
    """Replaces CollectionScopedPgVectorStore for the chunk path."""

    def __init__(self, hits):
        self._hits = hits

    async def similarity_search(self, *, query_embedding, top_k, min_score, **_kwargs):
        return self._hits


@pytest.fixture(autouse=True)
def _patch_retrieval(monkeypatch):
    async def fake_embed_query(db, user, model_name, dim, query):
        return [0.1] * dim

    monkeypatch.setattr(search_mod, "_embed_query", fake_embed_query)


# ── 200 happy path ────────────────────────────────────────────────────────


def test_chunk_search_returns_hits_with_full_shape(
    client: TestClient, db, alice, alice_collection, monkeypatch,
):
    """Owner gets 200 + every documented field in the response."""
    doc_id = alice_collection._test_doc_id
    hits = [
        _StubHit(
            chunk=_StubChunk(
                id=1001,
                document_id=doc_id,
                chunk_key=f"chunk:{doc_id}:001",
                content="leaf chunk content one",
                metadata={"page": 1, "section": "intro"},
                parent_chunk_id=2001,
                chunk_type="leaf",
                chunk_level=2,
            ),
            score=0.91,
            parent_content="parent of chunk one",
        ),
        _StubHit(
            chunk=_StubChunk(
                id=1002,
                document_id=doc_id,
                chunk_key=f"chunk:{doc_id}:002",
                content="leaf chunk content two",
                metadata={"page": 2},
                parent_chunk_id=None,
                chunk_type="leaf",
                chunk_level=0,
            ),
            score=0.83,
        ),
    ]
    # Patch the store ctor to return our stub regardless of args.
    monkeypatch.setattr(
        search_mod,
        "CollectionScopedPgVectorStore",
        lambda pool, collection_id: _StubStore(hits),
    )
    # get_pool is called but the stub Store ignores it; just stop the
    # RuntimeError path.
    monkeypatch.setattr(search_mod, "get_pool", lambda: object())

    resp = client.post(
        f"/api/ingestion/collections/{alice_collection.id}/search",
        json={"query": "what is in the intro", "top_k": 5, "min_score": 0.0},
        headers=_bearer(alice),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # Envelope
    assert body["query"] == "what is in the intro"
    assert body["embedding_model"] == "nv-embed"
    assert body["embedding_dim"] == 4096
    assert isinstance(body["results"], list)
    assert len(body["results"]) == 2

    first = body["results"][0]
    for key in (
        "chunk_id", "document_id", "filename", "chunk_key", "content",
        "score", "metadata", "parent_chunk_id", "parent_content",
        "chunk_type", "chunk_level",
    ):
        assert key in first, f"missing field {key} in result: {first}"
    assert first["chunk_id"] == 1001
    assert first["document_id"] == doc_id
    assert first["filename"] == "paper.pdf"
    assert first["chunk_key"] == f"chunk:{doc_id}:001"
    assert first["score"] == pytest.approx(0.91)
    assert first["metadata"] == {"page": 1, "section": "intro"}
    assert first["parent_chunk_id"] == 2001
    assert first["parent_content"] == "parent of chunk one"
    assert first["chunk_type"] == "leaf"
    assert first["chunk_level"] == 2

    # Second hit: parent_chunk_id None preserved
    second = body["results"][1]
    assert second["parent_chunk_id"] is None
    assert second["parent_content"] is None


def test_chunk_search_empty_results(
    client: TestClient, db, alice, alice_collection, monkeypatch,
):
    monkeypatch.setattr(
        search_mod, "CollectionScopedPgVectorStore",
        lambda pool, collection_id: _StubStore([]),
    )
    monkeypatch.setattr(search_mod, "get_pool", lambda: object())

    resp = client.post(
        f"/api/ingestion/collections/{alice_collection.id}/search",
        json={"query": "anything"},
        headers=_bearer(alice),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["results"] == []
    assert body["embedding_model"] == "nv-embed"
    assert body["embedding_dim"] == 4096


# ── document_ids filter ───────────────────────────────────────────────────


def test_chunk_search_document_ids_filter_drops_others(
    client: TestClient, db, alice, alice_collection, monkeypatch,
):
    """``document_ids`` post-filters the hits in-app."""
    hits = [
        _StubHit(
            chunk=_StubChunk(
                id=1001, document_id=5001, chunk_key="a",
                content="x", metadata={}, parent_chunk_id=None,
                chunk_type="leaf", chunk_level=0,
            ),
            score=0.9,
        ),
        _StubHit(
            chunk=_StubChunk(
                id=1002, document_id=9999, chunk_key="b",
                content="y", metadata={}, parent_chunk_id=None,
                chunk_type="leaf", chunk_level=0,
            ),
            score=0.8,
        ),
    ]
    monkeypatch.setattr(
        search_mod, "CollectionScopedPgVectorStore",
        lambda pool, collection_id: _StubStore(hits),
    )
    monkeypatch.setattr(search_mod, "get_pool", lambda: object())

    resp = client.post(
        f"/api/ingestion/collections/{alice_collection.id}/search",
        json={"query": "q", "document_ids": [5001]},
        headers=_bearer(alice),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["results"]) == 1
    assert body["results"][0]["document_id"] == 5001


# ── auth / status ────────────────────────────────────────────────────────


def test_chunk_search_404_when_collection_missing(client: TestClient, db, alice):
    resp = client.post(
        "/api/ingestion/collections/999999/search",
        json={"query": "anything"},
        headers=_bearer(alice),
    )
    assert resp.status_code == 404


def test_chunk_search_403_when_caller_not_owner(
    client: TestClient, db, alice, bob, alice_collection,
):
    resp = client.post(
        f"/api/ingestion/collections/{alice_collection.id}/search",
        json={"query": "anything"},
        headers=_bearer(bob),
    )
    assert resp.status_code == 403


def test_chunk_search_409_when_collection_not_active(
    client: TestClient, db, alice, alice_archived_collection,
):
    resp = client.post(
        f"/api/ingestion/collections/{alice_archived_collection.id}/search",
        json={"query": "anything"},
        headers=_bearer(alice),
    )
    assert resp.status_code == 409
    assert "archived" in resp.json()["detail"].lower()


def test_chunk_search_validation_rejects_empty_query(
    client: TestClient, db, alice, alice_collection,
):
    resp = client.post(
        f"/api/ingestion/collections/{alice_collection.id}/search",
        json={"query": ""},
        headers=_bearer(alice),
    )
    assert resp.status_code == 422


def test_chunk_search_validation_rejects_top_k_over_50(
    client: TestClient, db, alice, alice_collection,
):
    resp = client.post(
        f"/api/ingestion/collections/{alice_collection.id}/search",
        json={"query": "q", "top_k": 51},
        headers=_bearer(alice),
    )
    assert resp.status_code == 422
