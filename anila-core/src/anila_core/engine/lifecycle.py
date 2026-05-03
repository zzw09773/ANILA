"""Lifecycle hooks for QueryEngine.

Mirrors openai-agents `lifecycle.RunHooks` shape, adapted to ANILA's
flatter agent model (no separate ``Agent`` class — the agent_id +
session_id pair plus the in-flight `TurnResult` is what hooks see).

Use this layer for **synchronous instrumentation** that needs to react
to specific run events: structured logging, metrics, hierarchical
tracing (Sprint 11 PR 2), audit trails, …. Distinct from QueryEngine's
``post_turn_hooks`` which run *after* a successful turn and are
deliberately fire-and-forget.

Subclass :class:`RunHooks` and override only what you need; default
implementations are no-ops. Pass an instance to ``QueryEngine(hooks=…)``.

Hook firing order for a successful turn:

    on_run_start
      on_agent_start
        (one or more api_call → tool dispatches; each tool call:)
          on_tool_start
          on_tool_end
        (loop ends naturally OR an interrupt fires:)
        on_run_paused      ← if a tool returned InterruptItem
        on_handoff         ← if a tool returned HandoffRequest
      on_agent_end
    on_run_end

A resume entry point fires ``on_run_resumed`` then re-enters the
``on_run_start`` chain for the resumed turn.

All hook methods are async. Exceptions raised by hooks are caught and
logged — they never abort the run loop.
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Protocol, runtime_checkable

from ..models.handoff import HandoffRequest
from ..models.message import ToolCall, ToolResult

logger = logging.getLogger(__name__)


@runtime_checkable
class RunHooksProtocol(Protocol):
    """Structural type for any hook implementation."""

    async def on_run_start(
        self, *, agent_id: str, session_id: str
    ) -> None: ...

    async def on_run_end(
        self,
        *,
        agent_id: str,
        session_id: str,
        result: Any,  # TurnResult — Any to avoid forward import
    ) -> None: ...

    async def on_agent_start(
        self, *, agent_id: str, session_id: str
    ) -> None: ...

    async def on_agent_end(
        self, *, agent_id: str, session_id: str, result: Any
    ) -> None: ...

    async def on_tool_start(
        self,
        *,
        agent_id: str,
        session_id: str,
        call: ToolCall,
    ) -> None: ...

    async def on_tool_end(
        self,
        *,
        agent_id: str,
        session_id: str,
        call: ToolCall,
        result: ToolResult,
    ) -> None: ...

    async def on_run_paused(
        self,
        *,
        agent_id: str,
        session_id: str,
        interrupt_id: str,
        kind: str,
    ) -> None: ...

    async def on_run_resumed(
        self,
        *,
        agent_id: str,
        session_id: str,
        interrupt_id: str,
    ) -> None: ...

    async def on_handoff(
        self,
        *,
        source_agent_id: str,
        session_id: str,
        request: HandoffRequest,
    ) -> None: ...


class RunHooks:
    """Concrete base class — every method is a no-op.

    Subclass and override the hooks you care about::

        class TraceHooks(RunHooks):
            async def on_tool_start(self, *, agent_id, session_id, call):
                tracer.add_event(f"tool.start {call.name}")
    """

    async def on_run_start(
        self, *, agent_id: str, session_id: str
    ) -> None:
        return None

    async def on_run_end(
        self, *, agent_id: str, session_id: str, result: Any
    ) -> None:
        return None

    async def on_agent_start(
        self, *, agent_id: str, session_id: str
    ) -> None:
        return None

    async def on_agent_end(
        self, *, agent_id: str, session_id: str, result: Any
    ) -> None:
        return None

    async def on_tool_start(
        self,
        *,
        agent_id: str,
        session_id: str,
        call: ToolCall,
    ) -> None:
        return None

    async def on_tool_end(
        self,
        *,
        agent_id: str,
        session_id: str,
        call: ToolCall,
        result: ToolResult,
    ) -> None:
        return None

    async def on_run_paused(
        self,
        *,
        agent_id: str,
        session_id: str,
        interrupt_id: str,
        kind: str,
    ) -> None:
        return None

    async def on_run_resumed(
        self,
        *,
        agent_id: str,
        session_id: str,
        interrupt_id: str,
    ) -> None:
        return None

    async def on_handoff(
        self,
        *,
        source_agent_id: str,
        session_id: str,
        request: HandoffRequest,
    ) -> None:
        return None


async def _safe_call(
    hooks: Optional["RunHooks"],
    method_name: str,
    /,
    **kwargs: Any,
) -> None:
    """Call ``hooks.<method_name>(**kwargs)`` swallowing any exception.

    Used by QueryEngine to insulate the run loop from misbehaving
    instrumentation. When ``hooks`` is None, returns immediately.
    """
    if hooks is None:
        return
    method = getattr(hooks, method_name, None)
    if method is None:
        return
    try:
        await method(**kwargs)
    except Exception as exc:  # noqa: BLE001 — we explicitly want to swallow
        logger.warning("RunHooks.%s raised: %s", method_name, exc)


__all__ = ["RunHooks", "RunHooksProtocol", "_safe_call"]
