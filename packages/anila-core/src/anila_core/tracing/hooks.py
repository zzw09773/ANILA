"""``TracingHooks`` — RunHooks adapter that auto-emits spans.

Pass a tracer in and you get hierarchical tracing for free::

    from anila_core.engine import QueryEngine, QueryConfig
    from anila_core.tracing import Tracer, InMemoryProcessor, TracingHooks

    proc = InMemoryProcessor()
    tracer = Tracer(processors=[proc])
    hooks = TracingHooks(tracer)
    engine = QueryEngine(provider, registry, config, hooks=hooks)
    await engine.run(messages)
    # proc.to_tree() now has the full span tree.

Span layout for a typical run:

    run "agent:<id>"            ← on_run_start
    └── agent "<id>"            ← on_agent_start
        ├── tool "<name>"       ← on_tool_start / on_tool_end
        ├── tool "<name>"
        ├── handoff "<target>"  ← on_handoff (terminal)
        └── interrupt "<kind>"  ← on_run_paused (terminal)

In-flight spans are tracked by call_id (tools) / interrupt_id (pauses)
so the matching ``on_*_end`` hook closes the right one.
"""

from __future__ import annotations

from typing import Any

from ..engine.lifecycle import RunHooks
from ..models.handoff import HandoffRequest
from ..models.message import ToolCall, ToolResult
from .span import Span, SpanKind, SpanStatus
from .tracer import Tracer


class TracingHooks(RunHooks):
    """Bridge :class:`RunHooks` events into :class:`Tracer` spans."""

    def __init__(self, tracer: Tracer) -> None:
        self._tracer = tracer
        self._run_span: dict[str, Span] = {}
        self._agent_span: dict[str, Span] = {}
        self._tool_span: dict[str, Span] = {}  # keyed by call_id

    # ------------------------------------------------------------------
    # Run / agent
    # ------------------------------------------------------------------

    async def on_run_start(self, *, agent_id: str, session_id: str) -> None:
        sp = self._tracer.start_span(
            f"run:{agent_id or 'anonymous'}",
            kind=SpanKind.RUN,
            agent_id=agent_id,
            session_id=session_id,
        )
        self._run_span[session_id] = sp

    async def on_run_end(
        self, *, agent_id: str, session_id: str, result: Any
    ) -> None:
        sp = self._run_span.pop(session_id, None)
        if sp is not None:
            sp.set_attribute(
                "stop_reason", getattr(result, "stop_reason", "unknown")
            )
            sp.set_attribute(
                "turn_count", getattr(result, "turn_count", 0)
            )
            self._tracer.end_span(sp)

    async def on_agent_start(self, *, agent_id: str, session_id: str) -> None:
        sp = self._tracer.start_span(
            f"agent:{agent_id or 'anonymous'}",
            kind=SpanKind.AGENT,
            agent_id=agent_id,
        )
        self._agent_span[session_id] = sp

    async def on_agent_end(
        self, *, agent_id: str, session_id: str, result: Any
    ) -> None:
        sp = self._agent_span.pop(session_id, None)
        if sp is not None:
            self._tracer.end_span(sp)

    # ------------------------------------------------------------------
    # Tool calls
    # ------------------------------------------------------------------

    async def on_tool_start(
        self, *, agent_id: str, session_id: str, call: ToolCall
    ) -> None:
        sp = self._tracer.start_span(
            f"tool:{call.name}",
            kind=SpanKind.TOOL,
            tool_name=call.name,
            tool_call_id=call.id,
        )
        self._tool_span[call.id] = sp

    async def on_tool_end(
        self,
        *,
        agent_id: str,
        session_id: str,
        call: ToolCall,
        result: ToolResult,
    ) -> None:
        sp = self._tool_span.pop(call.id, None)
        if sp is None:
            return
        if result.is_error:
            sp.set_attribute(
                "error_preview",
                str(result.content)[:240] if result.content else "",
            )
            self._tracer.end_span(sp, status=SpanStatus.ERROR)
        else:
            self._tracer.end_span(sp, status=SpanStatus.OK)

    # ------------------------------------------------------------------
    # Pause / resume / handoff (terminal events on the active agent)
    # ------------------------------------------------------------------

    async def on_run_paused(
        self,
        *,
        agent_id: str,
        session_id: str,
        interrupt_id: str,
        kind: str,
    ) -> None:
        # Emit a zero-duration span as a marker; nothing remains open.
        sp = self._tracer.start_span(
            f"interrupt:{kind}",
            kind=SpanKind.INTERRUPT,
            interrupt_id=interrupt_id,
            interrupt_kind=kind,
        )
        self._tracer.end_span(sp)

    async def on_run_resumed(
        self, *, agent_id: str, session_id: str, interrupt_id: str
    ) -> None:
        sp = self._tracer.start_span(
            f"resume:{interrupt_id}",
            kind=SpanKind.INTERRUPT,
            interrupt_id=interrupt_id,
        )
        self._tracer.end_span(sp)

    async def on_handoff(
        self,
        *,
        source_agent_id: str,
        session_id: str,
        request: HandoffRequest,
    ) -> None:
        sp = self._tracer.start_span(
            f"handoff:{request.target_agent_id}",
            kind=SpanKind.HANDOFF,
            source_agent=source_agent_id,
            target_agent=request.target_agent_id,
            handoff_id=request.id,
            reason=request.reason,
        )
        self._tracer.end_span(sp)
