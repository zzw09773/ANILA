"""Storage backend Protocol for user-tenant memory.

anila-core defines the contract; concrete implementations live in
the platform that physically hosts the storage. Today that's
``myCSPPlatform.app.services.memory_service.PostgresMemoryAdapter``
(SQLAlchemy + httpx + pgvector). Test code can swap in an
in-memory fake without monkey-patching anything.

Method shapes match the call sites in CSP's ``proxy.py`` hooks and
``api/memory.py`` REST routes one-to-one — Phase 2 of the RFC is
literally "wire the existing CSP service through this Protocol",
not a redesign.

Adapter implementations MUST:

* Be safe to call concurrently for distinct ``user_id`` values.
* Never raise on a missing-fact / missing-chunk lookup — return an
  empty list and let the caller decide whether that's an error.
* Surface unrecoverable errors (DB down, embed endpoint 5xx) as
  exceptions that the caller can choose to log + degrade. The
  current CSP proxy wraps every read in a try/except so a memory
  outage degrades chat to "no memory injected" instead of failing
  the whole request — adapters should preserve that opt-in.
"""
from __future__ import annotations

from typing import Any, Optional, Protocol, runtime_checkable

from .models import MemoryReadResult, RetrievedChunk, UserFactDTO


@runtime_checkable
class MemoryAdapter(Protocol):
    """Async CRUD + retrieval contract for user memory storage."""

    # ── Facts (structured key/value) ────────────────────────────────────────

    async def get_user_facts(self, user_id: int) -> list[UserFactDTO]:
        """Return all facts for ``user_id``, newest-first."""
        ...

    async def upsert_user_facts(
        self,
        user_id: int,
        facts: list[dict[str, Any]],
        *,
        source_conversation_id: Optional[int] = None,
        source_message_id: Optional[int] = None,
    ) -> None:
        """Bulk-upsert on ``(user_id, key)`` — newest write wins.

        ``facts`` is the list emitted by
        :func:`anila_core.memory.user.extraction.parse_extraction_response`,
        already validated. Adapter does not re-validate.
        """
        ...

    async def delete_user_fact(self, user_id: int, fact_id: int) -> bool:
        """Delete a single fact by id. Returns True if a row was deleted.

        Adapter is responsible for cross-user-id-scoping — return
        False on a fact_id that exists but belongs to a different
        user (don't leak existence with 404 vs 403).
        """
        ...

    async def clear_user_facts(self, user_id: int) -> int:
        """Wipe every fact for ``user_id``. Returns delete count."""
        ...

    # ── Chunks (cross-conversation RAG) ─────────────────────────────────────

    async def write_chunk(
        self,
        *,
        user_id: int,
        conversation_id: int,
        message_id: Optional[int],
        role: str,
        content: str,
        is_encrypted: bool,
    ) -> None:
        """Embed + store one message slice.

        Adapter owns the embed call (so it can batch / cache /
        choose model per deployment). Empty ``content`` is a no-op
        — caller doesn't need to pre-filter.
        """
        ...

    async def retrieve_relevant_chunks(
        self,
        user_id: int,
        query_text: str,
        *,
        exclude_conversation_id: Optional[int] = None,
        top_k: int = 3,
        min_cosine: float = 0.4,
    ) -> list[RetrievedChunk]:
        """ANN search over this user's chunks.

        ``exclude_conversation_id`` filters out the active
        conversation's own chunks — those messages are already in
        the chat history the LLM will see, no need to surface them
        again as "past discussion".
        """
        ...

    async def clear_user_chunks(self, user_id: int) -> int:
        """Wipe every chunk for ``user_id``. Returns delete count."""
        ...

    # ── Combined read (the proxy hook entry point) ──────────────────────────

    async def build_memory_block(
        self,
        user_id: int,
        latest_user_message: str,
        *,
        exclude_conversation_id: Optional[int] = None,
    ) -> MemoryReadResult:
        """One-call read: facts + RAG, formatted for system-prompt injection.

        Default impl can compose ``get_user_facts`` + ``retrieve_relevant_chunks``;
        adapters that can do both in a single round-trip (e.g. a
        future Postgres-side join) can override for latency.
        """
        ...

    # ── Background write (the post-turn hook entry point) ───────────────────

    async def persist_turn(
        self,
        *,
        user_id: int,
        conversation_id: int,
        user_message: str,
        assistant_message: str,
        is_encrypted: bool,
        user_message_id: Optional[int] = None,
        assistant_message_id: Optional[int] = None,
    ) -> None:
        """Fire-and-forget post-turn write: chunks + extracted facts.

        Adapter owns the LLM call for fact extraction. This method
        must NEVER raise — callers schedule it as a background task
        and a propagating exception would crash the worker and lose
        every subsequent turn's memory write.
        """
        ...
