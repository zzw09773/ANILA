"""Tests for hierarchical tracing (Sprint 11 PR 2)."""

from __future__ import annotations

import pytest

from anila_core.engine.query_engine import QueryConfig, QueryEngine
from anila_core.memory import MemorySession
from anila_core.models.handoff import HandoffRequest
from anila_core.models.interrupt import InterruptItem
from anila_core.models.message import UserMessage
from anila_core.models.tool import ToolDefinition, ToolSafety
from anila_core.providers.mock import (
    MockProvider,
    ScriptedResponse,
    ScriptedToolCall,
)
from anila_core.router.tool_router import ToolRegistry
from anila_core.tracing import (
    InMemoryProcessor,
    Span,
    SpanKind,
    SpanStatus,
    Tracer,
    TracingHooks,
)


# ---------------------------------------------------------------------------
# Span model
# ---------------------------------------------------------------------------


def test_span_auto_ids_unique() -> None:
    seen = {Span(name="x").span_id for _ in range(50)}
    assert len(seen) == 50
    seen_traces = {Span(name="x").trace_id for _ in range(50)}
    assert len(seen_traces) == 50


def test_span_end_sets_duration_and_status() -> None:
    sp = Span(name="t")
    sp.end()
    assert sp.duration_ms is not None and sp.duration_ms >= 0
    assert sp.status == SpanStatus.OK


def test_span_end_is_idempotent() -> None:
    sp = Span(name="t")
    sp.end()
    first = sp.end_time
    sp.end()
    assert sp.end_time == first


def test_span_set_status_error_carries_message() -> None:
    sp = Span(name="t")
    sp.set_status(SpanStatus.ERROR, "kaboom")
    assert sp.error == "kaboom"
    assert sp.status == SpanStatus.ERROR


def test_span_to_dict_round_trip_keys() -> None:
    sp = Span(name="t", kind=SpanKind.TOOL)
    sp.add_event("retried", attempt=2)
    sp.set_attribute("k", "v")
    sp.end()
    d = sp.to_dict()
    assert {
        "span_id", "trace_id", "parent_id", "name", "kind",
        "status", "error", "start_ts", "end_ts", "duration_ms",
        "attributes", "events",
    } <= d.keys()
    assert d["attributes"] == {"k": "v"}
    assert d["events"][0]["name"] == "retried"


# ---------------------------------------------------------------------------
# Tracer + InMemoryProcessor
# ---------------------------------------------------------------------------


def test_tracer_synchronous_span_context_manager() -> None:
    proc = InMemoryProcessor()
    tracer = Tracer(processors=[proc])
    with tracer.span("outer", kind=SpanKind.RUN):
        with tracer.span("inner", kind=SpanKind.TOOL):
            pass
    spans = proc.spans
    assert [s.name for s in spans] == ["inner", "outer"]
    inner, outer = spans
    assert inner.parent_id == outer.span_id
    assert inner.trace_id == outer.trace_id
    assert outer.parent_id is None


@pytest.mark.asyncio
async def test_tracer_async_span_context_manager() -> None:
    proc = InMemoryProcessor()
    tracer = Tracer(processors=[proc])
    async with tracer.async_span("outer"):
        async with tracer.async_span("inner"):
            pass
    assert [s.name for s in proc.spans] == ["inner", "outer"]


def test_tracer_marks_error_on_exception() -> None:
    proc = InMemoryProcessor()
    tracer = Tracer(processors=[proc])
    with pytest.raises(RuntimeError):
        with tracer.span("boom"):
            raise RuntimeError("oops")
    [sp] = proc.spans
    assert sp.status == SpanStatus.ERROR
    assert "oops" in (sp.error or "")


def test_in_memory_processor_to_tree_builds_parent_child() -> None:
    proc = InMemoryProcessor()
    tracer = Tracer(processors=[proc])
    with tracer.span("root"):
        with tracer.span("a"):
            with tracer.span("a.1"):
                pass
        with tracer.span("b"):
            pass
    tree = proc.to_tree()
    assert len(tree) == 1
    root = tree[0]
    assert root["name"] == "root"
    assert [c["name"] for c in root["children"]] == ["a", "b"]
    [a_node] = [c for c in root["children"] if c["name"] == "a"]
    assert [c["name"] for c in a_node["children"]] == ["a.1"]


def test_in_memory_processor_clear() -> None:
    proc = InMemoryProcessor()
    tracer = Tracer(processors=[proc])
    with tracer.span("x"):
        pass
    assert len(proc.spans) == 1
    proc.clear()
    assert proc.spans == []


def test_processor_exceptions_dont_break_trace() -> None:
    class BoomProcessor:
        def on_end(self, span):
            raise RuntimeError("processor down")

    tracer = Tracer(processors=[BoomProcessor()])
    # Must not raise.
    with tracer.span("ok"):
        pass


# ---------------------------------------------------------------------------
# TracingHooks integration with QueryEngine
# ---------------------------------------------------------------------------


