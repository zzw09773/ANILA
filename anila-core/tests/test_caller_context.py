"""Tests for the route-3 Phase 3 caller-context plumbing.

Covers:

* :func:`extract_caller_context` parses the CSP-set ``X-ANILA-*`` /
  ``X-CSP-*`` headers into a typed :class:`CallerContext`.
* :func:`make_user_memory_reader` returns ``None`` on missing
  fields and a working :class:`HttpUserFactReader` when complete.
* :func:`create_subagent_context` propagates ``caller`` so a
  subagent serves the same user as its parent.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI, Depends
from fastapi.testclient import TestClient

from anila_core.api.caller_context import CallerContext, extract_caller_context
from anila_core.context.agent_context import (
    AgentContext,
    create_subagent_context,
)
from anila_core.memory.long_term import (
    HttpUserFactReader,
    make_user_memory_reader,
)


# ── CallerContext semantics ──────────────────────────────────────────────────


def test_caller_context_has_user_requires_user_id():
    assert CallerContext(user_id=42).has_user is True
    assert CallerContext().has_user is False


def test_caller_context_has_callback_credentials_requires_three_fields():
    """All three of (user_id, service_token, csp_base_url) needed.
    Pin so a refactor that quietly relaxes the check (e.g. forgets
    csp_base_url) doesn't make the factory fall over with KeyError."""
    full = CallerContext(
        user_id=1,
        service_token="csk-x",
        csp_base_url="http://csp:8000",
    )
    assert full.has_callback_credentials is True

    assert CallerContext(user_id=1, service_token="csk-x").has_callback_credentials is False
    assert CallerContext(user_id=1, csp_base_url="http://csp:8000").has_callback_credentials is False
    assert CallerContext(service_token="csk-x", csp_base_url="http://csp:8000").has_callback_credentials is False


# ── extract_caller_context FastAPI dependency ────────────────────────────────


def _make_test_app() -> FastAPI:
    app = FastAPI()

    @app.get("/echo")
    def echo(caller: CallerContext = Depends(extract_caller_context)) -> dict:
        return {
            "user_id": caller.user_id,
            "user_email": caller.user_email,
            "service_token": caller.service_token,
            "csp_base_url": caller.csp_base_url,
            "has_callback_credentials": caller.has_callback_credentials,
        }

    return app


def test_extract_caller_context_parses_full_header_set(monkeypatch):
    monkeypatch.setenv("ANILA_CSP_BASE_URL", "http://csp:8000/")  # trailing slash stripped
    client = TestClient(_make_test_app())
    resp = client.get(
        "/echo",
        headers={
            "X-ANILA-User-Id": "42",
            "X-ANILA-User-Email": "alice@example.com",
            "X-CSP-Service-Token": "csk-test",
        },
    )
    body = resp.json()
    assert body["user_id"] == 42
    assert body["user_email"] == "alice@example.com"
    assert body["service_token"] == "csk-test"
    assert body["csp_base_url"] == "http://csp:8000"  # trailing slash stripped
    assert body["has_callback_credentials"] is True


def test_extract_caller_context_tolerates_missing_headers(monkeypatch):
    monkeypatch.delenv("ANILA_CSP_BASE_URL", raising=False)
    client = TestClient(_make_test_app())
    resp = client.get("/echo")
    body = resp.json()
    assert body["user_id"] is None
    assert body["service_token"] is None
    assert body["csp_base_url"] is None
    assert body["has_callback_credentials"] is False


def test_extract_caller_context_treats_garbage_user_id_as_none(monkeypatch):
    """A malformed header shouldn't crash the request — agent
    code degrades to "no user attribution" rather than 500."""
    monkeypatch.setenv("ANILA_CSP_BASE_URL", "http://csp:8000")
    client = TestClient(_make_test_app())
    resp = client.get("/echo", headers={"X-ANILA-User-Id": "not-an-int"})
    assert resp.json()["user_id"] is None


# ── make_user_memory_reader factory ──────────────────────────────────────────


def test_make_user_memory_reader_returns_none_when_caller_missing():
    assert make_user_memory_reader(None) is None


def test_make_user_memory_reader_returns_none_on_partial_credentials():
    """Same gate as has_callback_credentials — pin separately so the
    factory doesn't drift away from CallerContext's contract."""
    partial = CallerContext(user_id=42)  # no token, no base url
    assert make_user_memory_reader(partial) is None


def test_make_user_memory_reader_returns_reader_when_complete():
    full = CallerContext(
        user_id=42,
        service_token="csk-x",
        csp_base_url="http://csp:8000",
    )
    reader = make_user_memory_reader(full)
    assert isinstance(reader, HttpUserFactReader)


def test_make_user_memory_reader_threads_timeout_kwarg():
    """The agent runtime overrides timeout for low-latency budgets;
    pin that the kwarg actually reaches the constructor."""
    full = CallerContext(
        user_id=42,
        service_token="csk-x",
        csp_base_url="http://csp:8000",
    )
    reader = make_user_memory_reader(full, timeout_seconds=1.5)
    # _timeout is private but stable — the assertion docs the
    # contract for callers tuning the read-path latency budget.
    assert reader is not None and reader._timeout == 1.5  # type: ignore[union-attr]


# ── AgentContext.caller propagation through subagent fork ────────────────────


def test_subagent_inherits_caller_from_parent():
    """A subagent serves the same user as its parent — the fork
    must propagate the immutable caller bundle so the subagent
    can call back into CSP for memory reads on the same user."""
    parent_caller = CallerContext(
        user_id=42,
        service_token="csk-x",
        csp_base_url="http://csp:8000",
    )

    async def _make_parent() -> AgentContext:
        # AgentContext.__post_init__ instantiates an asyncio.Event,
        # which needs a running loop — wrap in a coroutine so the
        # test runner provides one.
        return AgentContext(caller=parent_caller)

    parent = asyncio.run(_make_parent())
    sub = create_subagent_context(parent)
    assert sub.caller is parent_caller  # exact same frozen instance


def test_subagent_inherits_none_caller_when_parent_has_none():
    async def _make_parent() -> AgentContext:
        return AgentContext()  # no caller

    parent = asyncio.run(_make_parent())
    sub = create_subagent_context(parent)
    assert sub.caller is None
