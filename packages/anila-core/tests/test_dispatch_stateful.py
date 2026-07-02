"""Tests for stateful dispatch_tool (Sprint 10 PR 2).

Uses respx to mock the HTTP layer and verify the request payload shape
matches the design:

- ``messages = [system?, *context_messages, {role:user, content:query}]``
- ``anila_session_id`` embedded when session_id is passed
- ``anila_handoff`` embedded when handoff_meta is passed
- ``dispatch_for_handoff`` correctly unpacks a HandoffRequest
"""

from __future__ import annotations

import httpx
import pytest
import respx

from anila_core.models.handoff import HandoffRequest
from anila_core.tools.dispatch_tool import (
    dispatch_for_handoff,
    dispatch_to_agent,
    dispatch_to_agent_response,
)


CSP_BASE = "http://csp.test"
CSP_URL = f"{CSP_BASE}/v1/chat/completions"


def _agent_response(content: str = "ok") -> dict:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 0,
        "model": "agent-x",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
    }


# ---------------------------------------------------------------------------
# Backwards-compat — current callers (Router) keep working
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_dispatch_unchanged_when_no_extras_passed() -> None:
    route = respx.post(CSP_URL).mock(
        return_value=httpx.Response(200, json=_agent_response("hi"))
    )
    result = await dispatch_to_agent(
        agent_id="agent-x",
        query="say hi",
        csp_base_url=CSP_BASE,
        csp_api_key="sk-test",
    )
    assert result == "hi"
    sent = route.calls.last.request.read().decode()
    import json as _json

    body = _json.loads(sent)
    assert body["model"] == "agent-x"
    assert body["messages"] == [{"role": "user", "content": "say hi"}]
    assert "anila_session_id" not in body
    assert "anila_handoff" not in body


# ---------------------------------------------------------------------------
# context_messages
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_context_messages_inserted_before_query() -> None:
    route = respx.post(CSP_URL).mock(
        return_value=httpx.Response(200, json=_agent_response())
    )
    await dispatch_to_agent_response(
        agent_id="agent-x",
        query="continue please",
        csp_base_url=CSP_BASE,
        csp_api_key="sk-test",
        context_messages=[
            {"role": "user", "content": "earlier"},
            {"role": "assistant", "content": "earlier reply"},
        ],
    )
    import json as _json

    body = _json.loads(route.calls.last.request.read().decode())
    assert body["messages"] == [
        {"role": "user", "content": "earlier"},
        {"role": "assistant", "content": "earlier reply"},
        {"role": "user", "content": "continue please"},
    ]


@pytest.mark.asyncio
@respx.mock
async def test_system_prompt_then_context_then_query() -> None:
    route = respx.post(CSP_URL).mock(
        return_value=httpx.Response(200, json=_agent_response())
    )
    await dispatch_to_agent_response(
        agent_id="agent-x",
        query="now",
        csp_base_url=CSP_BASE,
        csp_api_key="sk-test",
        system_prompt="be concise",
        context_messages=[{"role": "user", "content": "before"}],
    )
    import json as _json

    body = _json.loads(route.calls.last.request.read().decode())
    assert body["messages"] == [
        {"role": "system", "content": "be concise"},
        {"role": "user", "content": "before"},
        {"role": "user", "content": "now"},
    ]


# ---------------------------------------------------------------------------
# session_id + handoff_meta extension fields
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_session_id_embedded_as_extension_field() -> None:
    route = respx.post(CSP_URL).mock(
        return_value=httpx.Response(200, json=_agent_response())
    )
    await dispatch_to_agent_response(
        agent_id="agent-x",
        query="x",
        csp_base_url=CSP_BASE,
        csp_api_key="sk-test",
        session_id="s-abc",
    )
    import json as _json

    body = _json.loads(route.calls.last.request.read().decode())
    assert body["anila_session_id"] == "s-abc"


@pytest.mark.asyncio
@respx.mock
async def test_handoff_meta_embedded_as_extension_field() -> None:
    route = respx.post(CSP_URL).mock(
        return_value=httpx.Response(200, json=_agent_response())
    )
    meta = {"handoff_id": "hand-1", "reason": "specialist needed"}
    await dispatch_to_agent_response(
        agent_id="agent-x",
        query="x",
        csp_base_url=CSP_BASE,
        csp_api_key="sk-test",
        handoff_meta=meta,
    )
    import json as _json

    body = _json.loads(route.calls.last.request.read().decode())
    assert body["anila_handoff"] == meta


# ---------------------------------------------------------------------------
# dispatch_for_handoff convenience
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_dispatch_for_handoff_unpacks_request() -> None:
    route = respx.post(CSP_URL).mock(
        return_value=httpx.Response(200, json=_agent_response())
    )
    req = HandoffRequest(
        id="hand-7",
        target_agent_id="agent-b",
        message="please continue",
        context_messages=[{"role": "user", "content": "history"}],
        reason="needs deeper expertise",
        metadata={"trace_id": "t-1"},
    )
    await dispatch_for_handoff(
        req,
        csp_base_url=CSP_BASE,
        csp_api_key="sk-test",
        session_id="s-shared",
    )
    import json as _json

    body = _json.loads(route.calls.last.request.read().decode())
    assert body["model"] == "agent-b"
    # context messages preserved + the handoff message added as user.
    assert body["messages"] == [
        {"role": "user", "content": "history"},
        {"role": "user", "content": "please continue"},
    ]
    assert body["anila_session_id"] == "s-shared"
    assert body["anila_handoff"]["handoff_id"] == "hand-7"
    assert body["anila_handoff"]["reason"] == "needs deeper expertise"
    assert body["anila_handoff"]["metadata"] == {"trace_id": "t-1"}


@pytest.mark.asyncio
@respx.mock
async def test_dispatch_for_handoff_omits_reason_when_none() -> None:
    route = respx.post(CSP_URL).mock(
        return_value=httpx.Response(200, json=_agent_response())
    )
    req = HandoffRequest(
        id="hand-8", target_agent_id="agent-b", message="x"
    )
    await dispatch_for_handoff(
        req,
        csp_base_url=CSP_BASE,
        csp_api_key="sk-test",
    )
    import json as _json

    body = _json.loads(route.calls.last.request.read().decode())
    assert "reason" not in body["anila_handoff"]
    assert body["anila_handoff"]["metadata"] == {}
    # No session_id passed → field omitted entirely.
    assert "anila_session_id" not in body


# ---------------------------------------------------------------------------
# Auth header still present
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_authorization_header_passed_through() -> None:
    route = respx.post(CSP_URL).mock(
        return_value=httpx.Response(200, json=_agent_response())
    )
    await dispatch_to_agent_response(
        agent_id="agent-x",
        query="x",
        csp_base_url=CSP_BASE,
        csp_api_key="sk-secret-123",
    )
    auth = route.calls.last.request.headers.get("authorization")
    assert auth == "Bearer sk-secret-123"
