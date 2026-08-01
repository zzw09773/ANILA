"""CSP HTTP retriever 的 request/response 形狀契約（平台契約）。"""

from __future__ import annotations

import httpx
import pytest

from anila_agent.dispatch_token import (
    MissingDispatchTokenError,
    dispatch_bearer_scope,
)
from anila_agent.retrieval.csp_http import CspHttpRetriever

pytestmark = pytest.mark.unit


class _FakeResp:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    def __init__(self, capture: dict, payload: dict, **_kw) -> None:
        self._capture = capture
        self._payload = payload

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *_a) -> bool:
        return False

    async def post(self, url, headers=None, json=None) -> _FakeResp:
        self._capture.update(url=url, headers=headers, json=json)
        return _FakeResp(self._payload)


def _patch_client(monkeypatch, capture, payload):
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _FakeClient(capture, payload, **kw))


async def test_request_shape(monkeypatch):
    cap: dict = {}
    _patch_client(monkeypatch, cap, {"results": []})
    r = CspHttpRetriever(csp_base_url="https://csp.internal/", collection_id=2, api_key="csk-x")
    await r.search("hello", k=4)
    # 路徑用 CSP origin（非 /v1 proxy base），尾斜線已去除。
    assert cap["url"] == "https://csp.internal/api/ingestion/collections/2/search"
    # Out-of-task / CLI fallback still accepts constructor api_key.
    assert cap["headers"]["Authorization"] == "Bearer csk-x"
    assert cap["json"] == {"query": "hello", "top_k": 4, "min_score": 0.25}


async def test_dispatch_scope_beats_constructor_api_key(monkeypatch):
    """In-task: request-scoped dispatch JWT wins over any leftover api_key."""
    cap: dict = {}
    _patch_client(monkeypatch, cap, {"results": []})
    r = CspHttpRetriever(
        csp_base_url="https://csp", collection_id=2, api_key="csk-should-not-win"
    )
    with dispatch_bearer_scope("dispatch-jwt-A"):
        await r.search("q")
    assert cap["headers"]["Authorization"] == "Bearer dispatch-jwt-A"


async def test_search_without_scope_or_api_key_fails_loudly(monkeypatch):
    _patch_client(monkeypatch, {}, {"results": []})
    r = CspHttpRetriever(csp_base_url="https://csp", collection_id=2, api_key=None)
    with pytest.raises(MissingDispatchTokenError, match="no dispatch JWT"):
        await r.search("q")


async def test_response_mapping(monkeypatch):
    payload = {
        "results": [
            {
                "chunk_id": 7,
                "content": "hello world",
                "score": 0.91,
                "chunk_key": "k1",
                "document_id": 3,
                "filename": "f.txt",
                "metadata": {"page": 2},
            }
        ]
    }
    _patch_client(monkeypatch, {}, payload)
    r = CspHttpRetriever(csp_base_url="https://csp", collection_id=2, api_key="csk-x")
    docs = await r.search("q")
    d = docs[0]
    assert d.id == "7" and d.text == "hello world" and d.score == 0.91
    assert d.metadata["chunk_key"] == "k1"
    assert d.metadata["document_id"] == 3
    assert d.metadata["filename"] == "f.txt"
    assert d.metadata["page"] == 2  # 巢狀 metadata 攤平合併


async def test_non_list_results_yield_empty(monkeypatch):
    _patch_client(monkeypatch, {}, {"results": None})
    r = CspHttpRetriever(csp_base_url="https://csp", collection_id=2, api_key="csk-x")
    assert await r.search("q") == []


async def test_fetch_returns_none(monkeypatch):
    _patch_client(monkeypatch, {}, {"results": []})
    r = CspHttpRetriever(csp_base_url="https://csp", collection_id=2, api_key="csk-x")
    assert await r.fetch("7") is None  # 契約：chunk 已帶全文，fetch 回 None


def test_rejects_bad_collection_id():
    with pytest.raises(ValueError):
        CspHttpRetriever(csp_base_url="https://csp", collection_id=0, api_key="csk-x")
    with pytest.raises(ValueError):
        CspHttpRetriever(csp_base_url="https://csp", collection_id=True, api_key="csk-x")  # bool 不算 int
