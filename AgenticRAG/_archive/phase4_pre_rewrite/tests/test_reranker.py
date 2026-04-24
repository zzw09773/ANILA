"""Tests for the cross-encoder reranker provider.

Covers:
- Dataclass shapes
- ``build_reranker_from_env`` flag handling and backend dispatch
- ``JinaRerankerProvider.rerank`` against a mocked HTTP transport
- The tools-level ``_arerank_candidates`` helper, which must be tolerant of
  reranker failures so retrieval never silently breaks.
"""
from __future__ import annotations

import json
import os
from typing import Iterable

import httpx
import pytest

from agentic_rag.providers.reranker import (
    JinaRerankerProvider,
    RerankCandidate,
    RerankedResult,
    build_reranker_from_env,
)
from agentic_rag.tools import _arerank_candidates


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """Strip reranker env vars so each test sees a clean slate."""
    for k in (
        "RAG_RERANKER_ENABLED",
        "RAG_RERANKER_BACKEND",
        "RAG_RERANKER_MODEL",
        "JINA_API_KEY",
        "JINA_BASE_URL",
        "JINA_VERIFY_SSL",
    ):
        monkeypatch.delenv(k, raising=False)


# ---------------------------------------------------------------------------
# build_reranker_from_env
# ---------------------------------------------------------------------------

def test_disabled_by_default():
    assert build_reranker_from_env() is None


def test_enabled_jina_without_api_key_returns_none(monkeypatch):
    monkeypatch.setenv("RAG_RERANKER_ENABLED", "true")
    monkeypatch.setenv("RAG_RERANKER_BACKEND", "jina")
    assert build_reranker_from_env() is None


def test_enabled_jina_with_api_key_constructs_provider(monkeypatch):
    monkeypatch.setenv("RAG_RERANKER_ENABLED", "true")
    monkeypatch.setenv("RAG_RERANKER_BACKEND", "jina")
    monkeypatch.setenv("JINA_API_KEY", "sk-test")
    r = build_reranker_from_env()
    assert isinstance(r, JinaRerankerProvider)


def test_unknown_backend_returns_none(monkeypatch):
    monkeypatch.setenv("RAG_RERANKER_ENABLED", "true")
    monkeypatch.setenv("RAG_RERANKER_BACKEND", "magic")
    assert build_reranker_from_env() is None


# ---------------------------------------------------------------------------
# JinaRerankerProvider against a mocked HTTP transport
# ---------------------------------------------------------------------------

class _MockTransport(httpx.AsyncBaseTransport):
    """Captures the request and returns a canned Jina-shaped response."""

    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.last_request: httpx.Request | None = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.last_request = request
        return httpx.Response(200, json=self._payload)


@pytest.mark.asyncio
async def test_jina_rerank_orders_by_response_index(monkeypatch):
    transport = _MockTransport({
        "results": [
            {"index": 2, "relevance_score": 0.95},
            {"index": 0, "relevance_score": 0.40},
            {"index": 1, "relevance_score": 0.10},
        ]
    })

    # Patch httpx.AsyncClient so the provider uses our transport.
    real_client = httpx.AsyncClient

    def _client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr("agentic_rag.providers.reranker.httpx.AsyncClient", _client_factory)

    provider = JinaRerankerProvider(api_key="sk-test")
    candidates = [
        RerankCandidate(chunk_id=f"c{i}", content=f"doc {i}", metadata={})
        for i in range(3)
    ]
    out = await provider.rerank("申誡的條件", candidates, top_k=3)

    assert [r.candidate.chunk_id for r in out] == ["c2", "c0", "c1"]
    assert [r.score for r in out] == [0.95, 0.40, 0.10]
    assert [r.rank for r in out] == [0, 1, 2]

    assert transport.last_request is not None
    body = json.loads(transport.last_request.content)
    assert body["query"] == "申誡的條件"
    assert body["documents"] == ["doc 0", "doc 1", "doc 2"]
    assert body["top_n"] == 3


@pytest.mark.asyncio
async def test_jina_rerank_empty_input_short_circuits():
    provider = JinaRerankerProvider(api_key="sk-test")
    assert await provider.rerank("q", [], top_k=5) == []
    cands = [RerankCandidate(chunk_id="x", content="x", metadata={})]
    assert await provider.rerank("q", cands, top_k=0) == []


# ---------------------------------------------------------------------------
# tools._arerank_candidates — failure tolerance
# ---------------------------------------------------------------------------

class _FakeReranker:
    def __init__(self, scores: dict[str, float] | None = None, raise_exc: bool = False) -> None:
        self._scores = scores or {}
        self._raise = raise_exc

    async def rerank(self, query, candidates, top_k):  # type: ignore[no-untyped-def]
        if self._raise:
            raise RuntimeError("boom")
        ranked = sorted(
            candidates,
            key=lambda c: self._scores.get(c.chunk_id, 0.0),
            reverse=True,
        )[:top_k]
        return [
            RerankedResult(candidate=c, score=self._scores.get(c.chunk_id, 0.0), rank=i)
            for i, c in enumerate(ranked)
        ]


@pytest.mark.asyncio
async def test_arerank_reorders_pool():
    pool = [
        {"chunk_id": "a", "content": "alpha"},
        {"chunk_id": "b", "content": "beta"},
        {"chunk_id": "c", "content": "gamma"},
    ]
    reranker = _FakeReranker(scores={"a": 0.1, "b": 0.9, "c": 0.5})
    out = await _arerank_candidates(reranker, "q", pool, top_k=2)
    assert [item["chunk_id"] for item in out] == ["b", "c"]
    assert all("rerank_score" in item for item in out)


@pytest.mark.asyncio
async def test_arerank_falls_back_on_exception():
    pool = [{"chunk_id": str(i), "content": f"d{i}"} for i in range(3)]
    out = await _arerank_candidates(_FakeReranker(raise_exc=True), "q", pool, top_k=2)
    assert [item["chunk_id"] for item in out] == ["0", "1"]


@pytest.mark.asyncio
async def test_arerank_empty_pool_returns_empty():
    assert await _arerank_candidates(_FakeReranker(), "q", [], top_k=5) == []