def _echo_tool() -> ToolDefinition:
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
async def test_tracing_hooks_emit_run_agent_tool_spans() -> None:
    proc = InMemoryProcessor()
    tracer = Tracer(processors=[proc])
    hooks = TracingHooks(tracer)
    provider = MockProvider(
        [
            ScriptedResponse(
                tool_calls=[
                    ScriptedToolCall(
                        name="echo", input={"text": "x"}, tool_id="c1"
                    )
                ],
                finish_reason="tool_use",
            ),
            ScriptedResponse(text="done", finish_reason="end_turn"),
        ]
    )
    registry = ToolRegistry()
    registry.register(_echo_tool())
    engine = QueryEngine(
        provider, registry,
        QueryConfig(model="m", agent_id="agent-x"),
        hooks=hooks,
    )
    await engine.run([UserMessage(content="hi")])

    names = sorted(s.name for s in proc.spans)
    assert "agent:agent-x" in names
    assert "run:agent-x" in names
    assert "tool:echo" in names

    # Tool span must be a child of the agent span.
    tool_sp = next(s for s in proc.spans if s.name == "tool:echo")
    agent_sp = next(s for s in proc.spans if s.name == "agent:agent-x")
    run_sp = next(s for s in proc.spans if s.name == "run:agent-x")
    assert tool_sp.parent_id == agent_sp.span_id
    assert agent_sp.parent_id == run_sp.span_id
    # All share the same trace_id.
    assert tool_sp.trace_id == agent_sp.trace_id == run_sp.trace_id


@pytest.mark.asyncio
async def test_tracing_hooks_record_run_attributes() -> None:
    proc = InMemoryProcessor()
    tracer = Tracer(processors=[proc])
    provider = MockProvider(
        [ScriptedResponse(text="ok", finish_reason="end_turn")]
    )
    engine = QueryEngine(
        provider, ToolRegistry(),
        QueryConfig(model="m", agent_id="agent-y"),
        hooks=TracingHooks(tracer),
    )
    await engine.run([UserMessage(content="hi")])
    run_sp = next(s for s in proc.spans if s.name == "run:agent-y")
    assert run_sp.attributes["stop_reason"] == "completed"
    assert run_sp.attributes["turn_count"] == 1


@pytest.mark.asyncio
async def test_tracing_hooks_emit_interrupt_span_on_pause() -> None:
    proc = InMemoryProcessor()
    tracer = Tracer(processors=[proc])

    async def ask_impl(input, **_):
        return InterruptItem(id="int-x", kind="ask_user", payload={})

    ask_tool = ToolDefinition(
        name="ask_user",
        description="ask",
        input_schema={"type": "object"},
        safety=ToolSafety.READ_ONLY,
        implementation=ask_impl,
    )
    sess = MemorySession("s1")
    provider = MockProvider(
        [
            ScriptedResponse(
                tool_calls=[
                    ScriptedToolCall(name="ask_user", input={}, tool_id="ca")
                ],
                finish_reason="tool_use",
            )
        ]
    )
    registry = ToolRegistry()
    registry.register(ask_tool)
    engine = QueryEngine(
        provider, registry,
        QueryConfig(model="m", agent_id="agent-z"),
        session=sess, hooks=TracingHooks(tracer),
    )
    from anila_core.engine.approvals import RunPaused

    with pytest.raises(RunPaused):
        await engine.run([UserMessage(content="hi")])

    interrupt_spans = [s for s in proc.spans if s.kind == SpanKind.INTERRUPT]
    assert len(interrupt_spans) == 1
    sp = interrupt_spans[0]
    assert sp.attributes["interrupt_id"] == "int-x"
    assert sp.attributes["interrupt_kind"] == "ask_user"


@pytest.mark.asyncio
async def test_tracing_hooks_emit_handoff_span() -> None:
    proc = InMemoryProcessor()
    tracer = Tracer(processors=[proc])

    async def handoff_impl(input, **_):
        return HandoffRequest(
            id="hand-1",
            target_agent_id="agent-b",
            message="please continue",
            reason="needs B",
        )

    handoff_tool = ToolDefinition(
        name="handoff_to",
        description="handoff",
        input_schema={"type": "object"},
        safety=ToolSafety.READ_ONLY,
        implementation=handoff_impl,
    )
    sess = MemorySession("s1")
    provider = MockProvider(
        [
            ScriptedResponse(
                tool_calls=[
                    ScriptedToolCall(name="handoff_to", input={}, tool_id="ch")
                ],
                finish_reason="tool_use",
            )
        ]
    )
    registry = ToolRegistry()
    registry.register(handoff_tool)
    engine = QueryEngine(
        provider, registry,
        QueryConfig(model="m", agent_id="agent-a"),
        session=sess, hooks=TracingHooks(tracer),
    )
    from anila_core.engine.handoff import RunHandoff

    with pytest.raises(RunHandoff):
        await engine.run([UserMessage(content="hi")])

    handoff_spans = [s for s in proc.spans if s.kind == SpanKind.HANDOFF]
    assert len(handoff_spans) == 1
    sp = handoff_spans[0]
    assert sp.attributes["target_agent"] == "agent-b"
    assert sp.attributes["source_agent"] == "agent-a"
    assert sp.attributes["reason"] == "needs B"


@pytest.mark.asyncio
async def test_tool_error_marks_tool_span_error() -> None:
    proc = InMemoryProcessor()
    tracer = Tracer(processors=[proc])

    async def boom_impl(input, **_):
        raise RuntimeError("tool boom")

    boom_tool = ToolDefinition(
        name="boom",
        description="boom",
        input_schema={"type": "object"},
        safety=ToolSafety.READ_ONLY,
        implementation=boom_impl,
    )
    provider = MockProvider(
        [
            ScriptedResponse(
                tool_calls=[
                    ScriptedToolCall(name="boom", input={}, tool_id="cb")
                ],
                finish_reason="tool_use",
            ),
            ScriptedResponse(text="ok", finish_reason="end_turn"),
        ]
    )
    registry = ToolRegistry()
    registry.register(boom_tool)
    engine = QueryEngine(
        provider, registry,
        QueryConfig(model="m", agent_id="agent-x"),
        hooks=TracingHooks(tracer),
    )
    await engine.run([UserMessage(content="hi")])

    tool_sp = next(s for s in proc.spans if s.name == "tool:boom")
    assert tool_sp.status == SpanStatus.ERROR
