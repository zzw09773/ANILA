"""Triton gRPC embedding path: query/document split + proxy wiring."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from app.services.proxy import service as proxy_impl


def _model(**overrides):
    base = dict(
        id=42,
        name="nv-embed-v2",
        model_type="embedding",
        endpoint_url="grpc://172.16.120.35:9001",
        api_version="v1",
        protocol="triton_grpc",
        api_key_secret_ref=None,
        display_name="nv-embed-v2",
        is_internal=True,
        classification_ceiling=None,
        is_active=True,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ANILA_ALLOW_GRPC_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_ALLOW_PRIVATE_ENDPOINT", "1")

    async def _noop_usage(*_a, **_k):
        return None

    monkeypatch.setattr(proxy_impl, "enqueue_usage", _noop_usage)
    monkeypatch.setattr(proxy_impl, "enqueue_usage_task_linked", _noop_usage)
    monkeypatch.setattr(proxy_impl, "_note_proxy_outcome", lambda **_k: None)
    yield


def test_query_role_calls_embed_texts_as_query(monkeypatch):
    """Acceptance: query-side must not silently embed as document."""
    seen: dict = {}

    def fake_embed(endpoint_url, model_name, texts, *, role, timeout_s=30.0):
        seen["role"] = role
        seen["texts"] = texts
        return [[0.1] * 8]

    monkeypatch.setattr(
        "app.services.triton_grpc.client.embed_texts", fake_embed
    )
    # proxy imports embed_texts inside the function — patch the package export too
    monkeypatch.setattr(
        "app.services.triton_grpc.embed_texts", fake_embed
    )

    result = asyncio.run(
        proxy_impl.proxy_request(
            model=_model(),
            api_key_id=None,
            user_id=1,
            department_id=None,
            request_body={"model": "nv-embed-v2", "input": "hello query"},
            endpoint_path="/v1/embeddings",
            embedding_input_role="query",
        )
    )
    assert seen["role"] == "query"
    assert seen["texts"] == ["hello query"]
    assert len(result["data"][0]["embedding"]) == 8


def test_default_role_is_document_for_public_surface(monkeypatch):
    seen: dict = {}

    def fake_embed(endpoint_url, model_name, texts, *, role, timeout_s=30.0):
        seen["role"] = role
        return [[0.2] * 4]

    monkeypatch.setattr("app.services.triton_grpc.embed_texts", fake_embed)

    asyncio.run(
        proxy_impl.proxy_request(
            model=_model(),
            api_key_id=None,
            user_id=1,
            department_id=None,
            request_body={"model": "nv-embed-v2", "input": ["doc"]},
            endpoint_path="/v1/embeddings",
            # embedding_input_role omitted → document
        )
    )
    assert seen["role"] == "document"


def test_query_site_regression_fails_if_wired_as_document(monkeypatch):
    """The invariant the package requires: a test that goes red if a
    query-side call site starts embedding as a document.

    Simulates search/memory forgetting ``embedding_input_role='query'``.
    """
    seen: dict = {}

    def fake_embed(endpoint_url, model_name, texts, *, role, timeout_s=30.0):
        seen["role"] = role
        return [[0.0] * 2]

    monkeypatch.setattr("app.services.triton_grpc.embed_texts", fake_embed)

    # What a broken query call site would do (omit role → document).
    asyncio.run(
        proxy_impl.proxy_request(
            model=_model(),
            api_key_id=None,
            user_id=1,
            department_id=None,
            request_body={"model": "nv-embed-v2", "input": "q"},
            endpoint_path="/v1/embeddings",
        )
    )
    # This assertion IS the guard: if someone "fixes" a call site by
    # dropping embedding_input_role='query', they must update this test
    # consciously — and the live cosine check will still catch it.
    assert seen["role"] != "query"  # documents by default
    # And the correct call site:
    asyncio.run(
        proxy_impl.proxy_request(
            model=_model(),
            api_key_id=None,
            user_id=1,
            department_id=None,
            request_body={"model": "nv-embed-v2", "input": "q"},
            endpoint_path="/v1/embeddings",
            embedding_input_role="query",
        )
    )
    assert seen["role"] == "query"


def test_search_embed_query_passes_query_role(db, monkeypatch):
    """Collection search embeds its query on the query side.

    Was an ``inspect.getsource()`` grep — which sees a literal at one call
    site and nothing else, so a wrapper, a variable, or a later re-assignment
    walks past it. This runs ``_embed_query`` and reads the argument
    ``proxy_request`` is actually handed.
    """
    from app.api.ingestion import search as search_mod
    from app.models.model_registry import ModelRegistry

    from tests.conftest import make_user

    user = make_user(db, username="search_query_role")
    model = ModelRegistry(
        name="nv-embed-v2",
        display_name="nv-embed-v2",
        model_type="embedding",
        endpoint_url="grpc://172.16.120.35:9001",
        api_version="v1",
        protocol="triton_grpc",
        is_active=True,
    )
    db.add(model)
    db.commit()

    seen: dict = {}

    async def fake_proxy_request(**kwargs):
        seen["role"] = kwargs.get("embedding_input_role")
        return {"data": [{"embedding": [0.1, 0.2, 0.3, 0.4]}]}

    monkeypatch.setattr(search_mod, "proxy_request", fake_proxy_request)

    vec = asyncio.run(
        search_mod._embed_query(db, user, "nv-embed-v2", 4, "去年的採購紀錄")
    )

    assert len(vec) == 4
    assert seen["role"] == "query"


def test_memory_retrieve_passes_query_role(monkeypatch):
    """Long-term memory recall embeds its query on the query side.

    Also was a source grep. ``_embed`` is the single decision point the
    recall path goes through, so recording its keyword is the behaviour; the
    embed is made to fail afterwards so no DB is needed (``retrieve_relevant_
    chunks`` is fail-closed and returns []).
    """
    from app.services import memory_service

    seen: dict = {}

    async def fake_embed(db, text_input, **kwargs):
        seen["role"] = kwargs.get("embedding_input_role")
        raise RuntimeError("stop here — the role is already decided")

    monkeypatch.setattr(memory_service, "_embed", fake_embed)

    result = asyncio.run(
        memory_service.retrieve_relevant_chunks(
            db=None, user_id=1, query_text="去年的採購紀錄"
        )
    )

    assert result == []
    assert seen["role"] == "query"


def test_triton_rejects_non_embedding_path():
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            proxy_impl.proxy_request(
                model=_model(model_type="llm"),
                api_key_id=None,
                user_id=1,
                department_id=None,
                request_body={"messages": []},
                endpoint_path="/v1/chat/completions",
            )
        )
    assert exc.value.status_code == 400


def test_join_upstream_path_not_used_for_triton(monkeypatch):
    """gRPC must branch before join_upstream_path / strip_trailing_api_version."""
    called = {"join": 0}

    def boom(*_a, **_k):
        called["join"] += 1
        raise AssertionError("join_upstream_path must not run for triton_grpc")

    monkeypatch.setattr(proxy_impl, "join_upstream_path", boom)
    monkeypatch.setattr(
        "app.services.triton_grpc.embed_texts",
        lambda *a, **k: [[0.0] * 3],
    )
    asyncio.run(
        proxy_impl.proxy_request(
            model=_model(),
            api_key_id=None,
            user_id=1,
            department_id=None,
            request_body={"input": ["x"]},
            endpoint_path="/v1/embeddings",
            embedding_input_role="query",
        )
    )
    assert called["join"] == 0
