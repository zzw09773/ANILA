"""Tests for the cross-encoder reranker provider.

Covers:
- ``build_reranker_from_env`` flag handling and config validation
- ``VllmScoreRerankerProvider.rerank`` against a mocked HTTP transport
  (request shape, response parsing, sorting, top_k truncation,
  malformed-response tolerance)
- The tools-level ``_arerank_candidates`` helper, which must be tolerant
  of reranker failures so retrieval never silently breaks.
"""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from agentic_rag.providers.reranker import (
    RerankCandidate,
    RerankedResult,
    VllmScoreRerankerProvider,
    build_reranker_from_env,
)
from agentic_rag.tools import _arerank_candidates


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """Strip reranker env vars so each test sees a clean slate."""
    for k in (
        "RAG_RERANKER_ENABLED",
        "RAG_RERANKER_URL",
        "RAG_RERANKER_MODEL",
        "RAG_RERANKER_API_KEY",
        "RAG_RERANKER_VERIFY_SSL",
    ):
        monkeypatch.delenv(k, raising=False)


# ---------------------------------------------------------------------------
# build_reranker_from_env
# ---------------------------------------------------------------------------

def test_disabled_by_default():
    assert build_reranker_from_env() is None


def test_enabled_without_url_returns_none(monkeypatch):
    monkeypatch.setenv("RAG_RERANKER_ENABLED", "true")
    monkeypatch.setenv("RAG_RERANKER_MODEL", "mxbai-rerank-large-v1")
    assert build_reranker_from_env() is None


def test_enabled_without_model_returns_none(monkeypatch):
    monkeypatch.setenv("RAG_RERANKER_ENABLED", "true")
    monkeypatch.setenv("RAG_RERANKER_URL", "http://example/v1")
    assert build_reranker_from_env() is None


def test_enabled_constructs_vllm_provider(monkeypatch):
    monkeypatch.setenv("RAG_RERANKER_ENABLED", "true")
    monkeypatch.setenv("RAG_RERANKER_URL", "http://172.16.120.35:8001/v1")
    monkeypatch.setenv("RAG_RERANKER_MODEL", "mxbai-rerank-large-v1")
    monkeypatch.setenv("RAG_RERANKER_API_KEY", "sk-test")
    monkeypatch.setenv("RAG_RERANKER_VERIFY_SSL", "false")

    r = build_reranker_from_env()
    assert isinstance(r, VllmScoreRerankerProvider)
    assert r._base_url == "http://172.16.120.35:8001/v1"
    assert r._model == "mxbai-rerank-large-v1"
    assert r._api_key == "sk-test"
    assert r._verify_ssl is False


def test_constructor_rejects_empty_url():
    with pytest.raises(ValueError, match="base_url"):
        VllmScoreRerankerProvider(base_url="", model="m")


def test_constructor_rejects_empty_model():
    with pytest.raises(ValueError, match="model"):
        VllmScoreRerankerProvider(base_url="http://x", model="")


# ---------------------------------------------------------------------------
# VllmScoreRerankerProvider against a mocked HTTP transport
# ---------------------------------------------------------------------------

class _MockTransport(httpx.AsyncBaseTransport):
    """Captures the request and returns a canned vLLM /v1/score response."""

    def __init__(self, payload: dict, status: int = 200) -> None:
        self._payload = payload
        self._status = status
        self.last_request: httpx.Request | None = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.last_request = request
        return httpx.Response(self._status, json=self._payload)


def _patch_async_client(monkeypatch, transport: _MockTransport) -> None:
    real_client = httpx.AsyncClient

    def _factory(*args: Any, **kwargs: Any):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(
        "agentic_rag.providers.reranker.httpx.AsyncClient", _factory
    )


@pytest.mark.asyncio
async def test_rerank_request_body_shape(monkeypatch):
    transport = _MockTransport({
        "data": [
            {"index": 0, "score": 0.91},
            {"index": 1, "score": 0.45},
            {"index": 2, "score": 0.10},
        ]
    })
    _patch_async_client(monkeypatch, transport)

    provider = VllmScoreRerankerProvider(
        base_url="http://172.16.120.35:8001/v1",
        model="mxbai-rerank-large-v1",
        api_key="sk-test",
        verify_ssl=False,
    )
    candidates = [
        RerankCandidate(chunk_id=f"c{i}", content=f"doc {i}", metadata={})
        for i in range(3)
    ]
    out = await provider.rerank("申誡的條件", candidates, top_k=3)
    assert len(out) == 3

    req = transport.last_request
    assert req is not None
    assert str(req.url).endswith("/score")
    body = json.loads(req.content)
    assert body == {
        "model": "mxbai-rerank-large-v1",
        "text_1": "申誡的條件",
        "text_2": ["doc 0", "doc 1", "doc 2"],
    }
    assert req.headers["Authorization"] == "Bearer sk-test"
    assert req.headers["Content-Type"] == "application/json"


