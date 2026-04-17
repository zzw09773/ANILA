"""pgvector-backed document chunk store and retrieval provider.

Implements:
  - RetrievalProvider Protocol  (search / index / delete_document)
  - DocumentStore Protocol      (store / retrieve / list_by_document / delete_document)

Schema (created by initialize_schema()):
  CREATE EXTENSION IF NOT EXISTS vector;
  CREATE TABLE document_chunks ( ... embedding vector(4096) ... );

Uses cosine distance (<=>)  for nearest-neighbour search.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from ...models.storage import DocumentChunk
from .pg_pool import PgPool

logger = logging.getLogger(__name__)

# DDL executed on first initialize_schema() call
_SCHEMA_SQL = """
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS document_chunks (
    chunk_id    TEXT PRIMARY KEY,
    document_id TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    project_id  TEXT NOT NULL,
    content     TEXT NOT NULL,
    embedding   vector(4096),
    metadata    JSONB DEFAULT '{}',
    created_at  TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_chunks_document
    ON document_chunks(document_id);

CREATE INDEX IF NOT EXISTS idx_chunks_project
    ON document_chunks(user_id, project_id);
"""

# IVFFlat index is created separately (requires rows to exist first)
_IVFFLAT_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_chunks_embedding
    ON document_chunks
    USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);
"""


class PgVectorStore:
    """Combined DocumentStore + RetrievalProvider backed by pgvector.

    Args:
        pool:      Shared PgPool instance.
        dimension: Embedding dimension (must match the vector column).
    """

    def __init__(self, pool: PgPool, dimension: int = 4096) -> None:
        self._pool = pool
        self._dimension = dimension

    async def initialize_schema(self, create_ivfflat: bool = False) -> None:
        """Create tables and indexes if they do not yet exist."""
        async with self._pool.acquire() as conn:
            await conn.execute(_SCHEMA_SQL)
            if create_ivfflat:
                try:
                    await conn.execute(_IVFFLAT_INDEX_SQL)
                except Exception as exc:
                    # IVFFlat requires at least some rows; log and skip.
                    logger.warning("IVFFlat index not created (may need data first): %s", exc)

    # ------------------------------------------------------------------
    # DocumentStore Protocol
    # ------------------------------------------------------------------

    async def store(self, chunk: DocumentChunk) -> None:
        """Upsert a document chunk (with optional embedding)."""
        embedding_value = _to_pg_vector(chunk.embedding)
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO document_chunks
                    (chunk_id, document_id, user_id, project_id,
                     content, embedding, metadata)
                VALUES ($1, $2, $3, $4, $5, $6::vector, $7::jsonb)
                ON CONFLICT (chunk_id) DO UPDATE SET
                    content    = EXCLUDED.content,
                    embedding  = EXCLUDED.embedding,
                    metadata   = EXCLUDED.metadata
                """,
                chunk.chunk_id,
                chunk.document_id,
                chunk.user_id,
                chunk.project_id,
                chunk.content,
                embedding_value,
                json.dumps(chunk.metadata),
            )

    async def retrieve(self, chunk_id: str) -> Optional[DocumentChunk]:
        """Return a chunk by ID, or None if not found."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM document_chunks WHERE chunk_id = $1",
                chunk_id,
            )
        return _row_to_chunk(row) if row else None

    async def list_by_document(
        self,
        document_id: str,
        user_id: Optional[str] = None,
        project_id: Optional[str] = None,
    ) -> list[DocumentChunk]:
        """Return all chunks for a document, ordered by creation time.

        When *user_id* and *project_id* are provided the query is scoped to
        that tenant only, preventing cross-tenant data access.
        """
        async with self._pool.acquire() as conn:
            if user_id is not None and project_id is not None:
                rows = await conn.fetch(
                    """
                    SELECT * FROM document_chunks
                    WHERE document_id = $1
                      AND user_id = $2
                      AND project_id = $3
                    ORDER BY created_at
                    """,
                    document_id,
                    user_id,
                    project_id,
                )
            else:
                rows = await conn.fetch(
                    "SELECT * FROM document_chunks WHERE document_id = $1 ORDER BY created_at",
                    document_id,
                )
        return [_row_to_chunk(r) for r in rows]

    async def list_all_documents(
        self,
        user_id: str = "default",
        project_id: str = "default",
    ) -> list[dict]:
        """Return one summary row per document_id, scoped to user_id + project_id.

        Each row contains:
            document_id, filename, source_path, title, chunk_count, last_indexed
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    document_id,
                    metadata->>'filename'    AS filename,
                    metadata->>'source_path' AS source_path,
                    metadata->>'title'       AS title,
                    COUNT(*)                 AS chunk_count,
                    MAX(created_at)          AS last_indexed
                FROM document_chunks
                WHERE user_id = $1 AND project_id = $2
                GROUP BY document_id,
                         metadata->>'filename',
                         metadata->>'source_path',
                         metadata->>'title'
                ORDER BY MAX(created_at) DESC
                """,
                user_id,
                project_id,
            )
        return [dict(r) for r in rows]

    async def delete_document(
        self,
        document_id: str,
        user_id: Optional[str] = None,
        project_id: Optional[str] = None,
    ) -> None:
        """Delete all chunks belonging to *document_id*.

        When *user_id* and *project_id* are provided, only chunks matching
        the scope are deleted — preventing cross-tenant deletions.
        """
        async with self._pool.acquire() as conn:
            if user_id is not None and project_id is not None:
                await conn.execute(
                    """
                    DELETE FROM document_chunks
                    WHERE document_id = $1
                      AND user_id = $2
                      AND project_id = $3
                    """,
                    document_id,
                    user_id,
                    project_id,
                )
            else:
                await conn.execute(
                    "DELETE FROM document_chunks WHERE document_id = $1",
                    document_id,
                )

    # ------------------------------------------------------------------
    # RetrievalProvider Protocol
    # ------------------------------------------------------------------

    async def search(
        self,
        query_embedding: list[float],
        user_id: str,
        project_id: str,
        top_k: int = 5,
        min_score: float = 0.0,
    ) -> list[DocumentChunk]:
        """Return top-k chunks nearest to *query_embedding* (cosine similarity)."""
        vector_str = _to_pg_vector(query_embedding)

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT *,
                       1 - (embedding <=> $1::vector) AS score
                FROM document_chunks
                WHERE user_id = $2 AND project_id = $3
                  AND embedding IS NOT NULL
                  AND 1 - (embedding <=> $1::vector) >= $4
                ORDER BY embedding <=> $1::vector
                LIMIT $5
                """,
                vector_str,
                user_id,
                project_id,
                min_score,
                top_k,
            )

        chunks: list[DocumentChunk] = []
        for row in rows:
            chunk = _row_to_chunk(row)
            chunk = chunk.model_copy(
                update={"metadata": {**chunk.metadata, "score": float(row["score"])}}
            )
            chunks.append(chunk)
        return chunks

    async def index(self, chunk: DocumentChunk) -> None:
        """Alias for store() — adds a chunk to the vector index."""
        await self.store(chunk)

    # delete_document is shared between DocumentStore and RetrievalProvider


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _to_pg_vector(values: Optional[list[float]]) -> Optional[str]:
    """Convert a Python float list to pgvector literal string '[v1,v2,...]'."""
    if values is None:
        return None
    return "[" + ",".join(str(v) for v in values) + "]"


def _row_to_chunk(row: Any) -> DocumentChunk:
    """Convert an asyncpg Record to a DocumentChunk."""
    metadata = row["metadata"] or {}
    if isinstance(metadata, str):
        metadata = json.loads(metadata)

    embedding: Optional[list[float]] = None
    raw_emb = row["embedding"]
    if raw_emb is not None:
        # asyncpg returns pgvector as a string like '[0.1,0.2,...]'
        if isinstance(raw_emb, str):
            embedding = [float(v) for v in raw_emb.strip("[]").split(",")]
        else:
            embedding = list(raw_emb)

    return DocumentChunk(
        chunk_id=row["chunk_id"],
        document_id=row["document_id"],
        user_id=row["user_id"],
        project_id=row["project_id"],
        content=row["content"],
        embedding=embedding,
        metadata=metadata,
        created_at=row["created_at"],
    )
