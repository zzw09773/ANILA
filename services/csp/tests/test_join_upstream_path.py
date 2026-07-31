# -*- coding: utf-8 -*-
"""Tests for join_upstream_path and every converted outbound URL join site.

Covers the behaviour table for the shared helper, then proves each call site
produces the same single correct URL under both stored conventions
(bare host vs trailing ``/v1`` / ``/v2``). The streaming-chat case is the
reported defect (``…/v1/v1/chat/completions`` 404); cross-version stacking
(``…/v2/v1/...``) is the regression this round closes.
"""
from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api import models as models_api
from app.api import proxy as proxy_api
from app.api.agents import health as agent_health
from app.services import health_checker, memory_service, prompt_gen_service
from app.services.auth_service import create_tokens
from app.services.proxy import service as proxy_impl
from app.services.proxy.urls import join_upstream_path
from tests.conftest import make_model, make_user


# ── helper behaviour table ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "base,path,expected",
    [
        # base /v1 + path /v1/...
        ("http://h:1/v1", "/v1/chat/completions", "http://h:1/v1/chat/completions"),
        # base bare + path /v1/...
        ("http://h:1", "/v1/chat/completions", "http://h:1/v1/chat/completions"),
        ("http://h:1/", "/v1/chat/completions", "http://h:1/v1/chat/completions"),
        # base /v2 + path /v1/... (path version wins; no /v2/v1 stack)
        ("http://h:1/v2", "/v1/models", "http://h:1/v1/models"),
        ("http://h:1/v2", "/v1/embeddings", "http://h:1/v1/embeddings"),
        ("http://h:1/v2", "/v1/chat/completions", "http://h:1/v1/chat/completions"),
        # base /v1 + path /v2/... (path version wins; embedding special case)
        ("http://h:1/v1", "/v2/embeddings", "http://h:1/v2/embeddings"),
        # base /v2 + path /v2/...
        ("http://h:1/v2", "/v2/embeddings", "http://h:1/v2/embeddings"),
        # real sub-path survives
        (
            "http://h:1/openai",
            "/v1/chat/completions",
            "http://h:1/openai/v1/chat/completions",
        ),
        # version in the MIDDLE is not trailing — must be preserved
        (
            "http://h:1/v1/proxy",
            "/v1/chat/completions",
            "http://h:1/v1/proxy/v1/chat/completions",
        ),
    ],
)
def test_join_upstream_path_behaviour_table(base, path, expected):
    out = join_upstream_path(base, path)
    assert out == expected
    assert "/v1/v1/" not in out
    assert "/v2/v1/" not in out
    assert "/v1/v2/" not in out
    assert "//" not in out.split("://", 1)[-1]


def test_join_upstream_path_never_double_version_or_slash():
    for base in ("http://h:1/v1", "http://h:1", "http://h:1/", "http://h:1/v2"):
        out = join_upstream_path(base, "/v1/models")
        assert out == "http://h:1/v1/models"
        assert "/v1/v1/" not in out
        assert "/v2/v1/" not in out
        assert "//" not in out.split("://", 1)[-1]


def test_join_upstream_path_same_result_under_both_conventions():
    """Bare host and trailing /vN bases all resolve to the path's version."""
    path = "/v1/chat/completions"
    a = join_upstream_path("http://h:1/v1", path)
    b = join_upstream_path("http://h:1", path)
    c = join_upstream_path("http://h:1/", path)
    d = join_upstream_path("http://h:1/v2", path)
    assert a == b == c == d == "http://h:1/v1/chat/completions"
    # Re-joining a stripped version base (not join(join(b,p),p)) stays stable.
    assert join_upstream_path(a.rsplit("/chat", 1)[0], path) == a


def test_join_upstream_path_not_idempotent_under_double_apply():
    """Docstring correction: join(join(b,p),p) stacks — no call site does this."""
    path = "/v1/chat/completions"
    once = join_upstream_path("http://h:1", path)
    twice = join_upstream_path(once, path)
    assert once == "http://h:1/v1/chat/completions"
    assert twice == "http://h:1/v1/chat/completions/v1/chat/completions"


def test_join_upstream_path_non_version_probe_paths():
    assert join_upstream_path("http://h:1/v1", "/health") == "http://h:1/v1/health"
    assert join_upstream_path("http://h:1", "/health") == "http://h:1/health"
    assert join_upstream_path("http://h:1/v1", "/") == "http://h:1/v1/"
    assert join_upstream_path("http://h:1", "/") == "http://h:1/"
    # Non-version probes do not strip a trailing version on the base.
    assert join_upstream_path("http://h:1/v2", "/health") == "http://h:1/v2/health"


# ── site: streaming model chat (reported defect) ──────────────────────────────


