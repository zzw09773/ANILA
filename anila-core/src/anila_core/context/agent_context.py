"""Agent context isolation — Python port of Claude Code AsyncLocalStorage.

Uses contextvars.ContextVar so each asyncio Task gets its own isolated view
of the current agent context. Subagent contexts fork the parent's state
without sharing mutable references.
"""

from __future__ import annotations

import asyncio
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from ..models.agent import AgentDefinition, Todo
from ..models.message import Message

# Forward declaration to avoid hard import (caller_context imports
# from ``fastapi``; agent_context is imported by code paths that
# don't always have FastAPI available, e.g. CLI tooling). Type
# checkers see the real class via TYPE_CHECKING.
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..api.caller_context import CallerContext


# Async (event_name, payload) → None — see AgentContext.event_emitter.
EventEmitter = Callable[[str, dict[str, Any]], Awaitable[None]]


_current_context: ContextVar["AgentContext"] = ContextVar("current_context")


@dataclass
class AgentContext:
    """Isolated runtime context for a single agent invocation.

    Designed to be forked: create_subagent_context() copies all relevant
    state so the subagent cannot accidentally mutate the parent's history,
    abort signal, or memory snapshot.
    """

    context_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str = ""
    agent_type: str = "default"
    model: str = ""
    messages: list[Message] = field(default_factory=list)
    memory_snapshot: dict[str, Any] = field(default_factory=dict)
    abort_signal: Optional[asyncio.Event] = None
    is_forked: bool = False
    parent_context_id: Optional[str] = None
    allowed_tools: set[str] = field(default_factory=set)
    metadata: dict[str, Any] = field(default_factory=dict)
    # Sprint 9: plan_mode gates DESTRUCTIVE tools so the model can draft
    # without committing. Toggled by ``tools.plan_mode.enter_plan_mode``;
    # cleared after ``exit_plan_mode`` is approved.
    plan_mode: bool = False
    todos: list[Todo] = field(default_factory=list)
    """Sprint 9 PR 4: TodoWrite tool writes here."""
    event_emitter: Optional[EventEmitter] = None
    """Sprint 9 PR 4: optional async ``(event_name, payload) -> None``
    callback the FastAPI server installs so tools can push SSE events
    (e.g. ``todos_updated``) without coupling to transport details."""

    classified_latch: bool = False
    """Sprint 13 follow-up: one-way latch flipped by tools that received
    a classified payload during this run. ``agent_as_tool`` flips it
    when the consulted agent's ``anila_meta.classified`` is True so the
    *calling* agent's response builder can OR it into its own
    ``anila_meta.classified``. The latch is per-run (not persisted across
    sessions) and never downgrades — once True, stays True for the
    remainder of the turn. Forks (``create_subagent_context``) inherit
    the parent's value so subagents start at least as tainted as their
    parent."""

    caller: Optional["CallerContext"] = None
    """Route-3 Phase 3: identity headers forwarded by CSP for this run.

    Populated by ``api.caller_context.extract_caller_context`` before
    the engine starts. Carries the calling user's id + the agent's
    own service token, which together let the agent call back into
    CSP for cross-tenant reads (notably user memory facts via
    :func:`anila_core.memory.long_term.clients.make_user_memory_reader`).

    None when the request didn't come through CSP (dev / test
    curl). Agent code should None-check before using identity-bound
    features and degrade to "no user attribution" rather than fail.
    Forked subagent contexts inherit the parent's caller verbatim —
    a subagent serves the same user as its parent."""

    def __post_init__(self) -> None:
        if self.abort_signal is None:
            self.abort_signal = asyncio.Event()

    def is_aborted(self) -> bool:
        """Return True if the abort signal has been fired."""
        return self.abort_signal is not None and self.abort_signal.is_set()

    def abort(self) -> None:
        """Signal that this context should abort."""
        if self.abort_signal is not None:
            self.abort_signal.set()


def get_current_context() -> Optional[AgentContext]:
    """Return the context bound to the current async task, or None."""
    return _current_context.get(None)


def set_current_context(ctx: AgentContext) -> None:
    """Bind a context to the current async task."""
    _current_context.set(ctx)


def create_subagent_context(
    parent: AgentContext,
    agent_def: Optional[AgentDefinition] = None,
    memory_snapshot: Optional[dict[str, Any]] = None,
    allowed_tools: Optional[set[str]] = None,
) -> AgentContext:
    """Fork a parent context for use by a subagent.

    The forked context:
    - Gets its own context_id and abort_signal
    - Copies the parent messages (independent list, not shared)
    - Uses agent_def's model if provided, else inherits parent model
    - Restricts to allowed_tools if provided
    - Inherits memory_snapshot by default (pass new dict to override)

    Background agents (memory extraction, session memory) should pass
    a restricted allowed_tools set to enforce read-only + memory-write-only.
    """
    fork_model = (
        agent_def.model if agent_def and agent_def.model else parent.model
    )
    fork_tools = allowed_tools if allowed_tools is not None else set(parent.allowed_tools)
    fork_agent_type = agent_def.agent_type if agent_def else parent.agent_type

    return AgentContext(
        session_id=parent.session_id,
        agent_type=fork_agent_type,
        model=fork_model,
        messages=list(parent.messages),  # independent copy
        memory_snapshot=memory_snapshot if memory_snapshot is not None
                        else dict(parent.memory_snapshot),
        abort_signal=asyncio.Event(),  # independent abort signal
        is_forked=True,
        parent_context_id=parent.context_id,
        allowed_tools=fork_tools,
        metadata=dict(parent.metadata),
        plan_mode=parent.plan_mode,
        todos=list(parent.todos),  # independent copy (Sprint 9 PR 4)
        event_emitter=parent.event_emitter,
        # Subagents start at least as tainted as their parent — never
        # downgrade. Subagent flipping it on does NOT auto-bubble back
        # to parent (forks have independent state); tools that want
        # parent-visible taint should set it on the parent's context
        # directly, which is what ``agent_as_tool`` does.
        classified_latch=parent.classified_latch,
        # Subagent inherits the parent's caller — same user, same
        # service token. Pass-by-reference because CallerContext is
        # frozen (immutable), so sharing is safe.
        caller=parent.caller,
    )
