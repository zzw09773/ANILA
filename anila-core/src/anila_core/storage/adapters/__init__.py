"""anila-core storage adapters.

History:

- v0.5.0 boundary cleanup (anila-core-boundary.md §2.3) removed the
  RAG-flavour PG adapters: ``PgPool``, ``PgVectorStore``,
  ``PgSessionStore``, ``PgMessageStore``, ``PgRetrievalTraceStore``,
  ``initialize_schema``. They lived under the assumption that anila-core
  was both runtime AND data plane; the new boundary makes anila-core a
  pure runtime + central SDK.
- v0.6.0 (Phase 2 Sprint 1) re-introduces PG adapters under a new
  contract: agent-scoped, RLS-aware, alembic-managed. ``PgPool`` is back
  as a generic pool; ``AgentScopedPgVectorStore`` is the single sanctioned
  read/write path for ``document_chunks``.

What lives here today:

- ``PgPool`` — asyncpg pool wrapper with the pgvector codec auto-registered.
- ``AgentScopedPgVectorStore`` — agent-scoped, RLS-enforcing vector store
  (Layer 3 of the docs/ingestion-platform-design.md §3.3 defence stack).
- ``MemoryFileStore`` — filesystem MemoryStore impl used by the memdir /
  extract / relevance / consolidation modules. Platform memory infra; a
  PostgresMemoryStore impl will land in Phase 3+.
"""

from .memory_file_store import MemoryFileStore
from .pg_pool import PgPool
from .pgvector_store import (
    AgentScopedPgVectorStore,  # back-compat alias for one transition cycle.
    CollectionScopedPgVectorStore,
)

__all__ = [
    "AgentScopedPgVectorStore",
    "CollectionScopedPgVectorStore",
    "MemoryFileStore",
    "PgPool",
]
