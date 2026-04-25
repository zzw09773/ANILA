"""AgenticRAG storage adapters.

History:

- v0.5: shipped local ``pg_pool``, ``pgvector_store``, ``postgres_store``,
  ``memory_file_store``. The first two duplicated what would later become
  the central ANILA platform SDK; the last two are AgenticRAG-specific
  conversation persistence and stay.
- v0.6 / Phase 2 Sprint 1 Chunk F: ``pg_pool`` and ``pgvector_store``
  were retired in favour of ``anila_core.storage.adapters``. The
  ``document_chunks`` schema is now owned by CSP migrations 0014 + 0015
  and accessed exclusively through ``AgentScopedPgVectorStore`` so RLS
  enforcement (``SET LOCAL anila.agent_id``) is centralised.

What's left here:

- ``MemoryFileStore`` — filesystem MemoryStore impl for the conversation
  memdir / extract / relevance pipeline. Not RAG-specific.
- ``PgSessionStore`` / ``PgMessageStore`` / ``PgRetrievalTraceStore``
  via ``postgres_store`` — chat-side persistence (sessions / messages /
  retrieval audit trail). Schema lives in ``initialize_schema``.

Importing ``PgPool`` from this package now re-exports anila_core's so
existing call sites in ``postgres_store.py`` and elsewhere continue to
work without chasing the central package path.
"""

from anila_core.storage.adapters import PgPool

from .memory_file_store import MemoryFileStore
from .postgres_store import (
    PgMessageStore,
    PgRetrievalTraceStore,
    PgSessionStore,
    initialize_schema,
)

__all__ = [
    "MemoryFileStore",
    "PgMessageStore",
    "PgPool",
    "PgRetrievalTraceStore",
    "PgSessionStore",
    "initialize_schema",
]
