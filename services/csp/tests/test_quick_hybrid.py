"""DS-05: real lexical + vector fusion, model-free modes and caller isolation."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import app.api.ingestion.search as api
from app.models.ingestion import IngestionCollection, IngestionDocument
from app.services.chunk_retrieval import collect_chunk_hits
from anila_core.embeddings.keyword import cjk_search_units
from anila_core.storage.adapters.pgvector_store import CollectionScopedPgVectorStore
from tests.conftest import make_user


def run(awaitable):
    return asyncio.run(awaitable)


def hit(ident, *, doc=1, content="採購規定第十二條", score=0.8, **metadata):
    return SimpleNamespace(
        chunk=SimpleNamespace(
            id=ident, document_id=doc, collection_id=1, chunk_key=f"chunk:{ident}",
            content=content, metadata=metadata,
        ),
        score=score, parent_content="採購規定",
    )


class HybridStore:
    """All three documents have vectors; lexical target missed vector top-k."""
    def __init__(self, hits):
        self.hits = hits
        self.calls = []

    async def embedding_coverage(self, model_id):
        return "ready"

    async def similarity_search_for_model(self, **kwargs):
        self.calls.append("vector")
        return [self.hits[0], self.hits[2]]

    async def keyword_search(self, query, top_k):
        self.calls.append("fulltext-index")
        return [h for h in self.hits if query in h.chunk.content][:top_k]

    async def keyword_fallback_search(self, query, top_k):
        self.calls.append("cjk")
        units = cjk_search_units(query)
        return [h for h in self.hits if any(u in h.chunk.content for u in units)][:top_k]

    async def keyword_search_unembedded(self, *args, **kwargs):
        raise AssertionError("hybrid must search the complete lexical corpus")


def collect(store, **kwargs):
    return run(collect_chunk_hits(
        store, query=kwargs.pop("query", "第十二條"), query_vec=[0.1, 0.2],
        embed_error=kwargs.pop("embed_error", None), model_id=7,
        top_k=kwargs.pop("top_k", 3), min_score=0.99, source_model="embed",
        **kwargs,
    ))


def test_hybrid_recalls_embedded_keyword_target_and_deduplicates():
    hits = [hit(1, content="語意相關的其他條文"), hit(2, page=9, slide=3, line_start=15,
            line_end=18), hit(3, content="第十二條的說明")]
    batch = collect(HybridStore(hits), search_mode="hybrid")
    ids = [h.chunk.id for h in batch.hits]
    assert set(ids) == {1, 2, 3}
    assert len(ids) == len(set(ids))
    assert ids[0] == 3  # present in both lexical and semantic channels
    target = next(h for h in batch.hits if h.chunk.id == 2)
    assert target.chunk.metadata == {"page": 9, "slide": 3, "line_start": 15, "line_end": 18}
    assert target.parent_content == "採購規定"
    assert hits[2].score == 0.8  # fusion must not mutate store hits
    assert all(0 < h.score <= 1 for h in batch.hits)
    assert batch.keyword_fallback is False


def test_default_semantic_keeps_original_vector_scores():
    class Store:
        async def similarity_search(self, **kwargs):
            return [hit(1, score=0.73)]
        async def keyword_search(self, *args, **kwargs):
            raise AssertionError("default must preserve semantic behavior")
    batch = collect(Store())
    assert batch.hits[0].score == 0.73


def test_hybrid_embedding_error_uses_keyword_and_marks_fallback():
    store = HybridStore([hit(1), hit(2), hit(3)])
    batch = collect(store, embed_error=RuntimeError("embedding offline"), search_mode="hybrid")
    assert batch.keyword_fallback is True
    assert batch.hits
    assert "vector" not in store.calls


@pytest.mark.parametrize("channel", ["vector", "keyword"])
def test_hybrid_does_not_mask_storage_failure(channel):
    class Store(HybridStore):
        async def similarity_search_for_model(self, **kwargs):
            if channel == "vector":
                raise RuntimeError("storage failed")
            return []
        async def keyword_search(self, *args, **kwargs):
            if channel == "keyword":
                raise RuntimeError("storage failed")
            return []
    with pytest.raises(RuntimeError, match="storage failed"):
        collect(Store([]), search_mode="hybrid")


def test_empty_hybrid_returns_empty_without_outage_notice():
    class Store(HybridStore):
        async def embedding_coverage(self, model_id):
            return "empty"
    batch = collect(Store([]), search_mode="hybrid")
    assert batch.hits == []
    assert batch.keyword_fallback is False


@pytest.fixture
def corpus(db):
    user = make_user(db, username="quick-reader", role="user")
    other = make_user(db, username="quick-outsider", role="user")
    coll = IngestionCollection(name="規章", chunking_config={}, embedding_model=None,
                               embedding_dim=2, status="active", created_by=user.id)
    db.add(coll)
    db.commit()
    doc = IngestionDocument(collection_id=coll.id, filename="採購辦法.pdf", sha256="b" * 64,
                            mime_type="application/pdf", status="indexed")
    db.add(doc)
    db.commit()
    return user, other, coll, doc


@pytest.mark.parametrize("mode", ["keyword", "fulltext"])
def test_model_free_modes_keep_filename_and_citations(db, corpus, monkeypatch, mode):
    user, _, coll, doc = corpus
    items = [hit(i, doc=doc.id, content=f"第十二條第{i}項", page=i, slide=i,
                 line_start=i * 10, line_end=i * 10 + 3) for i in range(1, 4)]

    def no_model(*args, **kwargs):
        raise AssertionError("model-free retrieval must not resolve or call models")

    class Store(HybridStore):
        async def bounded_fulltext_search(self, *, document_ids):
            assert document_ids == [doc.id]
            return items

    import app.services.embedding_swap as swap
    import app.services.embedding_keyword as rerank
    monkeypatch.setattr(swap, "search_model", no_model)
    monkeypatch.setattr(api, "_require_platform_embedding", no_model)
    monkeypatch.setattr(api, "_embed_query", no_model)
    monkeypatch.setattr(rerank, "maybe_rerank", no_model)
    monkeypatch.setattr(api, "get_pool", lambda: object())
    monkeypatch.setattr(api, "CollectionScopedPgVectorStore", lambda *a, **k: Store(items))
    response = run(api.search_collection(coll.id, api.SearchRequest(
        query="第十二條", search_mode=mode, top_k=1, document_ids=[doc.id],
    ), db, api.SearchPrincipal(user=user)))
    assert len(response.results) == (3 if mode == "fulltext" else 1)
    assert response.keyword_fallback is False
    assert response.results[0].filename == "採購辦法.pdf"
    assert response.results[0].metadata == {"page": 1, "slide": 1, "line_start": 10, "line_end": 13}


@pytest.mark.parametrize("mode", ["semantic", "hybrid", "keyword", "fulltext"])
def test_unauthorized_real_caller_rejected_before_store_or_model(db, corpus, monkeypatch, mode):
    _, other, coll, _ = corpus
    def forbidden(*args, **kwargs):
        raise AssertionError("unauthorized search reached retrieval/model")
    monkeypatch.setattr(api, "get_pool", forbidden)
    monkeypatch.setattr(api, "_embed_query", forbidden)
    monkeypatch.setattr(api, "expand_query", forbidden)
    with pytest.raises(HTTPException) as exc:
        run(api.search_collection(coll.id, api.SearchRequest(query="第十二條", search_mode=mode),
                                  db, api.SearchPrincipal(user=other)))
    assert exc.value.status_code == 403


def test_fulltext_limit_becomes_explicit_413(db, corpus, monkeypatch):
    user, _, coll, _ = corpus
    class Store:
        async def bounded_fulltext_search(self, **kwargs):
            raise ValueError("全文模式上限為 20 份文件、100000 字及 500 個片段")
    monkeypatch.setattr(api, "get_pool", lambda: object())
    monkeypatch.setattr(api, "CollectionScopedPgVectorStore", lambda *a, **k: Store())
    with pytest.raises(HTTPException) as exc:
        run(api.search_collection(coll.id, api.SearchRequest(query="全文", search_mode="fulltext"),
                                  db, api.SearchPrincipal(user=user)))
    assert exc.value.status_code == 413
    assert "100000" in exc.value.detail


@pytest.mark.parametrize("size", ["ok", "empty", "documents", "characters", "chunks"])
def test_fulltext_store_checks_bounds_and_scope(size):
    row = dict(id=5, collection_id=17, document_id=8, chunk_key="key", content="第十二條",
               metadata={"page": 2}, token_count=4, created_at=datetime.now(timezone.utc),
               parent_chunk_id=None, chunk_type="leaf", chunk_level=0, score=1.0,
               document_total=1, character_total=5, chunk_total=1)
    if size == "empty":
        row.update(id=None, document_total=0, character_total=0, chunk_total=0)
    for kind, column, value in [("documents", "document_total", 21),
                                ("characters", "character_total", 100001),
                                ("chunks", "chunk_total", 501)]:
        if size == kind:
            row.update(id=None)
            row[column] = value

    class Conn:
        async def fetch(self, sql, *args):
            assert args == (17, [8])
            assert "collection_id = $1" in sql
            assert "document_id = ANY($2::bigint[])" in sql
            return [row]
    @asynccontextmanager
    async def scoped():
        yield Conn()
    store = CollectionScopedPgVectorStore(None, collection_id=17)
    store._acquire = scoped
    if size in {"documents", "characters", "chunks"}:
        with pytest.raises(ValueError, match="全文模式上限"):
            run(store.bounded_fulltext_search(document_ids=[8]))
    else:
        result = run(store.bounded_fulltext_search(document_ids=[8]))
        assert len(result) == (0 if size == "empty" else 1)
        if result:
            assert result[0].chunk.metadata == {"page": 2}


def test_request_rejects_unknown_mode_and_keeps_semantic_default():
    assert api.SearchRequest(query="q").search_mode == "semantic"
    with pytest.raises(ValidationError):
        api.SearchRequest(query="q", search_mode="anything")


@pytest.mark.parametrize("query,literal", [("採購的第2條是什麼", "第2條"),
                                           ("查AB-001", "AB-001"), ("2", "2")])
def test_keyword_store_preserves_article_numbers_and_literal_identifiers(query, literal):
    from anila_core.embeddings.keyword import ilike_pattern
    class Conn:
        async def fetch(self, sql, *args):
            assert ilike_pattern(literal) in args[:-1]
            assert args[-1] == 5
            assert "ILIKE" in sql
            return []
    @asynccontextmanager
    async def scoped():
        yield Conn()
    store = CollectionScopedPgVectorStore(None, collection_id=17)
    store._acquire = scoped
    assert run(store.keyword_fallback_search(query, top_k=5)) == []


@pytest.mark.parametrize("configured", [True, False])
def test_hybrid_endpoint_uses_real_caller_and_survives_missing_embedding_role(
    db, corpus, monkeypatch, configured,
):
    user, _, coll, doc = corpus
    items = [hit(i, doc=doc.id, page=i) for i in range(1, 4)]
    embedded_users = []
    async def embed(db_, caller, model_name, dim, query):
        embedded_users.append(caller.id)
        return [0.1] * dim
    import app.services.embedding_swap as swap
    monkeypatch.setattr(swap, "search_model", lambda db_: (
        SimpleNamespace(id=7, name="embed") if configured else None
    ))
    monkeypatch.setattr(api, "_embed_query", embed)
    monkeypatch.setattr(api, "get_pool", lambda: object())
    monkeypatch.setattr(api, "CollectionScopedPgVectorStore", lambda *a, **k: HybridStore(items))
    result = run(api.search_collection(
        coll.id, api.SearchRequest(query="第十二條", search_mode="hybrid"),
        db, api.SearchPrincipal(user=user),
    ))
    assert embedded_users == ([user.id] if configured else [])
    assert result.results
    assert all(item.filename == "採購辦法.pdf" for item in result.results)
    assert result.keyword_fallback is (not configured)
    assert bool(result.keyword_fallback_notice) is (not configured)


def test_hybrid_can_fall_back_using_fulltext_index_only():
    class Store:
        async def keyword_search(self, query, top_k):
            return [hit(1)]
    batch = collect(Store(), search_mode="hybrid", embed_error=RuntimeError("offline"))
    assert batch.keyword_fallback is True
    assert [h.chunk.id for h in batch.hits] == [1]
