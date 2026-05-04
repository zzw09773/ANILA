"""Tests for the AgenticRAG-vendored user-memory client.

The client is decoupled from anila-core (per the route-3 ADR), so
these tests assert the wire contract directly:

* ``extract_caller_context`` parses the X-ANILA-* / X-CSP-* headers
* ``fetch_user_facts`` issues the right HTTP call and degrades to
  ``[]`` on every failure mode
* ``format_user_facts_block`` renders facts the way the chat
  handler prepends to the agent's system prompt

The fetch tests use respx to stub the CSP backend at
``GET /api/memory/users/{user_id}/facts`` so they run without a
live CSP container.
"""
from __future__ import annotations

import httpx
import pytest
import respx
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from agentic_rag.runtime.user_memory import (
    AgenticRagCallerContext,
    UserFact,
    extract_caller_context,
    fetch_user_facts,
    format_user_facts_block,
)


# ── AgenticRagCallerContext.can_read_user_memory ─────────────────────────────


def test_can_read_user_memory_requires_all_three_fields():
    """Pin the gate so a refactor that quietly drops one field
    doesn't make ``fetch_user_facts`` start firing requests with
    incomplete state and surface a confusing 401.
    """
    full = AgenticRagCallerContext(
        user_id=1, service_token="csk-x", csp_base_url="http://csp:8000"
    )
    assert full.can_read_user_memory is True

    assert AgenticRagCallerContext(user_id=1, service_token="csk-x").can_read_user_memory is False
    assert AgenticRagCallerContext(user_id=1, csp_base_url="http://csp:8000").can_read_user_memory is False
    assert AgenticRagCallerContext(service_token="csk-x", csp_base_url="http://csp:8000").can_read_user_memory is False


# ── extract_caller_context FastAPI dependency ────────────────────────────────


def _make_test_app() -> FastAPI:
    app = FastAPI()

    @app.get("/echo")
    def echo(
        caller: AgenticRagCallerContext = Depends(extract_caller_context),
    ) -> dict:
        return {
            "user_id": caller.user_id,
            "service_token": caller.service_token,
            "csp_base_url": caller.csp_base_url,
            "can_read": caller.can_read_user_memory,
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
    assert body["service_token"] == "csk-test"
    assert body["csp_base_url"] == "http://csp:8000"
    assert body["can_read"] is True


def test_extract_caller_context_tolerates_missing_headers(monkeypatch):
    monkeypatch.delenv("ANILA_CSP_BASE_URL", raising=False)
    client = TestClient(_make_test_app())
    resp = client.get("/echo")
    body = resp.json()
    assert body["user_id"] is None
    assert body["service_token"] is None
    assert body["can_read"] is False


def test_extract_caller_context_treats_garbage_user_id_as_none(monkeypatch):
    """A malformed header shouldn't crash the request — agent
    code degrades to "no user attribution" rather than 500."""
    monkeypatch.setenv("ANILA_CSP_BASE_URL", "http://csp:8000")
    client = TestClient(_make_test_app())
    resp = client.get("/echo", headers={"X-ANILA-User-Id": "not-an-int"})
    assert resp.json()["user_id"] is None


# ── fetch_user_facts ─────────────────────────────────────────────────────────


@pytest.fixture
def full_caller() -> AgenticRagCallerContext:
    return AgenticRagCallerContext(
        user_id=42,
        service_token="csk-test",
        csp_base_url="http://csp:8000",
    )


@pytest.mark.asyncio
async def test_fetch_user_facts_returns_empty_when_caller_incomplete():
    """Missing fields → ``[]`` without firing a request. Pinned so
    a refactor that flips the order of checks doesn't accidentally
    leak partial state to the wire.
    """
    incomplete = AgenticRagCallerContext(user_id=42)
    facts = await fetch_user_facts(incomplete)
    assert facts == []


@pytest.mark.asyncio
@respx.mock
async def test_fetch_user_facts_happy_path(full_caller):
    payload = {
        "total": 2,
        "facts": [
            {"key": "name", "value": "Sara", "confidence": 1.0},
            {"key": "role", "value": "engineer", "confidence": 0.9},
        ],
    }
    respx.get("http://csp:8000/api/memory/users/42/facts").mock(
        return_value=httpx.Response(200, json=payload)
    )

    facts = await fetch_user_facts(full_caller)
    assert [f.key for f in facts] == ["name", "role"]
    assert facts[0].value == "Sara"
    assert facts[1].confidence == 0.9


@pytest.mark.asyncio
@respx.mock
async def test_fetch_user_facts_returns_empty_on_401(full_caller):
    """401 (bad token) → ``[]``. The agent should keep serving the
    user without memory rather than 5xx the entire chat request."""
    respx.get("http://csp:8000/api/memory/users/42/facts").mock(
        return_value=httpx.Response(401, json={"detail": "bad token"})
    )
    assert await fetch_user_facts(full_caller) == []


@pytest.mark.asyncio
@respx.mock
async def test_fetch_user_facts_returns_empty_on_network_error(full_caller):
    """Connect errors absorbed silently — same degradation as 401."""
    respx.get("http://csp:8000/api/memory/users/42/facts").mock(
        side_effect=httpx.ConnectError("CSP unreachable")
    )
    assert await fetch_user_facts(full_caller) == []


@pytest.mark.asyncio
@respx.mock
async def test_fetch_user_facts_drops_malformed_items(full_caller):
    """Half-formed items in the payload don't poison the whole
    list — drop the broken ones and return the valid remainder."""
    payload = {
        "facts": [
            {"key": "ok", "value": "v"},
            {"key": "missing-value"},
            {"value": "missing-key"},
            "not-even-a-dict",
            {"key": "ok2", "value": "v2", "confidence": "not-a-float"},  # confidence fallback
        ],
    }
    respx.get("http://csp:8000/api/memory/users/42/facts").mock(
        return_value=httpx.Response(200, json=payload)
    )
    facts = await fetch_user_facts(full_caller)
    assert [f.key for f in facts] == ["ok", "ok2"]
    assert facts[1].confidence == 1.0  # bad float fell back to 1.0


# ── format_user_facts_block ──────────────────────────────────────────────────


def test_format_user_facts_block_returns_none_for_empty_list():
    """No facts → no block. Lets the chat handler write
    ``return enriched or base_prompt`` without an extra check."""
    assert format_user_facts_block([]) is None


def test_format_user_facts_block_renders_markdown_with_guidance_footer():
    facts = [
        UserFact(key="name", value="Sara"),
        UserFact(key="role", value="engineer"),
    ]
    block = format_user_facts_block(facts)
    assert block is not None
    assert "## 使用者背景" in block
    assert "**name**: Sara" in block
    assert "**role**: engineer" in block
    # Guidance footer instructing the model not to regurgitate.
    assert "矛盾" in block and "本次對話為準" in block
