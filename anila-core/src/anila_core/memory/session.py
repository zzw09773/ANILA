"""Session abstraction — conversation persistence for ANILA agents.

Inspired by openai-agents `Session` protocol. ANILA-specific extension:
``pending_interrupts`` to support pause / resume flows (AskUserQuestion,
PlanMode, tool approvals — Sprint 9).

Two adapters ship in anila-core:

- :class:`anila_core.memory.memory_session.MemorySession` — in-process dict,
  for dev / tests.
- :class:`anila_core.memory.sqlite_session.SqliteSession` — aiosqlite-backed,
  default for single-process deployments.

Multi-process / HA deployments should provide their own adapter
(``PostgresSession``, ``RedisSession``, …) implementing the
:class:`Session` Protocol.

Why it lives in ``memory/``: a Session stores **conversation memory** for one
chat. It is distinct from the long-term ``memdir`` (cross-session facts the
agent learns about the user / project) and from the ``compact/`` layer
(token-budget control). Keep the three layers conceptually separate even
though they all sit under ``memory/``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from ..models.message import Message


@dataclass(frozen=True)
class InterruptRecord:
    """Persisted form of an interrupt request (ask-user / plan / tool-approval).

    The semantically rich ``InterruptItem`` lives in
    ``anila_core.engine.approvals`` (Sprint 9 PR 2). Storage adapters keep
    only this opaque record so PR 2 can iterate without rewriting them.

    ``payload`` is whatever JSON-serialisable dict the interrupt producer
    needs the resume side to see (question + options for ask-user, plan
    text for plan-mode, etc).
    """

    id: str
    kind: str
    payload: dict[str, Any]
    created_at: datetime = field(default_factory=datetime.utcnow)


@runtime_checkable
class Session(Protocol):
    """Protocol for session implementations.

    Stores conversation history + pending interrupts for one chat session.
    Implementations must be safe to call from a single asyncio event loop;
    cross-loop / cross-process safety is adapter-specific.
    """

    session_id: str

    # ---- conversation history ----

    async def get_items(self, limit: int | None = None) -> list[Message]:
        """Retrieve conversation history.

        Args:
            limit: maximum items to return; ``None`` = all. When set,
                returns the latest ``N`` items in **chronological** order
                (oldest first), so callers can feed the result straight
                into a model prompt.
        """
        ...

    async def add_items(self, items: list[Message]) -> None:
        """Append items to history (in order)."""
        ...

    async def pop_item(self) -> Message | None:
        """Remove and return the most recent item, or ``None`` if empty."""
        ...

    async def clear_session(self) -> None:
        """Clear all history **and** pending interrupts for this session."""
        ...

    # ---- pending interrupts ----

    async def pending_interrupts(self) -> list[InterruptRecord]:
        """List unanswered interrupts in created-at order (oldest first)."""
        ...

    async def push_interrupt(self, record: InterruptRecord) -> None:
        """Add a new interrupt to the pending queue."""
        ...

    async def pop_interrupt(self, interrupt_id: str) -> InterruptRecord | None:
        """Remove and return the named interrupt, or ``None`` if absent."""
        ...


def new_session_id() -> str:
    """Generate a fresh session_id (UUID4 hex form, lowercase)."""
    return uuid.uuid4().hex


def new_interrupt_id() -> str:
    """Generate a fresh interrupt_id."""
    return f"int-{uuid.uuid4().hex[:16]}"
