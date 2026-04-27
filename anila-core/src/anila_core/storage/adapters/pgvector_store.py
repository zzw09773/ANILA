"""Collection-scoped pgvector store (docs/ingestion-platform-design.md §3.3 Layer 3).

Sprint 4 refactor renamed this class from ``AgentScopedPgVectorStore``
when the platform's ownership model moved from agent-scoped collections
to collection-as-first-class. The defence-in-depth shape is unchanged,
just keyed differently:

- Layer 1 (schema): ``document_chunks.collection_id`` is NOT NULL + FK
  (set in migration 0014; the legacy ``agent_id`` column was dropped in
  migration 0019).
- Layer 2 (RLS): ``CREATE POLICY chunks_collection_isolation`` filtering
  on ``current_setting('anila.collection_id')`` (migration 0019).
- **Layer 3 (this class)**: constructor refuses anything but a positive
  int ``collection_id``; every connection acquired by this store calls
  ``SET LOCAL anila.collection_id = $self._collection_id`` so RLS is
  automatically enforced for every query, even ones the developer
  forgot to scope.

Constructing this class without a valid ``collection_id`` is a
programming error — fail fast at construction so we never get to a
state where a caller could accidentally run an unscoped query.

Back-compat alias ``AgentScopedPgVectorStore`` is preserved for one
release cycle; consumers should switch to ``CollectionScopedPgVectorStore``.

Sprint 1 scope: index / search / list / delete. Sprint 2 adds hybrid
keyword + vector search; Sprint 3 adds evaluator-side bulk operations;
Sprint 4 dropped agent_id throughout.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, AsyncIterator

import asyncpg
from pgvector import HalfVector

from anila_core.ingestion.chunking_plugins.base import ChunkResult
from anila_core.ingestion.errors import StoreError
from anila_core.models.ingestion import IngestionChunk, SearchHit
from anila_core.storage.adapters.pg_pool import PgPool


class CollectionScopedPgVectorStore:
    """Read/write pgvector chunks under a single collection's RLS scope.

    Construct one per (collection, request)-style operation. Cheap to
    construct; the heavy resource (pool) is shared across instances.

    Concurrency model: each ``index_chunks`` / ``similarity_search``
    call acquires a fresh connection from the pool, sets
    ``anila.collection_id`` locally on that connection, and releases it.
    ``SET LOCAL`` is transactional, so as long as the connection isn't
    reused outside a transaction we get the right scoping. The internal
    ``_acquire`` context manager wraps everything in an explicit
    transaction to make that contract impossible to violate.
    """

    def __init__(self, pool: PgPool, collection_id: int) -> None:
        # Refuse anything that isn't clearly a positive Postgres BIGINT.
        # ``bool`` is a subclass of int in Python; rule it out so a stray
        # ``True`` doesn't silently scope to collection_id = 1.
        if isinstance(collection_id, bool) or not isinstance(collection_id, int):
            raise ValueError(
                f"collection_id must be a positive int, got "
                f"{type(collection_id).__name__} {collection_id!r}"
            )
        if collection_id <= 0:
            raise ValueError(f"collection_id must be > 0, got {collection_id}")
        self._pool = pool
        self._collection_id = collection_id

    @property
    def collection_id(self) -> int:
        return self._collection_id

    @asynccontextmanager
    async def _acquire(self) -> AsyncIterator[asyncpg.Connection]:
        """Acquire a connection scoped to this collection.

        Wraps the work in an explicit transaction because ``SET LOCAL``
        only persists within a transaction — without ``BEGIN``, asyncpg
        would auto-commit each statement and the GUC would be reset
        after the first query, defeating Layer 2.
        """
        async with self._pool.acquire() as conn:
            tr = conn.transaction()
            await tr.start()
            try:
                # ``SET LOCAL`` doesn't accept parameter binding ($1) —
                # Postgres parses it as a config command, not DML.
                # F-string is safe because ``__init__`` rejects non-int.
                await conn.execute(
                    f"SET LOCAL anila.collection_id = {int(self._collection_id)}"
                )
                yield conn
                await tr.commit()
            except BaseException:
                await tr.rollback()
                raise

    # ── Write path ──────────────────────────────────────────────────────────

    async def index_chunks(
        self,
        document_id: int,
        chunks: list[ChunkResult],
        embeddings: list[list[float]],
    ) -> int:
        """Bulk-insert chunks with their embeddings.

        Sprint 4: ``collection_id`` is no longer a per-call argument —
        the store is constructed against one collection, so all writes
        land in that scope. Pass ``document_id`` only.

        Returns the number of rows written. Caller-supplied
        ``len(chunks) == len(embeddings)`` is enforced.

        On constraint violation (duplicate ``chunk_key`` within the
        document) we re-raise the asyncpg error wrapped in
        ``StoreError`` so the worker's error taxonomy stays uniform.
        """
        if len(chunks) != len(embeddings):
            raise ValueError(
                f"index_chunks: got {len(chunks)} chunks but {len(embeddings)} "
                f"embeddings; counts must match"
            )
        if not chunks:
            return 0

        # JSONB codec is registered on every connection (PgPool.
        # _init_connection), so asyncpg encodes the dict to JSONB
        # on its own — passing a ``json.dumps`` string would
        # double-encode. HalfVector wraps the float list so the halfvec
        # codec ships the right binary shape into the halfvec(4000)
        # column; a bare list[float] would be interpreted as ``vector``
        # and rejected as a type mismatch.
        rows = [
            (
                self._collection_id,
                document_id,
                ch.chunk_key,
                ch.content,
                HalfVector(emb),
                ch.metadata,
                ch.token_count,
            )
            for ch, emb in zip(chunks, embeddings)
        ]

        sql = """
            INSERT INTO document_chunks
                (collection_id, document_id, chunk_key,
                 content, content_tsv, embedding, metadata, token_count)
            VALUES
                ($1, $2, $3, $4,
                 to_tsvector('simple', $4),
                 $5, $6, $7)
        """
        try:
            async with self._acquire() as conn:
                await conn.executemany(sql, rows)
        except asyncpg.ConnectionDoesNotExistError as e:
            raise StoreError.pg_connect(
                user_message="資料庫連線中斷，請稍後再試",
                details={"cause": type(e).__name__},
            ) from e
        return len(rows)

    # ── Read path ───────────────────────────────────────────────────────────

    async def similarity_search(
        self,
        query_embedding: list[float],
        top_k: int = 10,
        min_score: float = 0.0,
    ) -> list[SearchHit]:
        """Vector similarity search scoped to this collection.

        Sprint 4: dropped the legacy ``collection_id`` per-call argument
        — the store IS the collection scope. RLS auto-filters via
        ``anila.collection_id`` GUC set inside ``_acquire()``.

        Cosine *similarity* is what we return (1 - cosine_distance), so
        ``min_score`` reads naturally: 0.7 = "at least 70% similar".
        """
        if top_k <= 0:
            return []

        # The ANN index is HNSW on ``embedding halfvec_cosine_ops``;
        # ``<=>`` is cosine distance. We compute 1 - distance for the
        # score column so callers can read higher-is-closer.
        q = HalfVector(query_embedding)
        sql = """
            SELECT id, collection_id, document_id, chunk_key,
                   content, metadata, token_count, created_at,
                   1 - (embedding <=> $1) AS score
              FROM document_chunks
             WHERE 1 - (embedding <=> $1) >= $2
             ORDER BY embedding <=> $1
             LIMIT $3
        """
        async with self._acquire() as conn:
            rows = await conn.fetch(sql, q, min_score, top_k)
        return [self._row_to_search_hit(r) for r in rows]

    async def keyword_search(
        self,
        query: str,
        top_k: int = 10,
        tokenized_query: str | None = None,
    ) -> list[SearchHit]:
        """Full-text keyword search via the GIN index on ``content_tsv``.

        Sprint 4: dropped the legacy ``collection_id`` per-call argument.

        Two ranking modes:

        - When ``tokenized_query`` is provided (e.g. CJK pre-tokenized
          input), use ``plainto_tsquery`` on it and rank by
          ``ts_rank_cd``.
        - When only ``query`` is given, use ``plainto_tsquery`` directly
          on the raw user text. Works fine for whitespace-tokenised
          languages; CJK callers should pre-tokenise.

        ``score`` is ``ts_rank_cd`` — NOT a [0,1] cosine. Callers that
        mix this with ``similarity_search`` results need to merge by
        rank position (RRF) rather than score axis.
        """
        if top_k <= 0:
            return []

        tsquery_input = tokenized_query if tokenized_query else query
        sql = """
            SELECT id, collection_id, document_id, chunk_key,
                   content, metadata, token_count, created_at,
                   ts_rank_cd(content_tsv, q) AS score
              FROM document_chunks,
                   plainto_tsquery('simple', $1) q
             WHERE content_tsv @@ q
             ORDER BY score DESC
             LIMIT $2
        """
        async with self._acquire() as conn:
            rows = await conn.fetch(sql, tsquery_input, top_k)
        return [self._row_to_search_hit(r) for r in rows]

    async def list_by_document(
        self,
        document_id: int,
        limit: int = 100,
        offset: int = 0,
        include_embedding: bool = False,
    ) -> list[IngestionChunk]:
        """Inspector-side: list chunks belonging to one document.

        ``include_embedding`` defaults False because the inspector UI
        doesn't render the 1536-d vector — sending it bloats the payload.
        Set True only when the dev explicitly asks (e.g. embedding-norm
        debug column behind the inspector's "show vector debug" toggle).
        """
        cols = (
            "id, collection_id, document_id, chunk_key, content, "
            "embedding, metadata, token_count, created_at"
            if include_embedding
            else "id, collection_id, document_id, chunk_key, content, "
            "metadata, token_count, created_at"
        )
        sql = f"""
            SELECT {cols}
              FROM document_chunks
             WHERE document_id = $1
             ORDER BY id
             LIMIT $2 OFFSET $3
        """
        async with self._acquire() as conn:
            rows = await conn.fetch(sql, document_id, limit, offset)
        return [self._row_to_chunk(r, include_embedding=include_embedding) for r in rows]

    async def list_in_collection(
        self,
        limit: int = 100,
        offset: int = 0,
    ) -> list[IngestionChunk]:
        """Inspector-side: paginated list of chunks in this collection.

        Sprint 4 rename (was ``list_by_collection``): the store IS the
        collection, so the parameter is implicit. RLS does the filtering.
        """
        sql = """
            SELECT id, collection_id, document_id, chunk_key,
                   content, metadata, token_count, created_at
              FROM document_chunks
             ORDER BY id
             LIMIT $1 OFFSET $2
        """
        async with self._acquire() as conn:
            rows = await conn.fetch(sql, limit, offset)
        return [self._row_to_chunk(r, include_embedding=False) for r in rows]

    # ── Delete path ─────────────────────────────────────────────────────────

    async def delete_document(self, document_id: int) -> int:
        """Delete every chunk for one document in this collection."""
        sql = "DELETE FROM document_chunks WHERE document_id = $1"
        async with self._acquire() as conn:
            result = await conn.execute(sql, document_id)
        return int(result.split()[-1]) if result.startswith("DELETE ") else 0

    async def delete_all(self) -> int:
        """Delete every chunk in this collection. Returns count deleted.

        Sprint 4 rename (was ``delete_collection``): RLS scopes us to
        the construction-time collection automatically; the SQL no
        longer carries an explicit collection_id.
        """
        sql = "DELETE FROM document_chunks"
        async with self._acquire() as conn:
            result = await conn.execute(sql)
        return int(result.split()[-1]) if result.startswith("DELETE ") else 0

    # ── Row mappers ─────────────────────────────────────────────────────────

    @staticmethod
    def _row_to_chunk(row: asyncpg.Record, *, include_embedding: bool) -> IngestionChunk:
        # JSONB codec parses asynchronously into a dict. Defaults to {}
        # in the schema so this can never be NULL, but None-guard regardless.
        metadata = row["metadata"] or {}
        return IngestionChunk(
            id=row["id"],
            collection_id=row["collection_id"],
            document_id=row["document_id"],
            chunk_key=row["chunk_key"],
            content=row["content"],
            embedding=list(row["embedding"]) if include_embedding else None,
            metadata=metadata,
            token_count=row["token_count"],
            created_at=row["created_at"]
            if isinstance(row["created_at"], datetime)
            else datetime.fromisoformat(str(row["created_at"])),
        )

    @classmethod
    def _row_to_search_hit(cls, row: asyncpg.Record) -> SearchHit:
        # similarity_search SELECT does not return the embedding column.
        return SearchHit(
            chunk=IngestionChunk(
                id=row["id"],
                collection_id=row["collection_id"],
                document_id=row["document_id"],
                chunk_key=row["chunk_key"],
                content=row["content"],
                metadata=row["metadata"] or {},
                token_count=row["token_count"],
                created_at=row["created_at"],
            ),
            score=float(row["score"]),
        )


# ── Back-compat alias ───────────────────────────────────────────────────────
# Sprint 4 renamed the class from AgentScopedPgVectorStore. Existing
# imports survive one cycle through this alias; new code should use
# CollectionScopedPgVectorStore directly.
AgentScopedPgVectorStore = CollectionScopedPgVectorStore
