"""Contract tests for ``POST /api/ingestion/collections/{id}/images/search``.

The image-search endpoint is the public HTTP surface that ``anila-studio``
(extracted-out subservice) will call to obtain image hits without touching
the csp-db pgvector layer directly. The studio service must be able to
fetch the same shape of result as the in-process ``_retrieve_images`` helper
in ``app/api/studio.py``, so the contract test pins:

- response payload shape (results[], scores, image_id/document_id/page/...)
- auth (admin OR collection owner; 403 cross-user; 404 if collection missing)
- behaviour when collection has zero matches (200 + results=[])

The retrieval path goes asyncpg → pgvector which is not available under the
SQLite test fixture, so ``_embed_query`` and the pool fetch are monkeypatched
to return deterministic fixture data.
"""
from __future__ import annotations

import os

# Same shape as other client-based tests — the global conftest only sets the
# SQLite DB; the lifespan-startup security gate needs an explicit opt-in.
os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import pytest
from fastapi.testclient import TestClient

import app.api.ingestion.search as search_mod
from app.models.ingestion import IngestionCollection, IngestionDocument
from app.services.auth_service import create_tokens

from tests.conftest import make_user


# ── Fixtures ──────────────────────────────────────────────────────────────


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
    """Active collection owned by alice with one document."""
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
    return coll


class _StubPool:
    """Replaces ``get_pool()`` return; the endpoint only touches
    ``acquire()`` → context manager → ``conn.fetch(sql, *args)``.
    """

    def __init__(self, rows: list[dict], coverage_rows: list[dict] | None = None):
        self._rows = rows
        self._coverage_rows = coverage_rows if coverage_rows is not None else []
        self.last_sql: str | None = None
        self.last_args: tuple | None = None

    def acquire(self):
        rows = self._rows
        outer = self

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

            async def fetch(self, sql, *args):
                if "has_other" in sql or "bool_or" in sql:
                    return outer._coverage_rows
                outer.last_sql = sql
                outer.last_args = args
                return rows

        class _Acq:
            async def __aenter__(self):
                return _Conn()

            async def __aexit__(self, *exc):
                return False

        return _Acq()


@pytest.fixture(autouse=True)
def _patch_retrieval(monkeypatch):
    """Stub the embedding + pgvector layer so the endpoint logic itself
    is what gets exercised, not the asyncpg / proxy machinery."""

    async def fake_embed_query(db, user, model_name, dim, query):
        # Return a vector of the right length; values don't matter — the
        # pool fetch is also stubbed.
        return [0.1] * dim

    monkeypatch.setattr(search_mod, "_embed_query", fake_embed_query)


# ── 200 happy path ────────────────────────────────────────────────────────