@pytest.mark.asyncio
async def test_rerank_sorts_by_score_descending(monkeypatch):
    """vLLM returns scores in original order; provider must sort and re-rank."""
    transport = _MockTransport({
        "data": [
            {"index": 0, "score": 0.10},  # doc 0 → low
            {"index": 1, "score": 0.95},  # doc 1 → highest
            {"index": 2, "score": 0.45},
        ]
    })
    _patch_async_client(monkeypatch, transport)

    provider = VllmScoreRerankerProvider(
        base_url="http://x/v1", model="mxbai-rerank-large-v1"
    )
    candidates = [
        RerankCandidate(chunk_id="a", content="doc 0", metadata={}),
        RerankCandidate(chunk_id="b", content="doc 1", metadata={}),
        RerankCandidate(chunk_id="c", content="doc 2", metadata={}),
    ]
    out = await provider.rerank("q", candidates, top_k=3)

    assert [r.candidate.chunk_id for r in out] == ["b", "c", "a"]
    assert [r.score for r in out] == [0.95, 0.45, 0.10]
    assert [r.rank for r in out] == [0, 1, 2]


@pytest.mark.asyncio
async def test_rerank_truncates_to_top_k(monkeypatch):
    transport = _MockTransport({
        "data": [
            {"index": 0, "score": 0.10},
            {"index": 1, "score": 0.95},
            {"index": 2, "score": 0.45},
            {"index": 3, "score": 0.30},
        ]
    })
    _patch_async_client(monkeypatch, transport)

    provider = VllmScoreRerankerProvider(base_url="http://x/v1", model="m")
    candidates = [
        RerankCandidate(chunk_id=str(i), content=f"d{i}", metadata={})
        for i in range(4)
    ]
    out = await provider.rerank("q", candidates, top_k=2)
    assert [r.candidate.chunk_id for r in out] == ["1", "2"]


@pytest.mark.asyncio
async def test_rerank_no_auth_header_when_api_key_blank(monkeypatch):
    transport = _MockTransport({"data": []})
    _patch_async_client(monkeypatch, transport)

    provider = VllmScoreRerankerProvider(
        base_url="http://x/v1", model="m", api_key=""
    )
    await provider.rerank("q", [RerankCandidate("a", "x", {})], top_k=1)
    assert "Authorization" not in transport.last_request.headers


@pytest.mark.asyncio
async def test_rerank_skips_out_of_range_indices(monkeypatch):
    """Defensive: a malformed server reply must not crash the pipeline."""
    transport = _MockTransport({
        "data": [
            {"index": 99, "score": 0.99},   # bogus
            {"index": 0, "score": 0.50},
            {"index": -1, "score": 0.40},   # bogus
            {"index": "abc", "score": 0.30},  # bogus
        ]
    })
    _patch_async_client(monkeypatch, transport)

    provider = VllmScoreRerankerProvider(base_url="http://x/v1", model="m")
    candidates = [RerankCandidate("a", "doc", {})]
    out = await provider.rerank("q", candidates, top_k=5)
    assert len(out) == 1
    assert out[0].candidate.chunk_id == "a"
    assert out[0].score == 0.50


@pytest.mark.asyncio
async def test_rerank_empty_input_short_circuits():
    provider = VllmScoreRerankerProvider(base_url="http://x/v1", model="m")
    assert await provider.rerank("q", [], top_k=5) == []
    cands = [RerankCandidate(chunk_id="x", content="x", metadata={})]
    assert await provider.rerank("q", cands, top_k=0) == []


# ---------------------------------------------------------------------------
# tools._arerank_candidates — failure tolerance
# ---------------------------------------------------------------------------

class _FakeReranker:
    """Stand-in Reranker for tools-level helper tests."""

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