@pytest.mark.parametrize(
    "endpoint_url",
    [
        "http://mock-llm:8080/v1",
        "http://mock-llm:8080",
        "http://mock-llm:8080/",
        "http://mock-llm:8080/v2",
    ],
)
def test_streaming_model_chat_both_conventions_same_url(
    client: TestClient, db: Session, monkeypatch, endpoint_url: str
):
    """proxy.py streaming path must not emit …/v1/v1/chat/completions."""
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "mock-llm")
    suffix = abs(hash(endpoint_url)) % 100000
    admin = make_user(db, username=f"stream_join_{suffix}", role="admin")
    model = make_model(db, name=f"stream-join-{suffix}")
    model.endpoint_url = endpoint_url
    db.commit()

    captured: dict[str, Any] = {}

    async def fake_proxy_stream(**kwargs):
        captured["target_url"] = kwargs["target_url"]
        yield 'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(proxy_api, "proxy_stream", fake_proxy_stream)

    token = create_tokens(admin)["access_token"]
    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "model": model.name,
            "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert resp.status_code == 200, resp.text
    _ = resp.text
    assert captured["target_url"] == "http://mock-llm:8080/v1/chat/completions"
    assert "/v1/v1/" not in captured["target_url"]
    assert "/v2/v1/" not in captured["target_url"]


# ── site: proxy_request (non-streaming / embeddings) ──────────────────────────


def _capture_guard_url(monkeypatch, module) -> dict[str, Any]:
    """Abort at the SSRF guard after URL construction — no network needed."""
    captured: dict[str, Any] = {}

    def _guard(url, endpoint_kind=None):
        captured["url"] = url
        raise HTTPException(status_code=502, detail="stop-after-url-capture")

    monkeypatch.setattr(module, "_guard_outbound", _guard)
    return captured


@pytest.mark.parametrize(
    "endpoint_url",
    [
        "http://mock-llm:8080/v1",
        "http://mock-llm:8080",
        "http://mock-llm:8080/",
        "http://mock-llm:8080/v2",
    ],
)
def test_proxy_request_both_conventions_same_url(monkeypatch, endpoint_url: str):
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "mock-llm")
    captured = _capture_guard_url(monkeypatch, proxy_impl)

    model = SimpleNamespace(
        endpoint_url=endpoint_url,
        model_type="llm",
        api_version="v1",
        name="join-llm",
        id=1,
    )
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            proxy_impl.proxy_request(
                model=model,
                api_key_id=1,
                user_id=1,
                department_id=None,
                request_body={"messages": [{"role": "user", "content": "hi"}]},
                endpoint_path="/v1/chat/completions",
            )
        )
    assert exc.value.status_code == 502
    assert captured["url"] == "http://mock-llm:8080/v1/chat/completions"


@pytest.mark.parametrize(
    "endpoint_url",
    [
        "http://mock-llm:8080/v1",
        "http://mock-llm:8080",
        "http://mock-llm:8080/v2",
    ],
)
def test_proxy_request_v2_embedding_special_case(monkeypatch, endpoint_url: str):
    """api_version==v2 builds /v2/embeddings; path version wins over base /v1."""
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "mock-llm")
    captured = _capture_guard_url(monkeypatch, proxy_impl)

    model = SimpleNamespace(
        endpoint_url=endpoint_url,
        model_type="embedding",
        api_version="v2",
        name="join-embed",
        id=2,
    )
    with pytest.raises(HTTPException):
        asyncio.run(
            proxy_impl.proxy_request(
                model=model,
                api_key_id=1,
                user_id=1,
                department_id=None,
                request_body={"input": ["hi"]},
                endpoint_path="/v1/embeddings",
            )
        )
    assert captured["url"] == "http://mock-llm:8080/v2/embeddings"


# ── site: agent dispatch (streaming + non-streaming) ──────────────────────────


@pytest.mark.parametrize(
    "endpoint_url",
    [
        "http://agent-box:9100/v1",
        "http://agent-box:9100",
        "http://agent-box:9100/",
        "http://agent-box:9100/v2",
    ],
)
def test_agent_dispatch_join_both_conventions(endpoint_url: str):
    expected = "http://agent-box:9100/v1/chat/completions"
    assert join_upstream_path(endpoint_url, "/v1/chat/completions") == expected
    assert "/v1/v1/" not in expected
    assert "/v2/v1/" not in expected
    assert proxy_api.join_upstream_path is join_upstream_path


# ── site: agents/health.py ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "endpoint_url",
    ["http://agent-box:9100/v1", "http://agent-box:9100", "http://agent-box:9100/v2"],
)
def test_agent_health_chat_url_both_conventions(endpoint_url: str):
    assert (
        join_upstream_path(endpoint_url, "/v1/chat/completions")
        == "http://agent-box:9100/v1/chat/completions"
    )


