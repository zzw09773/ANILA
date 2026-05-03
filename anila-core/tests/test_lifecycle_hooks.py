"""Tests for QueryEngine lifecycle hooks (Sprint 11 PR 1)."""

from __future__ import annotations

from typing import Any

import pytest

from anila_core.engine.lifecycle import RunHooks, _safe_call
from anila_core.engine.query_engine import QueryConfig, QueryEngine
from anila_core.memory import MemorySession
from anila_core.models.handoff import HandoffRequest
from anila_core.models.interrupt import InterruptItem
from anila_core.models.message import ToolCall, UserMessage
from anila_core.models.tool import ToolDefinition, ToolSafety
from anila_core.providers.mock import (
    MockProvider,
    ScriptedResponse,
    ScriptedToolCall,
)
from anila_core.router.tool_router import ToolRegistry


# ---------------------------------------------------------------------------
# Recording hook implementation
# ---------------------------------------------------------------------------


class RecordingHooks(RunHooks):
    """Captures every hook call with its kwargs for assertion."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def on_run_start(self, *, agent_id, session_id):
        self.events.append(("run_start", {"agent_id": agent_id, "session_id": session_id}))

    async def on_run_end(self, *, agent_id, session_id, result):
        self.events.append(("run_end", {"agent_id": agent_id, "session_id": session_id, "stop_reason": result.stop_reason}))

    async def on_agent_start(self, *, agent_id, session_id):
        self.events.append(("agent_start", {"agent_id": agent_id, "session_id": session_id}))

    async def on_agent_end(self, *, agent_id, session_id, result):
        self.events.append(("agent_end", {"agent_id": agent_id, "session_id": session_id}))

    async def on_tool_start(self, *, agent_id, session_id, call):
        self.events.append(("tool_start", {"call_id": call.id, "name": call.name}))

    async def on_tool_end(self, *, agent_id, session_id, call, result):
        self.events.append(("tool_end", {"call_id": call.id, "name": call.name, "is_error": result.is_error}))

    async def on_run_paused(self, *, agent_id, session_id, interrupt_id, kind):
        self.events.append(("run_paused", {"interrupt_id": interrupt_id, "kind": kind}))

    async def on_run_resumed(self, *, agent_id, session_id, interrupt_id):
        self.events.append(("run_resumed", {"interrupt_id": interrupt_id}))

    async def on_handoff(self, *, source_agent_id, session_id, request):
        self.events.append(("handoff", {"target": request.target_agent_id, "id": request.id}))


# ---------------------------------------------------------------------------
# RunHooks base class — every method is a no-op
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_base_run_hooks_methods_are_no_ops() -> None:
    hooks = RunHooks()
    # All return None and accept the documented kwargs without raising.
    assert await hooks.on_run_start(agent_id="a", session_id="s") is None
    assert (
        await hooks.on_tool_start(
            agent_id="a", session_id="s",
            call=ToolCall(name="x", input={}),
        )
        is None
    )


# ---------------------------------------------------------------------------
# _safe_call swallows exceptions
# ---------------------------------------------------------------------------


class BoomHooks(RunHooks):
    async def on_run_start(self, *, agent_id, session_id):
        raise RuntimeError("boom")


@pytest.mark.asyncio
async def test_safe_call_swallows_hook_exceptions() -> None:
    # Must not raise.
    await _safe_call(BoomHooks(), "on_run_start", agent_id="a", session_id="s")


@pytest.mark.asyncio
async def test_safe_call_with_none_hooks_is_a_noop() -> None:
    await _safe_call(None, "on_run_start", agent_id="a", session_id="s")


# ---------------------------------------------------------------------------
# Successful run firing order
# ---------------------------------------------------------------------------


def _make_echo_tool() -> ToolDefinition:
    async def impl(input, **_):
        return f"echo:{input.get('text', '')}"

    return ToolDefinition(
        name="echo",
        description="echo",
        input_schema={"type": "object"},
        safety=ToolSafety.READ_ONLY,
        implementation=impl,
    )


@pytest.mark.asyncio
async def test_successful_run_fires_full_lifecycle_chain() -> None:
    hooks = RecordingHooks()
    provider = MockProvider(
        [
            ScriptedResponse(
                tool_calls=[
                    ScriptedToolCall(name="echo", input={"text": "hi"}, tool_id="c1")
                ],
                finish_reason="tool_use",
            ),
            ScriptedResponse(text="all done", finish_reason="end_turn"),
        ]
    )
    registry = ToolRegistry()
    registry.register(_make_echo_tool())
    engine = QueryEngine(
        provider, registry,
        QueryConfig(model="m", agent_id="agent-test"),
        hooks=hooks,
    )

    result = await engine.run([UserMessage(content="hello")])
    assert result.stop_reason == "completed"
    names = [name for name, _ in hooks.events]
    # Must contain these in this relative order.
    assert names.index("run_start") < names.index("agent_start")
    assert names.index("agent_start") < names.index("tool_start")
    assert names.index("tool_start") < names.index("tool_end")
    assert names.index("tool_end") < names.index("agent_end")
    assert names.index("agent_end") < names.index("run_end")


@pytest.mark.asyncio
async def test_tool_start_end_match_per_call_id() -> None:
    hooks = RecordingHooks()
    provider = MockProvider(
        [
            ScriptedResponse(
                tool_calls=[
                    ScriptedToolCall(name="echo", input={"text": "a"}, tool_id="c-a"),
                    ScriptedToolCall(name="echo", input={"text": "b"}, tool_id="c-b"),
                ],
                finish_reason="tool_use",
            ),
            ScriptedResponse(text="done", finish_reason="end_turn"),
        ]
    )
    registry = ToolRegistry()
    registry.register(_make_echo_tool())
    engine = QueryEngine(
        provider, registry,
        QueryConfig(model="m", agent_id="agent-x"),
        hooks=hooks,
    )
    await engine.run([UserMessage(content="hi")])

    starts = [e for n, e in hooks.events if n == "tool_start"]
    ends = [e for n, e in hooks.events if n == "tool_end"]
    assert sorted(s["call_id"] for s in starts) == ["c-a", "c-b"]
    assert sorted(e["call_id"] for e in ends) == ["c-a", "c-b"]


@pytest.mark.asyncio
async def test_agent_id_propagates_to_hooks() -> None:
    hooks = RecordingHooks()
    provider = MockProvider(
        [ScriptedResponse(text="ok", finish_reason="end_turn")]
    )
    engine = QueryEngine(
        provider, ToolRegistry(),
        QueryConfig(model="m", agent_id="my-agent"),
        hooks=hooks,
    )
    await engine.run([UserMessage(content="hi")])
    [run_start_event] = [e for n, e in hooks.events if n == "run_start"]
    assert run_start_event["agent_id"] == "my-agent"


# ---------------------------------------------------------------------------
# Pause / resume hooks
# ---------------------------------------------------------------------------


def _make_ask_tool(interrupt_id: str = "int-1") -> ToolDefinition:
    async def impl(input, **_):
        return InterruptItem(id=interrupt_id, kind="ask_user", payload={})
    return ToolDefinition(
        name="ask_user",
        description="ask",
        input_schema={"type": "object"},
        safety=ToolSafety.READ_ONLY,
        implementation=impl,
    )


@pytest.mark.asyncio
async def test_pause_fires_run_paused_hook() -> None:
    hooks = RecordingHooks()
    sess = MemorySession("s1")
    provider = MockProvider(
        [
            ScriptedResponse(
                tool_calls=[
                    ScriptedToolCall(name="ask_user", input={}, tool_id="c-ask")
                ],
                finish_reason="tool_use",
            )
        ]
    )
    registry = ToolRegistry()
    registry.register(_make_ask_tool(interrupt_id="int-A"))
    engine = QueryEngine(
        provider, registry,
        QueryConfig(model="m", agent_id="agent-x"),
        session=sess, hooks=hooks,
    )
    from anila_core.engine.approvals import RunPaused

    with pytest.raises(RunPaused):
        await engine.run([UserMessage(content="hi")])

    paused_events = [e for n, e in hooks.events if n == "run_paused"]
    assert paused_events == [{"interrupt_id": "int-A", "kind": "ask_user"}]


@pytest.mark.asyncio
async def test_resume_fires_run_resumed_hook() -> None:
    hooks = RecordingHooks()
    sess = MemorySession("s1")
    provider = MockProvider(
        [
            ScriptedResponse(
                tool_calls=[
                    ScriptedToolCall(name="ask_user", input={}, tool_id="c-ask")
                ],
                finish_reason="tool_use",
            ),
            ScriptedResponse(text="resumed", finish_reason="end_turn"),
        ]
    )
    registry = ToolRegistry()
    registry.register(_make_ask_tool(interrupt_id="int-R"))
    engine = QueryEngine(
        provider, registry,
        QueryConfig(model="m", agent_id="agent-x"),
        session=sess, hooks=hooks,
    )
    from anila_core.engine.approvals import RunPaused

    with pytest.raises(RunPaused):
        await engine.run([UserMessage(content="hi")])

    await engine.resume_from_interrupt("int-R", {"selected": ["ok"]})

    resumed = [e for n, e in hooks.events if n == "run_resumed"]
    assert resumed == [{"interrupt_id": "int-R"}]


# ---------------------------------------------------------------------------
# Handoff hook
# ---------------------------------------------------------------------------


def _make_handoff_tool() -> ToolDefinition:
    async def impl(input, **_):
        return HandoffRequest(
            id="hand-1",
            target_agent_id=input.get("target", "agent-b"),
            message=input.get("message", "please continue"),
        )
    return ToolDefinition(
        name="handoff_to",
        description="handoff",
        input_schema={"type": "object"},
        safety=ToolSafety.READ_ONLY,
        implementation=impl,
    )


@pytest.mark.asyncio
async def test_handoff_fires_handoff_hook() -> None:
    hooks = RecordingHooks()
    sess = MemorySession("s1")
    provider = MockProvider(
        [
            ScriptedResponse(
                tool_calls=[
                    ScriptedToolCall(
                        name="handoff_to",
                        input={"target": "agent-b", "message": "x"},
                        tool_id="c-h",
                    )
                ],
                finish_reason="tool_use",
            )
        ]
    )
    registry = ToolRegistry()
    registry.register(_make_handoff_tool())
    engine = QueryEngine(
        provider, registry,
        QueryConfig(model="m", agent_id="agent-a"),
        session=sess, hooks=hooks,
    )
    from anila_core.engine.handoff import RunHandoff

    with pytest.raises(RunHandoff):
        await engine.run([UserMessage(content="hi")])

    handoffs = [e for n, e in hooks.events if n == "handoff"]
    assert handoffs == [{"target": "agent-b", "id": "hand-1"}]


# ---------------------------------------------------------------------------
# Hook errors don't abort the run
# ---------------------------------------------------------------------------


class FailingToolEnd(RunHooks):
    async def on_tool_end(self, *, agent_id, session_id, call, result):
        raise RuntimeError("hook crash")


@pytest.mark.asyncio
async def test_hook_exception_does_not_abort_run() -> None:
    provider = MockProvider(
        [
            ScriptedResponse(
                tool_calls=[
                    ScriptedToolCall(name="echo", input={"text": "x"}, tool_id="c1")
                ],
                finish_reason="tool_use",
            ),
            ScriptedResponse(text="completed", finish_reason="end_turn"),
        ]
    )
    registry = ToolRegistry()
    registry.register(_make_echo_tool())
    engine = QueryEngine(
        provider, registry,
        QueryConfig(model="m", agent_id="x"),
        hooks=FailingToolEnd(),
    )
    result = await engine.run([UserMessage(content="hi")])
    # Despite the exploding hook, the run completed.
    assert result.stop_reason == "completed"


# ---------------------------------------------------------------------------
# No hooks (default) — existing engine behaviour intact
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_no_hooks_path_unchanged() -> None:
    provider = MockProvider(
        [ScriptedResponse(text="ok", finish_reason="end_turn")]
    )
    engine = QueryEngine(
        provider, ToolRegistry(),
        QueryConfig(model="m"),
    )  # hooks=None
    result = await engine.run([UserMessage(content="hi")])
    assert result.stop_reason == "completed"
