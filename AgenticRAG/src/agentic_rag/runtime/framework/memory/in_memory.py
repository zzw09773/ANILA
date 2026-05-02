"""In-memory reference impls for ``MessageHistory`` and ``SemanticMemory``.

Suitable for tests, single-process dev work, and bootstrap scenarios
where persistence isn't yet wired. Production deployments inject their
own — AgenticRAG's bridge wires to memdir + relevance_selector.

``InMemorySemanticMemory`` uses a naive substring + recency ranker for
``recall``. That's intentionally simple: real ranking belongs in a
proper impl with embeddings or a Haiku-tier LLM selector. The reference
impl exists to make tests self-contained.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from agentic_rag.runtime.framework.items import Message
from agentic_rag.runtime.framework.memory.protocol import (
    MemoryEntry,
    MemoryKind,
)


# ── MessageHistory in-memory ─────────────────────────────────────────


@dataclass
class InMemoryMessageHistory:
    """Simple list-backed history. No persistence."""

    _messages: list[Message] = field(default_factory=list)

    async def append(self, message: Message) -> None:
        self._messages.append(message)

    async def get(self, *, limit: int | None = None) -> list[Message]:
        if limit is None or limit >= len(self._messages):
            return list(self._messages)
        return list(self._messages[-limit:])

    async def truncate(self, n: int) -> None:
        if n <= 0:
            return
        self._messages = self._messages[n:]

    async def clear(self) -> None:
        self._messages.clear()

    def __len__(self) -> int:
        return len(self._messages)


# ── SemanticMemory in-memory ─────────────────────────────────────────


@dataclass
class InMemorySemanticMemory:
    """Dict-backed semantic store with substring + recency recall.

    Recall ranking:
      1. Filter by kind (if specified) and expiry
      2. Score = substring matches in ``content`` (case-insensitive)
         + recency bonus (newer entries score higher)
      3. Sort descending, return ``limit`` head

    The ranking is deliberately naive — anyone serious about quality
    should plug in an embedding-based or LLM-selector impl. This one
    is for tests and dev loops.
    """

    _entries: dict[str, MemoryEntry] = field(default_factory=dict)

    async def remember(self, entry: MemoryEntry) -> None:
        self._entries[entry.id] = entry

    async def recall(
        self,
        query: str,
        *,
        kind: MemoryKind | None = None,
        limit: int = 5,
    ) -> list[MemoryEntry]:
        candidates = [
            e
            for e in self._entries.values()
            if not e.is_expired() and (kind is None or e.kind is kind)
        ]
        if not candidates:
            return []

        query_lower = query.lower().strip()
        if not query_lower:
            # No query — return most-recent first.
            ranked = sorted(candidates, key=lambda e: e.updated_at, reverse=True)
            return ranked[:limit]

        # Tokenise query into words for slightly-better matching than a
        # single substring scan. Score = unique tokens hit.
        tokens = [t for t in query_lower.split() if t]
        scored: list[tuple[float, MemoryEntry]] = []
        for entry in candidates:
            content_lower = entry.content.lower()
            matches = sum(1 for t in tokens if t in content_lower)
            if matches == 0:
                continue
            # Recency bonus tiebreaker: scale 0..0.5 so it never trumps
            # a real match difference.
            recency = _recency_score(entry.updated_at)
            scored.append((matches + recency * 0.5, entry))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [entry for _, entry in scored[:limit]]

    async def forget(self, entry_id: str) -> None:
        self._entries.pop(entry_id, None)

    async def list_all(
        self, *, kind: MemoryKind | None = None
    ) -> list[MemoryEntry]:
        return [
            e
            for e in self._entries.values()
            if not e.is_expired() and (kind is None or e.kind is kind)
        ]

    def __len__(self) -> int:
        return len(self._entries)


def _recency_score(updated_at: datetime) -> float:
    """Map a timestamp onto [0, 1] — newer entries score higher.

    Pragmatic decay: anything updated in the last hour scores ~1,
    anything older than a week scores ~0. Sigmoid-ish curve in
    between via a simple linear interp.
    """
    from datetime import timezone

    now = datetime.now(timezone.utc)
    if updated_at.tzinfo is None:
        # Defensive — UTC-naive timestamps shouldn't happen but won't
        # crash the comparison if they do.
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    delta_seconds = max(0.0, (now - updated_at).total_seconds())
    week = 7 * 24 * 3600
    if delta_seconds <= 3600:  # 1h
        return 1.0
    if delta_seconds >= week:
        return 0.0
    return 1.0 - (delta_seconds - 3600) / (week - 3600)


__all__ = ["InMemoryMessageHistory", "InMemorySemanticMemory"]
