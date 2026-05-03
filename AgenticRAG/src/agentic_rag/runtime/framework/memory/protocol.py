"""Memory Protocols — ``MessageHistory`` (per-run) + ``SemanticMemory`` (cross-run).

Both Protocols are runtime_checkable so concrete impls don't need to
inherit nominally — duck-typed satisfaction is enough. The framework
imports nothing concrete; impls live in ``in_memory.py`` (reference)
and ``runtime/bridge/semantic_memory_bridge.py`` (production).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from agentic_rag.runtime.framework.items import Message


# ── MemoryKind enum ──────────────────────────────────────────────────


class MemoryKind(StrEnum):
    """Typed slots for ``SemanticMemory`` entries.

    Mirrors claude-code's memdir taxonomy plus a ``WORKING`` slot for
    session-scoped scratch that auto-expires.
    """

    USER = "user"
    """Who the user is, role, expertise, preferences. Stable."""

    FEEDBACK = "feedback"
    """Corrections / rules the user wants followed. Behavioural."""

    PROJECT = "project"
    """Ongoing work state, deadlines, decisions. Decays fast."""

    REFERENCE = "reference"
    """Pointers to external systems (Linear board, Slack channel, etc.)"""

    WORKING = "working"
    """This-session scratch. Auto-expires via TTL."""


# ── MemoryEntry ──────────────────────────────────────────────────────


def _new_entry_id() -> str:
    return f"mem_{uuid.uuid4().hex[:16]}"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class MemoryEntry:
    """One persisted fact in the semantic store.

    Frozen — the store may version entries by re-inserting an updated
    copy with the same ``id``. Mutating in place would invalidate any
    cached recall result a caller is still iterating.

    ``ttl_seconds`` (None = forever) is honored by the store's recall
    path: expired entries are filtered out at read time. Compaction
    of expired entries is the store's responsibility (some impls run
    a background sweep, others lazy-delete).

    ``metadata`` is a free-form dict — useful for source attribution
    (``{"source_session": "s_42"}``), confidence scores, tags.
    """

    id: str = field(default_factory=_new_entry_id)
    kind: MemoryKind = MemoryKind.PROJECT
    content: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=_utc_now)
    updated_at: datetime = field(default_factory=_utc_now)
    ttl_seconds: float | None = None

    @property
    def expires_at(self) -> datetime | None:
        if self.ttl_seconds is None:
            return None
        return self.updated_at + timedelta(seconds=self.ttl_seconds)

    def is_expired(self, *, now: datetime | None = None) -> bool:
        exp = self.expires_at
        if exp is None:
            return False
        actual = now or _utc_now()
        return actual >= exp


# ── MessageHistory Protocol ──────────────────────────────────────────


@runtime_checkable
class MessageHistory(Protocol):
    """Per-run conversation log. Append-only sequence access.

    Implementations may persist (write to disk for resume after
    restart) or stay purely in-memory. The framework's ``RunState.history``
    tuple satisfies the read side of this Protocol; a separate impl is
    only needed when callers want to persist history independently of
    the RunState snapshot path.

    Methods are async to leave room for I/O-backed impls without
    rewriting the call sites later.
    """

    async def append(self, message: Message) -> None:
        ...

    async def get(self, *, limit: int | None = None) -> list[Message]:
        ...

    async def truncate(self, n: int) -> None:
        """Drop the FIRST ``n`` messages. Used by compaction."""
        ...

    async def clear(self) -> None:
        ...


# ── SemanticMemory Protocol ──────────────────────────────────────────


@runtime_checkable
class SemanticMemory(Protocol):
    """Long-lived facts. Survives across runs.

    The framework consumes this Protocol; concrete impls (AgenticRAG
    memdir bridge, future Postgres-backed store) plug in via DI on the
    ``Agent`` (via ``Agent.semantic_memory`` if/when that lands) or
    consumed directly by user middleware that injects recall results
    into the prompt.

    ``recall`` returns at most ``limit`` entries matching ``query``,
    optionally filtered by ``kind``. Implementations decide ranking
    (embedding similarity, BM25, LLM-side selector, …); this Protocol
    enforces only the input/output shapes.
    """

    async def remember(self, entry: MemoryEntry) -> None:
        """Insert or upsert an entry. Upsert is keyed on ``entry.id``."""
        ...

    async def recall(
        self,
        query: str,
        *,
        kind: MemoryKind | None = None,
        limit: int = 5,
    ) -> list[MemoryEntry]:
        ...

    async def forget(self, entry_id: str) -> None:
        """Delete by id. No-op if missing."""
        ...

    async def list_all(
        self, *, kind: MemoryKind | None = None
    ) -> list[MemoryEntry]:
        """Return every (non-expired) entry, optionally filtered by kind.

        Mainly for dashboards / consolidation passes; recall is the
        normal access path.
        """
        ...


__all__ = [
    "MemoryEntry",
    "MemoryKind",
    "MessageHistory",
    "SemanticMemory",
]