@pytest.mark.parametrize(
    "endpoint_url",
    ["http://agent-box:9100/v1", "http://agent-box:9100", "http://agent-box:9100/v2"],
)
def test_agent_health_probe_models_path_both_conventions(endpoint_url: str):
    from app.services.health_checker import REAL_PROBE_PATHS, WEAK_PROBE_PATHS, _probe_url

    urls = [_probe_url(endpoint_url, p) for p in (*REAL_PROBE_PATHS, *WEAK_PROBE_PATHS)]
    assert urls[1] == "http://agent-box:9100/v1/models"
    assert urls[0] == "http://agent-box:9100/health"
    assert "/v1/v1/" not in urls[1]
    assert "/v2/v1/" not in urls[1]
    assert "/v1/health" not in urls[0]


def test_agent_health_module_imports_join_helper():
    assert agent_health.join_upstream_path is join_upstream_path


def test_agent_health_guards_once_per_host(monkeypatch):
    """Invariant 3: validate_outbound_url once, not once per probe path."""
    calls: list[str] = []

    def _guard(url, endpoint_kind=None):
        calls.append(url)
        return None

    monkeypatch.setattr(agent_health, "validate_outbound_url", _guard)

    class _Resp:
        status_code = 200

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url):
            return _Resp()

    # Manual health-check now delegates probing to health_checker (shared
    # REAL/WEAK path logic); patch the client there. Outer guard stays on
    # the agents.health module; probe is called with skip_validate=True.
    monkeypatch.setattr(health_checker.httpx, "AsyncClient", _Client)

    agent = SimpleNamespace(
        id=1, name="a", endpoint_url="http://agent-box:9100/v1", health_status="unknown"
    )
    admin = SimpleNamespace(id=1, username="admin", role="admin")
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = agent
    request = MagicMock()
    request.client = SimpleNamespace(host="127.0.0.1")
    request.headers = {}

    monkeypatch.setattr(agent_health, "log_audit_event", lambda *a, **k: None)
    monkeypatch.setattr(agent_health, "_client_ip", lambda r: "127.0.0.1")

    result = asyncio.run(
        agent_health.trigger_agent_health_check(1, request, admin, db)
    )
    assert result["status"] == "healthy"
    assert len(calls) == 1
    assert calls[0] == "http://agent-box:9100/v1"


# ── site: models._upstream_models_url ─────────────────────────────────────────


@pytest.mark.parametrize(
    "endpoint_url",
    [
        "http://mock-llm:8080/v1",
        "http://mock-llm:8080",
        "http://mock-llm:8080/",
        "http://mock-llm:8080/v2",
    ],
)
def test_upstream_models_url_both_conventions(endpoint_url: str):
    assert models_api._upstream_models_url(endpoint_url) == (
        "http://mock-llm:8080/v1/models"
    )
    assert "/v2/v1/" not in models_api._upstream_models_url(endpoint_url)


def test_upstream_models_url_already_ends_with_models():
    assert (
        models_api._upstream_models_url("http://mock-llm:8080/v1/models")
        == "http://mock-llm:8080/v1/models"
    )


# ── site: memory_service embed + extraction ───────────────────────────────────


@pytest.mark.parametrize(
    "endpoint_url",
    [
        "http://nv-embed:8000/v1",
        "http://nv-embed:8000",
        "http://nv-embed:8000/",
        "http://nv-embed:8000/v2",
    ],
)
def test_memory_embed_url_both_conventions(monkeypatch, endpoint_url: str):
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "nv-embed")
    captured: dict[str, Any] = {}

    from types import SimpleNamespace

    from app.services.platform_embedding import PlatformEmbedding

    fake_model = SimpleNamespace(
        name="nv-embed",
        endpoint_url=endpoint_url.rstrip("/"),
        is_platform_embedding=True,
        embedding_native_dim=3,
        is_active=True,
    )
    monkeypatch.setattr(
        memory_service,
        "resolve_platform_embedding",
        lambda db: PlatformEmbedding(
            model=fake_model, native_dim=3, truncates=False
        ),
    )
    monkeypatch.setattr(
        memory_service,
        "_guard_outbound",
        lambda url: captured.setdefault("guarded", url),
    )
    monkeypatch.setattr(memory_service, "_apply_gateway_auth", lambda h: h)
    monkeypatch.setattr(
        memory_service,
        "truncate_embedding",
        lambda v, pad_from=None: v[:2] if len(v) > 2 else list(v),
    )

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"embedding": [0.1, 0.2, 0.3]}]}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None, headers=None):
            captured["url"] = url
            return _Resp()

    monkeypatch.setattr(memory_service.httpx, "AsyncClient", _Client)
    vec, source, native = asyncio.run(memory_service._embed(MagicMock(), "hello"))
    assert vec == [0.1, 0.2]
    assert source == "nv-embed"
    assert native == 3
    assert captured["url"] == "http://nv-embed:8000/v1/embeddings"
    assert captured["guarded"] == captured["url"]
    assert "/v2/v1/" not in captured["url"]


