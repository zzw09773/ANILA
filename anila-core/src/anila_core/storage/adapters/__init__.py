"""anila-core storage adapters.

Sprint 1 boundary cleanup (anila-core-boundary.md §2.3) removed the
PostgreSQL-specific adapters that used to live here:

    - PgPool (asyncpg connection pool)
    - PgVectorStore (vector store backed by pgvector)
    - PgSessionStore / PgMessageStore / PgRetrievalTraceStore
    - initialize_schema (RAG schema bootstrap)

These were RAG-specific data plane code and have moved to the
AgenticRAG template / future Ingestion Platform service. The
``storage/ports.py`` Protocols stay in core — they're the interface
contract any future backend (qdrant / milvus / chroma) implements.

What still lives here:

- ``MemoryFileStore``: filesystem MemoryStore implementation used by
  the memdir / extract / relevance / consolidation modules in
  ``anila_core/memory/``. Not RAG; this is platform memory infra. A
  PostgresMemoryStore impl will land in Phase 3+.
"""

from .memory_file_store import MemoryFileStore

__all__ = ["MemoryFileStore"]
