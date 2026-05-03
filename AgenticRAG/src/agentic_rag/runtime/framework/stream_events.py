"""Streaming events emitted by ``Runner.stream()``.

Two consumers care about streaming:

1. **SSE / WebSocket frontends** — want to push tokens / tool starts /
   tool finishes to a UI in real time without waiting for the entire
   run to finish.
2. **Tracing / dashboards** — want to surface partial state (which
   tool is currently running, how many turns elapsed, current
   active agent) for live observability.

Both reduce to "emit observable events as they happen." The
``Runner.stream()`` async generator yields ``StreamEvent`` instances;
``Runner.run()`` is now a thin wrapper that drains the stream and
returns the final ``RunResult``.

Event taxonomy:

- ``MessageDeltaEvent`` — the runner emitted a (whole) assistant
  message at a turn boundary. Phase A v0.1 still does NOT do
  per-token deltas inside a single LLM call (the framework's
  ``LLMProvider.chat_completion`` returns unary). Per-token streaming
  needs the provider's streaming surface plumbed through; that lands
  in Sprint 3 alongside the StateMachine refactor. The current event
  fires once per assistant turn — coarse but useful for SSE
  responsiveness.
- ``ToolCallStartedEvent`` / ``ToolCallFinishedEvent`` — wrap each
  tool dispatch.
- ``HandoffEvent`` — control transferred to another agent.
- ``UsageUpdateEvent`` — usage accumulator changed.
- ``RunCompletedEvent`` — terminal event, carries the final result.
- ``RunErrorEvent`` — terminal event for an aborted run.

All events are frozen dataclasses with a ``type`` discriminator field
so downstream code can dispatch on string keys (``isinstance`` works
too, but the discriminator is friendlier for JSON serialisation).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Union

from agentic_rag.runtime.framework.items import (
    Message,
    ToolCall,
    ToolResult,
)
from agentic_rag.runtime.framework.usage import Usage


@dataclass(frozen=True)
class MessageDeltaEvent:
    """An assistant message produced this turn.

    For the v0.1 unary runner, ``message`` is the whole assistant
    message; ``is_partial`` is always False. Sprint 3's streaming
    runner will emit incremental deltas with ``is_partial=True``
    followed by a final ``is_partial=False`` when the LLM call closes.
    """

    message: Message
    turn_index: int
    is_partial: bool = False
    type: Literal["message_delta"] = "message_delta"


@dataclass(frozen=True)
class ToolCallStartedEvent:
    """An action invocation is about to run."""

    call: ToolCall
    agent_name: str
    type: Literal["tool_call_started"] = "tool_call_started"


@dataclass(frozen=True)
class ToolCallFinishedEvent:
    """An action invocation completed (success or recoverable error)."""

    result: ToolResult
    elapsed_seconds: float
    type: Literal["tool_call_finished"] = "tool_call_finished"


@dataclass(frozen=True)
class HandoffEvent:
    """Control transferred from one agent to another."""

    from_agent: str
    to_agent: str
    reason: str | None = None
    type: Literal["handoff"] = "handoff"


@dataclass(frozen=True)
class UsageUpdateEvent:
    """Aggregate usage advanced by ``delta`` (a single LLM call's worth)."""

    delta: Usage
    cumulative: Usage
    type: Literal["usage_update"] = "usage_update"


@dataclass(frozen=True)
class RunCompletedEvent:
    """Terminal event — the run finished cleanly.

    Held separately from ``MessageDeltaEvent`` so consumers can
    distinguish "another turn happened" from "we're done." The full
    ``RunResult`` is attached for callers that want to short-circuit
    a typical drain-into-result pipeline.
    """

    final_output: str
    final_agent_name: str
    turns: int
    usage: Usage
    parsed_output: Any = None
    type: Literal["run_completed"] = "run_completed"


@dataclass(frozen=True)
class RunErrorEvent:
    """Terminal event — the run aborted.

    ``error_type`` is the exception class name; ``message`` is the
    human-readable description. Carries enough state to log without
    needing the original exception object (useful for SSE wire format).
    """

    error_type: str
    message: str
    turns_completed: int
    metadata: dict[str, Any] = field(default_factory=dict)
    type: Literal["run_error"] = "run_error"


StreamEvent = Union[
    MessageDeltaEvent,
    ToolCallStartedEvent,
    ToolCallFinishedEvent,
    HandoffEvent,
    UsageUpdateEvent,
    RunCompletedEvent,
    RunErrorEvent,
]
"""Discriminated union of every event ``Runner.stream()`` emits."""


__all__ = [
    "HandoffEvent",
    "MessageDeltaEvent",
    "RunCompletedEvent",
    "RunErrorEvent",
    "StreamEvent",
    "ToolCallFinishedEvent",
    "ToolCallStartedEvent",
    "UsageUpdateEvent",
]
