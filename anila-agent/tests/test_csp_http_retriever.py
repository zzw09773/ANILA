"""CspHttpRetriever — retrieval via CSP's HTTP search API + the build_agent
retriever escape hatch (#114).

httpx is mocked at the module level (no respx in this venv, no network).
"""
from __future__ import annotations

import httpx
import pytest

from anila_agent.retrieval import csp_http
from anila_agent.retrieval.csp_http import CspHttpRetriever, from_env


# ── httpx fake ──────────────────────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "err", request=httpx.Request("POST", "http://x"),
                response=httpx.Response(self.status_code),
            )

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    def __init__(self, payload: dict, capture: dict, **_: object) -> None:
        self._payload = payload
        self._capture = capture

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def post(self, url: str, *, headers=None, json=None) -> _FakeResponse:
        self._capture.update(url=url, headers=headers, json=json)
        return _FakeResponse(self._payload)


def _patch_httpx(monkeypatch, payload: dict) -> dict:
    capture: dict = {}
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda *a, **k: _FakeClient(payload, capture, **k),
    )
    return capture


_HIT = {
    "chunk_id": 42,
    "document_id": 7,
    "filename": "spec.pdf",
    "chunk_key": "c-1",
    "content": "the answer is 42",
    "score": 0.91,
    "metadata": {"page": 3},
}


# ── search() ─────────────────────────────────────────────────────────────────


async def test_search_maps_hits_to_documents(monkeypatch):
    _patch_httpx(monkeypatch, {"results": [_HIT]})
    r = CspHttpRetriever(base_url="https://csp.test", collection_id=5, api_key="sk-x")
    docs = await r.search("q", k=3)
    assert len(docs) == 1
    d = docs[0]
    assert d.id == "42" and d.text == "the answer is 42" and d.score == 0.91
    assert d.metadata["chunk_key"] == "c-1"
    assert d.metadata["document_id"] == 7
    assert d.metadata["filename"] == "spec.pdf"
    assert d.metadata["page"] == 3  # extra metadata merged through


async def test_search_request_shape(monkeypatch):
    cap = _patch_httpx(monkeypatch, {"results": []})
    r = CspHttpRetriever(
        base_url="https://csp.test/", collection_id=9, api_key="sk-key", min_score=0.4
    )
    await r.search("hello", k=7)
    assert cap["url"] == "https://csp.test/api/ingestion/collections/9/search"
    assert cap["headers"]["Authorization"] == "Bearer sk-key"
    assert cap["json"] == {"query": "hello", "top_k": 7, "min_score": 0.4}


async def test_search_empty_results(monkeypatch):
    _patch_httpx(monkeypatch, {"results": []})
    r = CspHttpRetriever(base_url="https://csp.test", collection_id=5, api_key="sk-x")
    assert await r.search("q") == []


async def test_search_missing_results_key(monkeypatch):
    _patch_httpx(monkeypatch, {"query": "q"})  # no 'results'
    r = CspHttpRetriever(base_url="https://csp.test", collection_id=5, api_key="sk-x")
    assert await r.search("q") == []


async def test_fetch_returns_none():
    r = CspHttpRetriever(base_url="https://csp.test", collection_id=5, api_key="sk-x")
    assert await r.fetch("42") is None


def test_name_and_metadata():
    r = CspHttpRetriever(base_url="https://csp.test", collection_id=5, api_key="sk-x")
    assert r.name == "csp-http:collection=5"
    assert r.metadata == {
        "backend": "csp-http", "collection_id": 5, "base_url": "https://csp.test",
    }


# ── validation ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("bad", [0, -1, True])
def test_invalid_collection_id_raises(bad):
    with pytest.raises(ValueError):
        CspHttpRetriever(base_url="https://csp.test", collection_id=bad, api_key="k")


def test_empty_api_key_raises():
    with pytest.raises(ValueError):
        CspHttpRetriever(base_url="https://csp.test", collection_id=1, api_key="")


# ── from_env ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for v in (
        "ANILA_CSP_BASE_URL", "ANILA_COLLECTION_ID", "ANILA_CSP_API_KEY",
        "ANILA_API_KEY", "ANILA_CSP_MIN_SCORE", "ANILA_SSL_VERIFY",
    ):
        monkeypatch.delenv(v, raising=False)


def test_from_env_none_when_unconfigured():
    assert from_env() is None


def test_from_env_none_without_base_url(monkeypatch):
    monkeypatch.setenv("ANILA_COLLECTION_ID", "5")
    assert from_env() is None  # base_url gate keeps it distinct from pgvector


def test_from_env_builds(monkeypatch):
    monkeypatch.setenv("ANILA_CSP_BASE_URL", "https://csp.test")
    monkeypatch.setenv("ANILA_COLLECTION_ID", "5")
    monkeypatch.setenv("ANILA_CSP_API_KEY", "sk-csp")
    r = from_env()
    assert isinstance(r, CspHttpRetriever)
    assert r.name == "csp-http:collection=5"


def test_from_env_api_key_fallback(monkeypatch):
    monkeypatch.setenv("ANILA_CSP_BASE_URL", "https://csp.test")
    monkeypatch.setenv("ANILA_COLLECTION_ID", "5")
    monkeypatch.setenv("ANILA_API_KEY", "sk-shared")
    assert isinstance(from_env(), CspHttpRetriever)


def test_from_env_missing_key_raises(monkeypatch):
    monkeypatch.setenv("ANILA_CSP_BASE_URL", "https://csp.test")
    monkeypatch.setenv("ANILA_COLLECTION_ID", "5")
    with pytest.raises(ValueError):
        from_env()


def test_from_env_non_int_collection_raises(monkeypatch):
    monkeypatch.setenv("ANILA_CSP_BASE_URL", "https://csp.test")
    monkeypatch.setenv("ANILA_COLLECTION_ID", "abc")
    monkeypatch.setenv("ANILA_CSP_API_KEY", "sk-x")
    with pytest.raises(ValueError):
        from_env()


def test_protocol_compliance():
    from anila_agent.retrieval.base import Retriever
    assert isinstance(
        CspHttpRetriever(base_url="https://csp.test", collection_id=1, api_key="k"),
        Retriever,
    )


# ── build_agent retriever escape hatch (H1/H2) ───────────────────────────────


def test_build_agent_retriever_escape_hatch(monkeypatch):
    """An explicit retriever= overrides env detection and is installed."""
    from anila_agent.core.agent import build_agent
    from anila_agent.retrieval.dummy import DummyRetriever
    from anila_agent.tools.rag_tools import get_retriever
    from anila_agent.utils.config import load_config

    # Make sure no env-based retriever could win.
    for v in ("ANILA_CSP_BASE_URL", "ANILA_COLLECTION_ID", "PGVECTOR_URL"):
        monkeypatch.delenv(v, raising=False)

    stub = DummyRetriever()
    build_agent(load_config("configs"), retriever=stub, session_id="escape-hatch")
    assert get_retriever() is stub
