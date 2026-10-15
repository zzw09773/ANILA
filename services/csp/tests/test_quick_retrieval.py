"""Global RAG recall, caller ACL and atomic bounded small-corpus fulltext."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.ingestion.search import SearchHitOut
from app.models.ingestion import IngestionCollection
from app.services import quick_retrieval as rag
from tests.conftest import make_user


def run(coro):
    return asyncio.run(coro)


def hit(ident=1, *, doc=1, content="採購辦法第2條", score=0.5, **metadata):
    return SearchHitOut(chunk_id=ident, document_id=doc, filename=f"條文{doc}.pdf",
                        chunk_key=f"chunk:{ident}", content=content, score=score,
                        metadata=metadata)


@pytest.fixture
def scope(db):
    reader = make_user(db, username="global-rag-reader", role="user")
    outsider = make_user(db, username="global-rag-outsider", role="user")
    collections = [IngestionCollection(
        name=f"source-{i}", chunking_config={}, embedding_model=None,
        embedding_dim=2, status="active", created_by=reader.id,
    ) for i in range(2)]
    db.add_all(collections)
    db.commit()
    return reader, outsider, [coll.id for coll in collections]


def stub_search(monkeypatch, batches, reader=None):
    calls = []
    async def search(collection_id, payload, db, principal):
        if reader is not None:
            assert principal.user.id == reader.id
        calls.append((collection_id, payload.search_mode, payload.top_k))
        value = batches.get((collection_id, payload.search_mode), [])
        if isinstance(value, Exception):
            raise value
        return SimpleNamespace(results=value)
    monkeypatch.setattr(rag.search_api, "search_collection", search)
    return calls


@pytest.mark.parametrize("query,weak,exact", [
    ("採購的第2條是什麼", "採購辦法第3條申請", "採購辦法第2條核准"),
    ("查AB-001", "相似型號AB-002規格", "AB-001完整規格"),
    ("AB-001", "AB-0012相似型號", "型號 AB-001 完整規格"),
    ("第十二條", "第十三條", "第十二條規定"),
])
def test_later_collection_exact_match_wins_globally(db, scope, monkeypatch, query, weak, exact):
    reader, _, ids = scope
    calls = stub_search(monkeypatch, {
        (ids[0], "hybrid"): [hit(content=weak, score=0.99)],
        (ids[1], "hybrid"): [hit(content=exact, score=0.02, page=8, slide=3,
                                   line_start=20, line_end=24)],
    }, reader)
    result = run(rag.retrieve_collections(db, reader, ids, query, top_k=1))
    assert result[0]["collection_id"] == ids[1]
    assert result[0]["content"] == exact
    assert result[0]["filename"] == "條文1.pdf"
    assert result[0]["metadata"]["page"] == 8
    assert result[0]["line"] == 20
    assert [call[0] for call in calls] == ids
    assert all(call[1] == "hybrid" and call[2] > 1 for call in calls)


def test_merge_is_order_independent_and_retains_distinct_source_identity():
    rows = [{**hit().model_dump(), "collection_id": i} for i in (2, 1)]
    before = deepcopy(rows)
    result = rag.merge_ranked_hits("第2條", rows + [rows[0]], top_k=5)
    reverse = rag.merge_ranked_hits("第2條", list(reversed(rows)), top_k=5)
    assert result == reverse
    assert len(result) == 2
    assert rows == before


@pytest.mark.parametrize("mode", ["hybrid", "fulltext"])
def test_all_acl_checks_precede_any_search_or_embedding(db, scope, monkeypatch, mode):
    reader, outsider, ids = scope
    forbidden = IngestionCollection(name="private", chunking_config={}, embedding_dim=2,
                                     status="active", created_by=outsider.id)
    db.add(forbidden)
    db.commit()
    calls = stub_search(monkeypatch, {}, reader)
    with pytest.raises(HTTPException) as exc:
        run(rag.retrieve_collections(db, reader, [ids[0], forbidden.id], "查詢", mode=mode))
    assert exc.value.status_code == 403
    assert calls == []


def test_small_fulltext_preserves_every_chunk_regardless_of_top_k(db, scope, monkeypatch):
    reader, _, ids = scope
    calls = stub_search(monkeypatch, {
        (ids[0], "fulltext"): [hit(i, doc=1, content=f"全文片段{i}") for i in range(1, 4)],
        (ids[1], "fulltext"): [hit(4, doc=2, content="另一份完整內容", page=9)],
    }, reader)
    result = run(rag.retrieve_collections(db, reader, ids, "全文", top_k=1, mode="fulltext"))
    assert len(result) == 4
    assert all(item["metadata"]["retrieval_mode"] == "fulltext" for item in result)
    assert all(item["metadata"]["fulltext_fallback"] is False for item in result)
    assert {item["collection_id"] for item in result} == set(ids)
    assert all(mode == "fulltext" for _, mode, _ in calls)


@pytest.mark.parametrize("limit", ["documents", "characters", "chunks", "context", "single"])
def test_oversized_fulltext_restarts_all_collections_in_hybrid(db, scope, monkeypatch, limit):
    reader, _, ids = scope
    small = [hit(content="完整內容")]
    large = [hit(2, doc=2, content="第二集合內容")]
    expected = {
        "documents": "fulltext_document_limit", "characters": "fulltext_character_limit",
        "chunks": "fulltext_chunk_limit", "context": "fulltext_context_limit",
        "single": "fulltext_collection_limit",
    }[limit]
    if limit == "documents":
        large = [hit(i + 10, doc=i + 10) for i in range(20)]
    elif limit == "characters":
        monkeypatch.setattr(rag, "MAX_CHARACTERS", 10)
        large = [hit(2, doc=2, content="第二集合內容超過全部字數")]
    elif limit == "chunks":
        monkeypatch.setattr(rag, "MAX_CHUNKS", 2)
        large = [hit(2, doc=2), hit(3, doc=2)]
    elif limit == "context":
        large = [hit(2, doc=2, content="內文", large_metadata="字" * 50000)]
    elif limit == "single":
        large = HTTPException(413, detail="此集合過大")
    calls = stub_search(monkeypatch, {
        (ids[0], "fulltext"): small, (ids[1], "fulltext"): large,
        (ids[0], "hybrid"): [hit(content="第3條", score=0.99)],
        (ids[1], "hybrid"): [hit(2, doc=2, content="第2條", score=0.1)],
    }, reader)
    result = run(rag.retrieve_collections(db, reader, ids, "第2條", top_k=1, mode="fulltext"))
    assert result[0]["collection_id"] == ids[1]
    assert result[0]["metadata"]["requested_mode"] == "fulltext"
    assert result[0]["metadata"]["retrieval_mode"] == "hybrid"
    assert result[0]["metadata"]["fulltext_fallback"] is True
    assert result[0]["metadata"]["fallback_reason"] == expected
    assert [(coll, mode) for coll, mode, _ in calls][-2:] == [(i, "hybrid") for i in ids]
    assert rag._size(result) <= rag.MAX_CONTEXT_BYTES


def test_hybrid_context_budget_omits_whole_chunks_without_truncation(db, scope, monkeypatch):
    reader, _, ids = scope
    oversized = "第2條" + "大" * 50000
    calls = stub_search(monkeypatch, {
        (ids[0], "hybrid"): [hit(content=oversized)],
        (ids[1], "hybrid"): [hit(2, content="完整小片段", score=0.1)],
    }, reader)
    result = run(rag.retrieve_collections(db, reader, ids, "第2條", top_k=2))
    assert len(calls) == 2
    assert len(result) == 1
    assert result[0]["content"] == "完整小片段"
    assert result[0]["metadata"]["context_limited"] is True
    assert rag._size(result) <= rag.MAX_CONTEXT_BYTES


def test_candidate_budget_is_global_and_fails_explicitly(db, scope, monkeypatch):
    reader, _, ids = scope
    monkeypatch.setattr(rag, "MAX_CANDIDATE_BYTES", 1500)
    stub_search(monkeypatch, {(i, "hybrid"): [hit(content="字" * 200)] for i in ids}, reader)
    with pytest.raises(HTTPException) as exc:
        run(rag.retrieve_collections(db, reader, ids, "查詢"))
    assert exc.value.status_code == 413
    assert exc.value.detail["error_type"] == "rag_candidates_too_large"


def test_regular_empty_hybrid_is_empty_but_fulltext_fallback_is_explicit(db, scope, monkeypatch):
    reader, _, ids = scope
    stub_search(monkeypatch, {(ids[0], "fulltext"): HTTPException(413)}, reader)
    assert run(rag.retrieve_collections(db, reader, ids, "查詢")) == []
    with pytest.raises(HTTPException) as exc:
        run(rag.retrieve_collections(db, reader, ids, "查詢", mode="fulltext"))
    assert exc.value.detail["error_type"] == "rag_fulltext_fallback_empty"


@pytest.mark.parametrize("kwargs", [{"mode": "semantic"}, {"top_k": True}, {"top_k": 51}])
def test_invalid_configuration_never_searches(db, scope, monkeypatch, kwargs):
    reader, _, ids = scope
    calls = stub_search(monkeypatch, {}, reader)
    with pytest.raises(HTTPException) as exc:
        run(rag.retrieve_collections(db, reader, ids, "查詢", **kwargs))
    assert exc.value.status_code == 422
    assert calls == []
