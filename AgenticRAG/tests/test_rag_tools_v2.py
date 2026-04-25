"""Tests for the Sprint 3 / Chunk M re-implemented tool factories.

Run pure-Python: a fake ``AgentScopedPgVectorStore`` (just a small
class with the methods the tools call) replaces the real DB-backed
one. The cross-encoder rerank path is exercised via a fake reranker.

These factories are the LLM-callable surface: their ``input_schema``
must validate against the OpenAI tool schema, and ``implementation``
must return JSON-serialisable dicts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from agentic_rag.models.tool import ToolSafety
from agentic_rag.tools import (
    create_keyword_search_tool,
    create_read_document_tool,
    create_vector_search_tool,
)


@dataclass
class _FakeChunk:
    id: int
    document_id: int
    chunk_key: str
    content: str
    metadata: dict
    agent_id: int = 1
    collection_id: int = 1
    token_count: int | None = 10
    created_at: Any = None


@dataclass
class _FakeHit:
    chunk: _FakeChunk
    score: float


class _FakeStore:
    """Minimal AgentScopedPgVectorStore stand-in for tool tests."""

    def __init__(self, hits: list[_FakeHit] | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._hits = hits or []
        self._chunks: dict[int, list[_FakeChunk]] = {}

    def add_doc(self, doc_id: int, chunks: list[_FakeChunk]) -> None:
        self._chunks[doc_id] = chunks

    async def similarity_search(self, embedding, **kw):  # noqa: ANN001
        self.calls.append(("similarity_search", {"embedding": embedding, **kw}))
        return list(self._hits)

    async def keyword_search(self, query, **kw):  # noqa: ANN001
        self.calls.append(("keyword_search", {"query": query, **kw}))
        return list(self._hits)

    async def list_by_document(self, document_id, **kw):  # noqa: ANN001
        self.calls.append(("list_by_document", {"document_id": document_id, **kw}))
        return list(self._chunks.get(document_id, []))


# ── vector_search ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_vector_search_routes_query_through_embedder_and_store() -> None:
    """Tool calls embedder → store.similarity_search → returns mapped rows."""
    embedder_called: list[str] = []

    async def fake_embedder(text: str) -> list[float]:
        embedder_called.append(text)
        return [0.1] * 4

    store = _FakeStore(
        hits=[
            _FakeHit(_FakeChunk(1, 100, "k1", "alpha", {"h": "x"}), 0.85),
            _FakeHit(_FakeChunk(2, 100, "k2", "beta", {"h": "y"}), 0.62),
        ]
    )
    tool = create_vector_search_tool(store, fake_embedder, default_top_k=2)
    out = await tool.implementation({"query": "what is alpha"})

    assert embedder_called == ["what is alpha"]
    assert len(out["results"]) == 2
    assert out["results"][0]["chunk_id"] == 1
    assert out["results"][0]["score"] == 0.85
    # Make sure the call passed the right top_k upstream.
    sim = next(c for c in store.calls if c[0] == "similarity_search")
    assert sim[1]["top_k"] == 2


@pytest.mark.asyncio
async def test_vector_search_rejects_empty_query() -> None:
    async def emb(_text: str) -> list[float]: return []
    tool = create_vector_search_tool(_FakeStore(), emb)
    out = await tool.implementation({"query": "  "})
    assert "error" in out


@pytest.mark.asyncio
async def test_vector_search_uses_pool_multiplier_when_reranker_present() -> None:
    """top_k stays the *output* size; pool_k = top_k * multiplier upstream."""
    async def emb(_text: str) -> list[float]: return [0.0] * 4

    class _PassThroughRanker:
        async def rerank(self, query, candidates, top_k):
            from agentic_rag.providers.reranker import RerankedResult
            # Re-emit candidates with descending fake scores. ``rank``
            # is the third required positional arg per the dataclass.
            return [
                RerankedResult(candidate=c, score=1.0 - 0.01 * i, rank=i + 1)
                for i, c in enumerate(candidates[:top_k])
            ]

    store = _FakeStore(
        hits=[
            _FakeHit(_FakeChunk(i, 100, f"k{i}", f"c{i}", {}), 1.0 - i * 0.01)
            for i in range(10)
        ]
    )
    tool = create_vector_search_tool(
        store, emb,
        default_top_k=3,
        reranker=_PassThroughRanker(),
        rerank_pool_multiplier=4,
    )
    out = await tool.implementation({"query": "x"})

    sim = next(c for c in store.calls if c[0] == "similarity_search")
    assert sim[1]["top_k"] == 3 * 4, "should fetch top_k * multiplier upstream"
    assert len(out["results"]) == 3, "but final result count == top_k"
    # Reranker added rerank_score to every survivor.
    assert all("rerank_score" in r for r in out["results"])


# ── keyword_search ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_keyword_search_passes_query_through() -> None:
    store = _FakeStore(
        hits=[_FakeHit(_FakeChunk(7, 100, "k7", "found", {}), 0.5)]
    )
    tool = create_keyword_search_tool(store, default_top_k=5)
    out = await tool.implementation({"query": "find me"})
    assert len(out["results"]) == 1
    assert out["results"][0]["chunk_id"] == 7
    kw = next(c for c in store.calls if c[0] == "keyword_search")
    assert kw[1]["query"] == "find me"


@pytest.mark.asyncio
async def test_keyword_search_uses_tokenizer_when_provided() -> None:
    """CJK callers pre-tokenise; tool forwards via ``tokenized_query``."""
    received: dict = {}

    def tok(s: str) -> str:
        return f"<tok>{s}</tok>"

    class _Capture(_FakeStore):
        async def keyword_search(self, query, **kw):
            received.update(kw)
            return []

    tool = create_keyword_search_tool(_Capture(), tokenizer=tok)
    await tool.implementation({"query": "中文"})
    assert received["tokenized_query"] == "<tok>中文</tok>"


# ── read_document ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_read_document_returns_chunks_in_order() -> None:
    store = _FakeStore()
    store.add_doc(
        100,
        [
            _FakeChunk(1, 100, "k1", "first", {}),
            _FakeChunk(2, 100, "k2", "second", {}),
            _FakeChunk(3, 100, "k3", "third", {}),
        ],
    )
    tool = create_read_document_tool(store, max_chunks=10)
    out = await tool.implementation({"document_id": 100})
    assert out["document_id"] == 100
    assert out["chunk_count"] == 3
    assert [c["chunk_key"] for c in out["chunks"]] == ["k1", "k2", "k3"]


@pytest.mark.asyncio
async def test_read_document_rejects_non_int_id() -> None:
    tool = create_read_document_tool(_FakeStore())
    out = await tool.implementation({"document_id": "not-a-number"})
    assert "error" in out


@pytest.mark.asyncio
async def test_read_document_caps_at_max_chunks() -> None:
    store = _FakeStore()
    store.add_doc(99, [_FakeChunk(i, 99, f"k{i}", "x", {}) for i in range(50)])
    tool = create_read_document_tool(store, max_chunks=20)
    await tool.implementation({"document_id": 99})
    call = next(c for c in store.calls if c[0] == "list_by_document")
    assert call[1]["limit"] == 20


# ── ToolDefinition shape ──────────────────────────────────────────────────


def test_factories_emit_read_only_tooldef() -> None:
    async def emb(_t: str) -> list[float]: return []
    for tool in (
        create_vector_search_tool(_FakeStore(), emb),
        create_keyword_search_tool(_FakeStore()),
        create_read_document_tool(_FakeStore()),
    ):
        assert tool.safety == ToolSafety.READ_ONLY
        assert tool.implementation is not None
        # OpenAI schema must be JSON-serialisable.
        oa = tool.to_openai_schema()
        assert oa["function"]["name"] == tool.name
