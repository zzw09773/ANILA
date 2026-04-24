"""pgvector-backed document chunk store and retrieval provider.

Implements:
  - RetrievalProvider Protocol  (search / index / delete_document)
  - DocumentStore Protocol      (store / retrieve / list_by_document / delete_document)

Schema (created by initialize_schema()):
  CREATE EXTENSION IF NOT EXISTS vector;
  CREATE TABLE document_chunks ( ... hierarchical columns ... );

Hierarchy columns:
  - parent_chunk_id: self-referencing link (NULL for document root)
  - chunk_level:     depth from the root (0 = doc, 1 = H1, 2 = H2, ...)
  - chunk_type:      'document' | 'heading' | 'content' | 'image' | 'table'
  - heading_path:    JSONB array of ancestor heading titles

Only ``content`` and ``image`` nodes have an embedding and participate
in vector search. ``search()`` returns ``Citation`` objects with
cosine-similarity confidence and, by default, the enclosing parent's
full content for context expansion.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from ...ingestion.tokenize_zh import tokenize
from ...models.storage import ChunkType, Citation, DocumentChunk
from .pg_pool import PgPool

logger = logging.getLogger(__name__)

# DDL executed on every initialize_schema() call — drops + recreates.
# Callers must re-ingest their corpus after upgrading.
_SCHEMA_SQL = """
CREATE EXTENSION IF NOT EXISTS vector;

DROP TABLE IF EXISTS document_chunks CASCADE;

