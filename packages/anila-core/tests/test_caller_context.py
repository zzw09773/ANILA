"""Tests for caller-context plumbing after P2.1 identity retirement.

* :class:`CallerContext` dataclass semantics remain.
* :func:`extract_caller_context` raises — plaintext X-ANILA-User-* is
  no longer trusted.
* :func:`caller_context_from_dispatch` reads verified JWT claims.
* :func:`create_subagent_context` propagates ``caller``.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI, Depends, Request
from fastapi.testclient import TestClient
from starlette.requests import Request as StarletteRequest

from anila_core.api.caller_context import (
    CallerContext,
    caller_context_from_dispatch,
    extract_caller_context,
)
from anila_core.context.agent_context import (
    AgentContext,
    create_subagent_context,
)


def test_caller_context_has_user_requires_user_id():
    assert CallerContext(user_id="1147259").has_user is True
    assert CallerContext().has_user is False
    assert CallerContext(user_id="").has_user is False


def test_caller_context_has_callback_credentials_requires_three_fields():
    full = CallerContext(
        user_id="1147259",
        service_token="csk-x",
        csp_base_url="http://csp:8000",
    )
    assert full.has_callback_credentials is True

    assert CallerContext(user_id="1147259", service_token="csk-x").has_callback_credentials is False
    assert CallerContext(user_id="1147259", csp_base_url="http://csp:8000").has_callback_credentials is False
    assert CallerContext(service_token="csk-x", csp_base_url="http://csp:8000").has_callback_credentials is False
    assert CallerContext(
        user_id="", service_token="csk-x", csp_base_url="http://csp:8000"
    ).has_callback_credentials is False
    assert CallerContext(
        user_id="1147259", service_token="   ", csp_base_url="http://csp:8000"
    ).has_callback_credentials is False
    assert CallerContext(
        user_id="1147259", service_token="csk-x", csp_base_url="   "
    ).has_callback_credentials is False


def test_extract_caller_context_raises_plaintext_retired():
    """P2.1: dependency must not silently trust X-ANILA-User-* headers."""
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 123),
        "server": ("test", 80),
    }
    req = StarletteRequest(scope)
    with pytest.raises(RuntimeError, match="no longer trusted"):
        extract_caller_context(req)


def test_extract_caller_context_dependency_returns_500_style_error():
    app = FastAPI()

    @app.get("/echo")
    def echo(caller: CallerContext = Depends(extract_caller_context)) -> dict:
        return {"user_id": caller.user_id}

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/echo", headers={"X-ANILA-User-Id": "1147259"})
    # FastAPI surfaces the RuntimeError as 500 — the point is it does NOT
    # quietly accept the plaintext header as identity.
    assert resp.status_code == 500


def test_caller_context_from_dispatch_uses_verified_claims(monkeypatch):
    monkeypatch.setenv("ANILA_CSP_BASE_URL", "http://csp:8000/")
    app = FastAPI()

    @app.get("/echo")
    def echo(request: Request) -> dict:
        request.state.anila_dispatch = {
            "user_id": 7,
            "department": 3,
            "agent_id": 42,
        }
        ctx = caller_context_from_dispatch(request, service_token="csk-x")
        return {
            "user_id": ctx.user_id,
            "csp_base_url": ctx.csp_base_url,
            "has_callback_credentials": ctx.has_callback_credentials,
        }

    client = TestClient(app)
    body = client.get("/echo").json()
    assert body["user_id"] == "7"
    assert body["csp_base_url"] == "http://csp:8000"
    assert body["has_callback_credentials"] is True


def test_subagent_inherits_caller_from_parent():
    parent_caller = CallerContext(
        user_id="1147259",
        service_token="csk-x",
        csp_base_url="http://csp:8000",
    )

    async def _make_parent() -> AgentContext:
        return AgentContext(caller=parent_caller)

    parent = asyncio.run(_make_parent())
    sub = create_subagent_context(parent)
    assert sub.caller is parent_caller


def test_subagent_inherits_none_caller_when_parent_has_none():
    async def _make_parent() -> AgentContext:
        return AgentContext()

    parent = asyncio.run(_make_parent())
    sub = create_subagent_context(parent)
    assert sub.caller is None
