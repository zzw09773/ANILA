"""Pure DTOs for the user-tenant memory layer.

These mirror the shapes the CSP storage backend serialises to/from
SQL, but carry no ORM dependency — anila-core is the single source
of truth for what a "fact" or "retrieved chunk" looks like, and
storage backends conform.

All three types are frozen dataclasses so an adapter can return a
list and the consumer can hash / dedupe / pass through middleware
without worrying about identity mutation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass(frozen=True)
class UserFactDTO:
    """A single piece of long-term, structured knowledge about a user.

    ``id`` is optional so the same dataclass can represent both
    persisted facts (server-assigned id) and not-yet-saved facts
    being passed into an upsert. ``confidence`` is clamped to
    ``[0.0, 1.0]`` at extraction time; consumers can trust the
    range without re-validating.
    """

    user_id: int
    key: str
    value: str
    confidence: float = 1.0
    id: Optional[int] = None
    source_conversation_id: Optional[int] = None
    source_message_id: Optional[int] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


@dataclass(frozen=True)
class RetrievedChunk:
    """One hit from cross-conversation RAG search.

    Chunks come back ranked by ``cosine`` similarity (higher = more
    relevant). ``is_encrypted`` flows verbatim from the stored row;
    consumers are responsible for honouring it (the CSP proxy uses
    it to latch the consuming conversation into classified state —
    see P3 / migration 0031).
    """

    id: int
    conversation_id: int
    role: str  # 'user' | 'assistant'
    content: str
    cosine: float
    is_encrypted: bool


@dataclass(frozen=True)
class MemoryReadResult:
    """Bundle of everything pulled before a chat completion.

    The proxy P3 latch keys off ``encryption_inherited`` — if any
    retrieved chunk was written from an encrypted conversation, the
    consuming conversation must be upgraded to classified before
    the LLM call returns. Keep the property here (rather than on the
    proxy side) so any downstream consumer of MemoryReadResult gets
    the same Bell-LaPadula semantics for free.
    """

    block: Optional[str]
    facts_count: int
    chunks: list[RetrievedChunk] = field(default_factory=list)

    @property
    def encryption_inherited(self) -> bool:
        return any(c.is_encrypted for c in self.chunks)
