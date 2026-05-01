"""AgenticRAG storage adapters.

History:

- v0.5: shipped local ``pg_pool``, ``pgvector_store``, ``postgres_store``,
  ``memory_file_store``.
- v0.6 / Sprint 1 Chunk F: ``pg_pool`` + ``pgvector_store`` were
  retired in favour of ``anila_core.storage.adapters`` (centralised
  for RLS enforcement at the time when AgenticRAG was treated as a
  platform-internal service).
- Phase 0 (2026-05-02): boundary repositioned. AgenticRAG is now a
  fork-template for devs starting new agents, not a platform-internal
  service. Local copies of ``pg_pool`` and ``pgvector_store`` are
  re-introduced; platform deploys can inject anila-core's RLS-aware
  variant via ``app_factory.build_app(vector_store_override=...)``.

Modules:

- ``pg_pool`` — asyncpg pool with pgvector / halfvec / jsonb codecs
- ``pgvector_store`` — collection-scoped pgvector reader/writer
- ``memory_file_store`` — filesystem MemoryStore impl
- ``postgres_store`` — chat-side sessions / messages / retrieval_traces
"""

from .memory_file_store import MemoryFileStore
from .pg_pool import PgPool
from .pgvector_store import (
    AgentScopedPgVectorStore,  # back-compat alias for v0.5 callers
    CollectionScopedPgVectorStore,
)
from .postgres_store import (
    PgMessageStore,
    PgRetrievalTraceStore,
    PgSessionStore,
    initialize_schema,
)

__all__ = [
    "AgentScopedPgVectorStore",
    "CollectionScopedPgVectorStore",
    "MemoryFileStore",
    "PgMessageStore",
    "PgPool",
    "PgRetrievalTraceStore",
    "PgSessionStore",
    "initialize_schema",
]
