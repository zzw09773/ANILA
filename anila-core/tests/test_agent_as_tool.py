"""Tests for the agent-as-tool wrapper (Sprint 10 PR 5)."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from anila_core.context.agent_context import (
    AgentContext,
    set_current_context,
)
from anila_core.models.tool import ToolSafety
from anila_core.registry.remote_agent_manifest import RemoteAgentManifest
from anila_core.router.tool_router import ToolRegistry
from anila_core.tools.agent_as_tool import _safe_tool_name, make_agent_tool


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


def _manifest(agent_id: str = "drone-specialist") -> RemoteAgentManifest:
    return RemoteAgentManifest(
        agent_id=agent_id,
        name="Drone Specialist",
        description_for_router="Use for drone parameter questions.",
        endpoint_url=f"http://{agent_id}",
    )


# ---------------------------------------------------------------------------
# _safe_tool_name
# ---------------------------------------------------------------------------


def test_safe_tool_name_sanitises_non_alphanumeric() -> None:
    assert _safe_tool_name("agent-a/b.c", "consult_") == "consult_agent_a_b_c"


def test_safe_tool_name_truncates_to_64_chars() -> None:
    name = _safe_tool_name("a" * 100, "consult_")
    assert len(name) == 64


def test_safe_tool_name_handles_empty_prefix() -> None:
    assert _safe_tool_name("agent_x", "") == "agent_x"


def test_safe_tool_name_falls_back_for_pure_garbage() -> None:
    assert _safe_tool_name("///", "p_") == "p_agent"


# ---------------------------------------------------------------------------
# make_agent_tool — definition shape
# ---------------------------------------------------------------------------


def test_tool_uses_manifest_description_by_default() -> None:
    tool = make_agent_tool(
        _manifest(),
        csp_base_url=CSP_BASE,
        csp_api_key="sk-test",
    )
    assert tool.name == "consult_drone_specialist"
    assert tool.description == "Use for drone parameter questions."
    assert tool.safety == ToolSafety.READ_ONLY
    assert tool.input_schema["required"] == ["query"]


def test_custom_description_overrides_manifest() -> None:
    tool = make_agent_tool(
        _manifest(),
        csp_base_url=CSP_BASE,
        csp_api_key="sk-test",
        custom_description="custom desc",
    )
    assert tool.description == "custom desc"


def test_empty_prefix_omitted() -> None:
    tool = make_agent_tool(
        _manifest("agent-a"),
        csp_base_url=CSP_BASE,
        csp_api_key="sk-test",
        name_prefix="",
    )
    assert tool.name == "agent_a"


# ---------------------------------------------------------------------------
# Implementation behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_dispatches_to_csp_with_agent_id_as_model() -> None:
    route = respx.post(CSP_URL).mock(
        return_value=httpx.Response(
            200, json=_agent_response("the answer")
        )
    )
    tool = make_agent_tool(
        _manifest("agent-b"),
        csp_base_url=CSP_BASE,
        csp_api_key="sk-test",
    )
    result = await tool.implementation({"query": "what's X?"})
    assert result == "the answer"
    body = json.loads(route.calls.last.request.read().decode())
    assert body["model"] == "agent-b"
    assert body["messages"][-1] == {"role": "user", "content": "what's X?"}


@pytest.mark.asyncio
@respx.mock
async def test_session_id_falls_through_from_agent_context() -> None:
    route = respx.post(CSP_URL).mock(
        return_value=httpx.Response(200, json=_agent_response())
    )
    ctx = AgentContext(session_id="s-from-ctx")
    set_current_context(ctx)
    tool = make_agent_tool(
        _manifest(),
        csp_base_url=CSP_BASE,
        csp_api_key="sk-test",
    )
    await tool.implementation({"query": "x"})
    body = json.loads(route.calls.last.request.read().decode())
    assert body["anila_session_id"] == "s-from-ctx"


@pytest.mark.asyncio
@respx.mock
async def test_explicit_session_id_overrides_agent_context() -> None:
    route = respx.post(CSP_URL).mock(
        return_value=httpx.Response(200, json=_agent_response())
    )
    ctx = AgentContext(session_id="s-from-ctx")
    set_current_context(ctx)
    tool = make_agent_tool(
        _manifest(),
        csp_base_url=CSP_BASE,
        csp_api_key="sk-test",
        session_id="s-explicit",
    )
    await tool.implementation({"query": "x"})
    body = json.loads(route.calls.last.request.read().decode())
    assert body["anila_session_id"] == "s-explicit"


@pytest.mark.asyncio
@respx.mock
async def test_system_prompt_override_in_input() -> None:
    route = respx.post(CSP_URL).mock(
        return_value=httpx.Response(200, json=_agent_response())
    )
    tool = make_agent_tool(
        _manifest(),
        csp_base_url=CSP_BASE,
        csp_api_key="sk-test",
    )
    await tool.implementation(
        {"query": "x", "system_prompt": "you are concise"}
    )
    body = json.loads(route.calls.last.request.read().decode())
    assert body["messages"][0] == {
        "role": "system",
        "content": "you are concise",
    }


@pytest.mark.asyncio
async def test_blank_query_returns_error_string() -> None:
    tool = make_agent_tool(
        _manifest(),
        csp_base_url=CSP_BASE,
        csp_api_key="sk-test",
    )
    result = await tool.implementation({"query": "  "})
    assert "query" in result and "required" in result


@pytest.mark.asyncio
@respx.mock
async def test_dispatch_failure_returns_friendly_error_string() -> None:
    respx.post(CSP_URL).mock(
        return_value=httpx.Response(500, json={"detail": "boom"})
    )
    tool = make_agent_tool(
        _manifest("flaky-agent"),
        csp_base_url=CSP_BASE,
        csp_api_key="sk-test",
    )
    result = await tool.implementation({"query": "x"})
    # Must not raise — returns a string the model can read.
    assert "flaky-agent" in result
    assert "error" in result.lower()


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_registry_can_register_make_agent_tool_output() -> None:
    registry = ToolRegistry()
    registry.register(
        make_agent_tool(
            _manifest("agent-x"),
            csp_base_url=CSP_BASE,
            csp_api_key="sk-test",
        )
    )
    assert "consult_agent_x" in registry.list_tools()