@pytest.mark.parametrize(
    "endpoint_url",
    ["http://gemma:8000/v1", "http://gemma:8000", "http://gemma:8000/v2"],
)
def test_memory_extract_url_both_conventions(monkeypatch, endpoint_url: str):
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "gemma")
    captured: dict[str, Any] = {}

    monkeypatch.setattr(
        memory_service,
        "_resolve_extraction_target",
        lambda db: ("gemma4", endpoint_url.rstrip("/")),
    )
    monkeypatch.setattr(
        memory_service,
        "_guard_outbound",
        lambda url: captured.setdefault("guarded", url),
    )
    monkeypatch.setattr(memory_service, "_EXTRACT_MIN_CHARS", 1)

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "[]"}}]}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None, headers=None):
            captured["url"] = url
            return _Resp()

    monkeypatch.setattr(memory_service.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(memory_service, "parse_extraction_response", lambda raw: [])
    asyncio.run(
        memory_service._extract_facts(MagicMock(), "user said enough text here")
    )
    assert captured["url"] == "http://gemma:8000/v1/chat/completions"
    assert captured["guarded"] == captured["url"]


# ── site: prompt_gen_service ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "endpoint_url",
    [
        "http://primary:8000/v1",
        "http://primary:8000",
        "http://primary:8000/",
        "http://primary:8000/v2",
    ],
)
def test_prompt_gen_url_both_conventions(monkeypatch, endpoint_url: str):
    captured: dict[str, Any] = {}

    monkeypatch.setattr(
        prompt_gen_service,
        "_resolve_primary_llm",
        lambda db: ("gemma4", endpoint_url),
    )
    monkeypatch.setattr(
        prompt_gen_service,
        "validate_outbound_url",
        lambda url: captured.setdefault("guarded", url),
    )

    class _Boom(Exception):
        pass

    class _Client:
        def __init__(self, *a, **k):
            raise _Boom("stop-before-network")

    monkeypatch.setattr(prompt_gen_service.httpx, "AsyncClient", _Client)

    col = SimpleNamespace(id=1, name="kb", description="", created_by=1)
    user = SimpleNamespace(id=1, role="admin")
    db = MagicMock()
    q_col = MagicMock()
    q_col.filter.return_value.first.return_value = col
    q_docs = MagicMock()
    q_docs.filter.return_value.order_by.return_value.limit.return_value.all.return_value = (
        []
    )

    def _query(*models):
        if models and models[0] is prompt_gen_service.IngestionCollection:
            return q_col
        return q_docs

    db.query.side_effect = _query

    with pytest.raises(_Boom):
        asyncio.run(
            prompt_gen_service.generate_system_prompt(db, 1, "ideas here", user)
        )

    assert captured["guarded"] == "http://primary:8000/v1/chat/completions"


# ── site: health_checker probe paths ──────────────────────────────────────────


@pytest.mark.parametrize(
    "endpoint_url",
    [
        "http://mock-llm:8080/v1",
        "http://mock-llm:8080",
        "http://mock-llm:8080/",
        "http://mock-llm:8080/v2",
    ],
)
def test_health_checker_probe_urls_both_conventions(monkeypatch, endpoint_url: str):
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "mock-llm")
    seen: list[str] = []

    class _Resp:
        def __init__(self, status_code: int):
            self.status_code = status_code

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url):
            seen.append(url)
            if url.endswith("/v1/models"):
                return _Resp(200)
            raise health_checker.httpx.ConnectError("skip")

    monkeypatch.setattr(health_checker.httpx, "AsyncClient", _Client)
    status, _latency = asyncio.run(
        health_checker.probe_model_health_detailed(endpoint_url)
    )
    assert status == health_checker.HEALTH_HEALTHY
    assert "http://mock-llm:8080/v1/models" in seen
    assert not any("/v1/v1/" in u or "/v2/v1/" in u for u in seen)


def test_health_checker_guards_once_per_host(monkeypatch):
    """Invariant 3: one validate_outbound_url call per probe, not three."""
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "mock-llm")
    calls: list[str] = []

    def _guard(url, endpoint_kind=None):
        calls.append(url)
        return None

    monkeypatch.setattr(health_checker, "validate_outbound_url", _guard)

    class _Resp:
        status_code = 200

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url):
            return _Resp()

    monkeypatch.setattr(health_checker.httpx, "AsyncClient", _Client)
    status, _ = asyncio.run(
        health_checker.probe_model_health_detailed("http://mock-llm:8080/v1")
    )
    assert status == health_checker.HEALTH_HEALTHY
    assert len(calls) == 1
    assert calls[0] == "http://mock-llm:8080/v1"
