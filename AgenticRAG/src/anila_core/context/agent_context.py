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
from typing import Any, Optional

from ..models.agent import AgentDefinition
from ..models.message import Message


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
    )