def test_image_search_returns_hits(client: TestClient, db, alice, alice_collection,
                                   monkeypatch):
    """Owner gets 200 + the expected response shape (results[] with full
    metadata + score in [0,1]; embedding_model / embedding_dim echoed)."""
    fixture_rows = [
        {
            "pk_id": 101,
            "image_id": "img-abc",
            "document_id": 5001,
            "page": 3,
            "storage_path": "alice/uploads/img-abc.png",
            "mime": "image/png",
            "caption": "Figure 3 architecture diagram",
            "filename": "paper.pdf",
            "dist": 0.18,  # cosine distance → similarity ≈ 0.82
        },
        {
            "pk_id": 102,
            "image_id": "img-def",
            "document_id": 5001,
            "page": 5,
            "storage_path": "alice/uploads/img-def.jpg",
            "mime": "image/jpeg",
            "caption": None,
            "filename": "paper.pdf",
            "dist": 0.30,
        },
    ]
    monkeypatch.setattr(search_mod, "get_pool", lambda: _StubPool(fixture_rows))

    resp = client.post(
        f"/api/ingestion/collections/{alice_collection.id}/images/search",
        json={"query": "show me the architecture diagram", "top_k": 8, "min_score": 0.0},
        headers=_bearer(alice),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["query"] == "show me the architecture diagram"
    assert body["embedding_model"] == "nv-embed"
    assert body["embedding_dim"] == 4096
    assert isinstance(body["results"], list)
    assert len(body["results"]) == 2

    first = body["results"][0]
    # Required fields
    for key in ("image_id", "document_id", "page", "storage_path",
                "mime", "caption", "filename", "score"):
        assert key in first, f"missing field {key} in result: {first}"
    assert first["image_id"] == 101
    assert first["document_id"] == 5001
    assert first["page"] == 3
    assert first["mime"] == "image/png"
    assert first["filename"] == "paper.pdf"
    # 1 - distance(0.18) = 0.82 (allow float tolerance)
    assert abs(first["score"] - 0.82) < 1e-6
    # caption=None is preserved as null
    assert body["results"][1]["caption"] is None


def test_image_search_empty_collection_returns_empty_results(
    client: TestClient, db, alice, alice_collection, monkeypatch,
):
    """A collection with zero matching images returns 200 + results=[]."""
    monkeypatch.setattr(search_mod, "get_pool", lambda: _StubPool([]))

    resp = client.post(
        f"/api/ingestion/collections/{alice_collection.id}/images/search",
        json={"query": "anything"},
        headers=_bearer(alice),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["results"] == []
    assert body["embedding_model"] == "nv-embed"
    assert body["embedding_dim"] == 4096
    assert body["source_model_mismatch"] is False


# ── 404: collection does not exist ────────────────────────────────────────


def test_image_search_404_when_collection_missing(client: TestClient, db, alice):
    resp = client.post(
        "/api/ingestion/collections/999999/images/search",
        json={"query": "anything"},
        headers=_bearer(alice),
    )
    assert resp.status_code == 404, resp.text


# ── 403: caller not the collection owner ──────────────────────────────────


def test_image_search_403_when_not_owner(
    client: TestClient, db, bob, alice_collection,
):
    """bob tries to search alice's collection — 403."""
    resp = client.post(
        f"/api/ingestion/collections/{alice_collection.id}/images/search",
        json={"query": "anything"},
        headers=_bearer(bob),
    )
    assert resp.status_code == 403, resp.text


# ── Default top_k / min_score per spec ────────────────────────────────────


def test_image_search_default_top_k_is_8(
    client: TestClient, db, alice, alice_collection, monkeypatch,
):
    """ImageSearchRequest default top_k = 8 per the studio-extraction
    contract. The endpoint must pass that to the underlying SQL."""
    captured: dict = {}

    def fake_pool():
        pool = _StubPool([])
        captured["pool"] = pool
        return pool

    monkeypatch.setattr(search_mod, "get_pool", fake_pool)

    resp = client.post(
        f"/api/ingestion/collections/{alice_collection.id}/images/search",
        json={"query": "test"},  # no top_k specified
        headers=_bearer(alice),
    )
    assert resp.status_code == 200, resp.text
    # The LIMIT $4 arg in _retrieve_images is the 4th positional arg.
    # args order: (collection_id, q_value, max_dist, top_k)
    assert captured["pool"].last_args is not None
    assert captured["pool"].last_args[3] == 8


def test_image_search_source_model_mismatch_is_visible(
    client: TestClient, db, alice, alice_collection, monkeypatch,
):
    """Stranded images stay 200 + empty, but the flag is on."""
    from types import SimpleNamespace

    monkeypatch.setattr(
        "app.services.platform_embedding.resolve_platform_embedding",
        lambda db: SimpleNamespace(name="nv-embed-v2"),
    )
    pool = _StubPool(
        [],
        coverage_rows=[
            {
                "has_matching": False,
                "has_other": True,
                "sample_other": "old-embed",
            }
        ],
    )
    monkeypatch.setattr(search_mod, "get_pool", lambda: pool)

    resp = client.post(
        f"/api/ingestion/collections/{alice_collection.id}/images/search",
        json={"query": "anything"},
        headers=_bearer(alice),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["results"] == []
    assert body["source_model_mismatch"] is True
    assert body["embedding_model"] == "nv-embed-v2"
