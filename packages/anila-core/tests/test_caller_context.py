"""Tests for the route-3 Phase 3 caller-context plumbing.

Covers:

* :func:`extract_caller_context` parses the CSP-set ``X-ANILA-*`` /
  ``X-CSP-*`` headers into a typed :class:`CallerContext`.
* :func:`create_subagent_context` propagates ``caller`` so a
  subagent serves the same user as its parent.

Identity note: ``X-ANILA-User-Id`` carries the employee ID (員編) on
the card-login intranet branch — an opaque, stable *string* (NOT a DB
primary key). These tests pin that string contract: non-numeric
identities are kept verbatim; only blank / whitespace degrades to None.
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


# ── CallerContext semantics ──────────────────────────────────────────────────


def test_caller_context_has_user_requires_user_id():
    assert CallerContext(user_id="990000002").has_user is True
    assert CallerContext().has_user is False
    # 員編 is a string now; blank / whitespace-only is "no identity".
    assert CallerContext(user_id="").has_user is False


def test_caller_context_has_callback_credentials_requires_three_fields():
    """All three of (user_id, service_token, csp_base_url) needed.
    Pin so a refactor that quietly relaxes the check (e.g. forgets
    csp_base_url) doesn't make the factory fall over with KeyError."""
    full = CallerContext(
        user_id="990000002",
        service_token="csk-x",
        csp_base_url="http://csp:8000",
    )
    assert full.has_callback_credentials is True

    assert CallerContext(user_id="990000002", service_token="csk-x").has_callback_credentials is False
    assert CallerContext(user_id="990000002", csp_base_url="http://csp:8000").has_callback_credentials is False
    assert CallerContext(service_token="csk-x", csp_base_url="http://csp:8000").has_callback_credentials is False
    # Blank / whitespace fields must not satisfy the callback gate (would
    # otherwise build a reader that calls /users//facts or auths with "").
    assert CallerContext(
        user_id="", service_token="csk-x", csp_base_url="http://csp:8000"
    ).has_callback_credentials is False
    assert CallerContext(
        user_id="990000002", service_token="   ", csp_base_url="http://csp:8000"
    ).has_callback_credentials is False
    assert CallerContext(
        user_id="990000002", service_token="csk-x", csp_base_url="   "
    ).has_callback_credentials is False


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
            "X-ANILA-User-Id": "990000002",
            "X-ANILA-User-Email": "alice@example.com",
            "X-CSP-Service-Token": "csk-test",
        },
    )
    body = resp.json()
    assert body["user_id"] == "990000002"  # 員編 kept as string, not int-coerced
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


def test_extract_caller_context_keeps_non_numeric_identity(monkeypatch):
    """X-ANILA-User-Id now carries the employee ID (員編) — an opaque
    string. Non-numeric identities (e.g. the admin account) are kept
    verbatim; only blank / whitespace-only degrades to None. (Previously
    the header was int-coerced and non-numeric values became None.)"""
    monkeypatch.setenv("ANILA_CSP_BASE_URL", "http://csp:8000")
    client = TestClient(_make_test_app())
    assert client.get("/echo", headers={"X-ANILA-User-Id": "admin"}).json()["user_id"] == "admin"
    assert client.get("/echo", headers={"X-ANILA-User-Id": "990000002"}).json()["user_id"] == "990000002"
    assert client.get("/echo", headers={"X-ANILA-User-Id": "   "}).json()["user_id"] is None


# ── AgentContext.caller propagation through subagent fork ────────────────────


def test_subagent_inherits_caller_from_parent():
    """A subagent serves the same user as its parent — the fork
    must propagate the immutable caller bundle so the subagent
    can call back into CSP for memory reads on the same user."""
    parent_caller = CallerContext(
        user_id="990000002",
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
