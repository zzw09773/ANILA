"""``RunState`` — the immutable snapshot the StateMachine transitions through.

Architecture spec ``docs/anila-agent-framework-architecture.md`` §3.2.

Every step the runner takes produces a NEW ``RunState`` (frozen
dataclass). Old states are kept by callers that want them — the
runtime itself never mutates a state, so a checkpoint pinned in
memory is automatically resumable later.

Why immutable: race-free concurrent inspection by middleware /
tracing / dashboards while the run continues. Cheap to serialise.
And a checkpoint is just "the latest state", no defensive copying
machinery required.

Why phase-explicit (vs the implicit ``while True`` in v0.1's runner):

- ``StateMachine.step()`` becomes a pure function modulo I/O — easy
  to unit-test phase transitions in isolation
- Resume after a pod restart is "load state from disk → ``step()``"
  with no special bootstrap path
- Tracing dashboards can render a state machine diagram of the live
  run by reading ``state.phase``
- Future RunMonitorMiddleware can watch for stuck phases (still
  ``ACTING`` after 60s? probably hung)

This module ships only the data shapes. The transition logic lives
in ``state_machine.py``; serialisation in ``serialization.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from agentic_rag.runtime.framework.items import (
    Message,
    RunItem,
    ToolCall,
)
from agentic_rag.runtime.framework.usage import Usage


# ── Phase ────────────────────────────────────────────────────────────


class RunPhase(StrEnum):
    """The seven phases an agent run progresses through.

    Transitions:

        PLANNING ──tool_calls──▶ ACTING ◀─more_tools─┐
            │                         │              │
            │                         ▼              │
            │                     OBSERVING ─────────┘
            │                         │
            │                       (last tool done) │
            │                         ▼              │
            └────── REFLECTING (optional) ───────────┘
            │                         │
            │                       (accept)
            ▼                         ▼
          DONE                       DONE

        Any phase ──error──▶ ERROR
        ACTING ──handoff_target──▶ HANDING_OFF ──▶ PLANNING (new agent)

    REFLECTING is opt-in via ``Agent.reflection_enabled``; default
    flow skips straight from final PLANNING (no tool_calls) to DONE.
    """

    PLANNING = "planning"
    ACTING = "acting"
    OBSERVING = "observing"
    REFLECTING = "reflecting"
    HANDING_OFF = "handing_off"
    DONE = "done"
    ERROR = "error"

    @property
    def is_terminal(self) -> bool:
        return self in (RunPhase.DONE, RunPhase.ERROR)


# ── Pending tool call tracking ──────────────────────────────────────


@dataclass(frozen=True)
class PendingToolCall:
    """One LLM-emitted tool call waiting for ACTING dispatch.

    Wraps ``ToolCall`` plus a position index — preserves LLM-emit
    order so dispatch matches what the LLM intended even when the
    pending list is rebuilt mid-run from a serialised state.
    """

    call: ToolCall
    index: int  # Position within the parent assistant message's tool_calls


# ── Run state ────────────────────────────────────────────────────────


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class RunState:
    """Immutable snapshot of an agent run mid-flight.

    Every field below is captured as of the moment the StateMachine
    last produced this state. To advance, call
    ``StateMachine.step(state)`` which returns a NEW state — never
    mutates the input.

    Field meanings:

    Identity / correlation:
      - ``run_id`` — unique id of THIS run
      - ``parent_run_id`` — caller's run id (multi-agent / batch)
      - ``group_id`` — conversation thread id; multiple runs may share
      - ``trace_metadata`` — caller-supplied free-form correlation bag

    Conversation:
      - ``agent_name`` — currently active agent (changes on handoff)
      - ``model`` — model id this agent is pinned to (helps cost /
        compaction layers)
      - ``history`` — the message log the next LLM call will see
      - ``items`` — audit trail (one item per observable event)

    Execution state:
      - ``phase`` — current ``RunPhase``; drives StateMachine.step()
      - ``turns_completed`` — count of (LLM call → tool_dispatch) cycles
      - ``max_turns`` — hard cap from the agent
      - ``pending_tool_calls`` — tools the LLM emitted in the last
        PLANNING phase, waiting for ACTING dispatch (FIFO)
      - ``handoff_target_name`` — set during HANDING_OFF, consumed on
        next PLANNING
      - ``reflection_count`` — REFLECTING phase iterations so far;
        capped to prevent reflection loops

    Accounting:
      - ``usage`` — accumulated token / request usage
      - ``deadline_at`` — monotonic deadline; checkpoint preserves so
        resume honors original wall-clock budget
      - ``created_at`` / ``updated_at`` — for staleness / TTL checks

    Termination:
      - ``final_output`` — final assistant text once DONE
      - ``parsed_output`` — typed output if Agent.output_type set
      - ``error_type`` / ``error_message`` — populated on ERROR

    Note on what's NOT here: middleware chain, agent / provider
    references, runtime callbacks. RunState is data; the StateMachine
    is what stitches it back to live execution.
    """

    # Identity
    run_id: str
    agent_name: str
    model: str
    parent_run_id: str | None = None
    group_id: str | None = None
    trace_metadata: dict[str, Any] = field(default_factory=dict)

    # Phase machinery
    phase: RunPhase = RunPhase.PLANNING
    turns_completed: int = 0
    max_turns: int = 10
    pending_tool_calls: tuple[PendingToolCall, ...] = ()
    handoff_target_name: str | None = None
    reflection_count: int = 0
    max_reflections: int = 1

    # Conversation
    history: tuple[Message, ...] = ()
    items: tuple[RunItem, ...] = ()

    # Accounting
    usage: Usage = field(default_factory=Usage)
    deadline_at: float | None = None
    created_at: datetime = field(default_factory=_utc_now)
    updated_at: datetime = field(default_factory=_utc_now)

    # Termination
    final_output: str | None = None
    parsed_output: Any = None
    error_type: str | None = None
    error_message: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.phase.is_terminal

    @property
    def has_pending_tools(self) -> bool:
        return bool(self.pending_tool_calls)

    def with_phase(self, phase: RunPhase, **changes: Any) -> RunState:
        """Build a new state with a different phase + arbitrary field overrides.

        Always bumps ``updated_at``. Use ``replace()`` directly if you
        need to overwrite ``updated_at`` (e.g. for serialisation
        round-trip tests).
        """
        return replace(self, phase=phase, updated_at=_utc_now(), **changes)

    def append_history(self, *messages: Message) -> RunState:
        """Return a new state with messages appended to history."""
        return replace(
            self,
            history=self.history + tuple(messages),
            updated_at=_utc_now(),
        )

    def append_items(self, *items: RunItem) -> RunState:
        """Return a new state with audit-trail items appended."""
        return replace(
            self,
            items=self.items + tuple(items),
            updated_at=_utc_now(),
        )

    def with_usage_added(self, delta: Usage) -> RunState:
        """Return a new state with ``delta`` accumulated into usage.

        ``Usage.add`` mutates in place; we copy the existing usage
        first so the source state's usage stays untouched.
        """
        new_usage = Usage(
            requests=self.usage.requests,
            input_tokens=self.usage.input_tokens,
            output_tokens=self.usage.output_tokens,
            total_tokens=self.usage.total_tokens,
        )
        new_usage.input_tokens_details = type(self.usage.input_tokens_details)(
            cached_tokens=self.usage.input_tokens_details.cached_tokens
        )
        new_usage.output_tokens_details = type(self.usage.output_tokens_details)(
            reasoning_tokens=self.usage.output_tokens_details.reasoning_tokens
        )
        new_usage.request_usage_entries = list(self.usage.request_usage_entries)
        new_usage.add(delta)
        return replace(self, usage=new_usage, updated_at=_utc_now())


__all__ = ["PendingToolCall", "RunPhase", "RunState"]