CREATE TABLE document_chunks (
    chunk_id         TEXT PRIMARY KEY,
    document_id      TEXT NOT NULL,
    user_id          TEXT NOT NULL,
    project_id       TEXT NOT NULL,
    parent_chunk_id  TEXT REFERENCES document_chunks(chunk_id) ON DELETE CASCADE,
    chunk_level      SMALLINT NOT NULL DEFAULT 0,
    chunk_type       TEXT NOT NULL DEFAULT 'content',
    heading_path     JSONB NOT NULL DEFAULT '[]',
    content          TEXT NOT NULL,
    content_tsv      tsvector,
    embedding        vector({dim}),
    metadata         JSONB NOT NULL DEFAULT '{{}}',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_chunks_document
    ON document_chunks(document_id);

CREATE INDEX idx_chunks_project
    ON document_chunks(user_id, project_id);

CREATE INDEX idx_chunks_parent
    ON document_chunks(parent_chunk_id);

CREATE INDEX idx_chunks_type_level
    ON document_chunks(chunk_type, chunk_level);

CREATE INDEX idx_chunks_content_tsv
    ON document_chunks
    USING GIN (content_tsv);
"""

# Idempotent upgrade for existing deployments — adds tsvector column and
# GIN index without dropping data. Callers should re-tokenize old rows
# (UPDATE ... SET content_tsv = ...) after running this.
_FTS_UPGRADE_SQL = """
ALTER TABLE document_chunks
    ADD COLUMN IF NOT EXISTS content_tsv tsvector;

CREATE INDEX IF NOT EXISTS idx_chunks_content_tsv
    ON document_chunks
    USING GIN (content_tsv);
"""

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
        """Create (or recreate) tables and indexes.

        WARNING: this drops ``document_chunks`` — callers upgrading an
        existing deployment must re-ingest their corpus afterwards.
        """
        ddl = _SCHEMA_SQL.format(dim=self._dimension)
        async with self._pool.acquire() as conn:
            await conn.execute(ddl)
            if create_ivfflat:
                try:
                    await conn.execute(_IVFFLAT_INDEX_SQL)
                except Exception as exc:
                    logger.warning(
                        "IVFFlat index not created (may need data first): %s", exc
                    )

    async def ensure_fts_schema(self) -> None:
        """Idempotent upgrade: add ``content_tsv`` column + GIN index.

        Use on existing deployments to enable FTS without dropping data.
        After running this, call ``backfill_content_tsv()`` to tokenize
        existing rows.
        """
        async with self._pool.acquire() as conn:
            await conn.execute(_FTS_UPGRADE_SQL)

    async def backfill_content_tsv(self, batch_size: int = 500) -> int:
        """Re-tokenize rows whose ``content_tsv`` is NULL. Returns count."""
        updated = 0
        async with self._pool.acquire() as conn:
            while True:
                rows = await conn.fetch(
                    """
                    SELECT chunk_id, content
                    FROM document_chunks
                    WHERE content_tsv IS NULL
                    LIMIT $1
                    """,
                    batch_size,
                )
                if not rows:
                    break
                for row in rows:
                    tokens = tokenize(row["content"] or "")
                    await conn.execute(
                        """
                        UPDATE document_chunks
                        SET content_tsv = to_tsvector('simple', $2)
                        WHERE chunk_id = $1
                        """,
                        row["chunk_id"],
                        tokens,
                    )
                    updated += 1
        return updated

    # ------------------------------------------------------------------
    # DocumentStore Protocol
    # ------------------------------------------------------------------

    async def store(self, chunk: DocumentChunk) -> None:
        """Upsert a document chunk (with optional embedding)."""
        embedding_value = _to_pg_vector(chunk.embedding)
        chunk_type_value = (
            chunk.chunk_type.value
            if isinstance(chunk.chunk_type, ChunkType)
            else str(chunk.chunk_type)
        )
        tokenized = tokenize(chunk.content or "")
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO document_chunks
                    (chunk_id, document_id, user_id, project_id,
                     parent_chunk_id, chunk_level, chunk_type, heading_path,
                     content, content_tsv, embedding, metadata)
                VALUES ($1, $2, $3, $4,
                        $5, $6, $7, $8::jsonb,
                        $9, to_tsvector('simple', $10), $11::vector, $12::jsonb)
                ON CONFLICT (chunk_id) DO UPDATE SET
                    parent_chunk_id = EXCLUDED.parent_chunk_id,
                    chunk_level     = EXCLUDED.chunk_level,
                    chunk_type      = EXCLUDED.chunk_type,
                    heading_path    = EXCLUDED.heading_path,
                    content         = EXCLUDED.content,
                    content_tsv     = EXCLUDED.content_tsv,
                    embedding       = EXCLUDED.embedding,
                    metadata        = EXCLUDED.metadata
                """,
                chunk.chunk_id,
                chunk.document_id,
                chunk.user_id,
                chunk.project_id,
                chunk.parent_chunk_id,
                chunk.chunk_level,
                chunk_type_value,
                json.dumps(chunk.heading_path),
                chunk.content,
                tokenized,
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
        """Return all chunks for a document, ordered by tree depth."""
        async with self._pool.acquire() as conn:
            if user_id is not None and project_id is not None:
                rows = await conn.fetch(
                    """
                    SELECT * FROM document_chunks
                    WHERE document_id = $1
                      AND user_id = $2
                      AND project_id = $3
                    ORDER BY chunk_level, created_at
                    """,
                    document_id,
                    user_id,
                    project_id,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT * FROM document_chunks
                    WHERE document_id = $1
                    ORDER BY chunk_level, created_at
                    """,
                    document_id,
                )
        return [_row_to_chunk(r) for r in rows]

    async def list_all_documents(
        self,
        user_id: str = "default",
        project_id: str = "default",
    ) -> list[dict]:
        """Return one summary row per document_id, scoped to user_id + project_id."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    document_id,
                    metadata->>'filename'    AS filename,
                    metadata->>'source_path' AS source_path,
                    metadata->>'title'       AS title,
                    COUNT(*) FILTER (WHERE chunk_type IN ('content', 'image'))
                        AS leaf_count,
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
        """Delete all chunks belonging to *document_id*."""
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
        include_parent_context: bool = True,
    ) -> list[Citation]:
        """Return top-k ``Citation`` results for *query_embedding*.

        Only leaf chunks (chunk_type in ('content', 'image')) participate
        in the similarity search. When ``include_parent_context`` is
        True, each citation is enriched with the parent chunk's full
        content — useful for feeding an LLM with broader context than
        the matched paragraph alone.
        """
        vector_str = _to_pg_vector(query_embedding)

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    c.*,
                    1 - (c.embedding <=> $1::vector) AS score,
                    p.content      AS parent_content_full,
                    p.chunk_type   AS parent_chunk_type
                FROM document_chunks c
                LEFT JOIN document_chunks p
                       ON p.chunk_id = c.parent_chunk_id
                WHERE c.user_id = $2
                  AND c.project_id = $3
                  AND c.embedding IS NOT NULL
                  AND c.chunk_type IN ('content', 'image')
                  AND 1 - (c.embedding <=> $1::vector) >= $4
                ORDER BY c.embedding <=> $1::vector
                LIMIT $5
                """,
                vector_str,
                user_id,
                project_id,
                min_score,
                top_k,
            )

        citations: list[Citation] = []
        for row in rows:
            chunk = _row_to_chunk(row)
            metadata = chunk.metadata or {}
            confidence = _clamp01(float(row["score"]))
            parent_content = (
                row["parent_content_full"]
                if include_parent_context and row["parent_content_full"] is not None
                else ""
            )

            citations.append(
                Citation(
                    chunk_id=chunk.chunk_id,
                    document_id=chunk.document_id,
                    document_title=str(metadata.get("title") or ""),
                    source_path=str(metadata.get("source_path") or ""),
                    format=str(metadata.get("format") or ""),
                    chunk_type=chunk.chunk_type,
                    chunk_level=chunk.chunk_level,
                    heading_path=list(chunk.heading_path or []),
                    page=metadata.get("page"),
                    content=chunk.content,
                    parent_chunk_id=chunk.parent_chunk_id,
                    parent_content=parent_content or "",
                    confidence=confidence,
                    metadata=metadata,
                )
            )
        return citations

    async def index(self, chunk: DocumentChunk) -> None:
        """Alias for store() — adds a chunk to the vector index."""
        await self.store(chunk)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _to_pg_vector(values: Optional[list[float]]) -> Optional[str]:
    """Convert a Python float list to pgvector literal string '[v1,v2,...]'."""
    if values is None:
        return None
    return "[" + ",".join(str(v) for v in values) + "]"


def _clamp01(x: float) -> float:
    if x < 0.0:
        return 0.0
    if x > 1.0:
        return 1.0
    return x


def _row_to_chunk(row: Any) -> DocumentChunk:
    """Convert an asyncpg Record to a DocumentChunk."""
    keys = row.keys()

    metadata = row["metadata"] if "metadata" in keys else {}
    if metadata is None:
        metadata = {}
    if isinstance(metadata, str):
        metadata = json.loads(metadata)

    heading_path: Any = row["heading_path"] if "heading_path" in keys else []
    if isinstance(heading_path, str):
        heading_path = json.loads(heading_path)
    if heading_path is None:
        heading_path = []

    embedding: Optional[list[float]] = None
    raw_emb = row["embedding"] if "embedding" in keys else None
    if raw_emb is not None:
        if isinstance(raw_emb, str):
            embedding = [float(v) for v in raw_emb.strip("[]").split(",")]
        else:
            embedding = list(raw_emb)

    chunk_type_raw = row["chunk_type"] if "chunk_type" in keys else "content"
    try:
        chunk_type = ChunkType(chunk_type_raw)
    except ValueError:
        chunk_type = ChunkType.CONTENT

    return DocumentChunk(
        chunk_id=row["chunk_id"],
        document_id=row["document_id"],
        user_id=row["user_id"],
        project_id=row["project_id"],
        parent_chunk_id=row["parent_chunk_id"] if "parent_chunk_id" in keys else None,
        chunk_level=int(row["chunk_level"]) if "chunk_level" in keys else 0,
        chunk_type=chunk_type,
        heading_path=list(heading_path),
        content=row["content"],
        embedding=embedding,
        metadata=metadata,
        created_at=row["created_at"],
    )
