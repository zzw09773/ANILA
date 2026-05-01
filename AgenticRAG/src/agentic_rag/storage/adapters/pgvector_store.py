"""Collection-scoped pgvector store.

Phase 0 decoupling (2026-05-02): copied back into AgenticRAG from
``anila_core.storage.adapters.pgvector_store``. Originally lived here
in v0.5; was centralised into anila-core in v0.6 / Sprint 1 Chunk F.
AgenticRAG now owns its own copy so the fork-template promise holds.

Defence-in-depth shape (all three layers must agree for safety):

- Layer 1 (schema): ``document_chunks.collection_id`` is NOT NULL + FK
  (set in CSP migration 0014; legacy ``agent_id`` dropped in 0019).
- Layer 2 (RLS): ``CREATE POLICY chunks_collection_isolation`` filtering
  on ``current_setting('anila.collection_id')`` (migration 0019).
- **Layer 3 (this class)**: constructor refuses anything but a positive
  int ``collection_id``; every connection acquired calls
  ``SET LOCAL anila.collection_id = $self._collection_id`` so RLS is
  enforced even for callers that forgot to scope.

Constructing without a valid ``collection_id`` is a programming error
— fail fast at construction so we never reach a state where an
unscoped query could leak data.

Sprint 9 X / parent-child:
``add_parent_chunks`` inserts heading / document parent rows
(no embedding); ``index_chunks`` resolves ``parent_chunk_key`` from
metadata into ``parent_chunk_id`` via the returned mapping;
``similarity_search`` / ``keyword_search`` filter to ``chunk_type='leaf'``
and JOIN-fetch parent content into ``SearchHit.parent_content``.

Back-compat alias ``AgentScopedPgVectorStore`` is preserved at the
bottom for callers still on the v0.5 name.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from typing import AsyncIterator

import asyncpg
from pgvector import HalfVector

from ...ingestion.chunking_types import ChunkResult
from ...ingestion.errors import StoreError
from ...models.ingestion import IngestionChunk, SearchHit
from .pg_pool import PgPool


class CollectionScopedPgVectorStore:
    """Read/write pgvector chunks under a single collection's RLS scope.

    Construct one per (collection, request)-style operation. Cheap to
    construct; the heavy resource (pool) is shared across instances.

    Concurrency model: each call acquires a fresh connection, sets
    ``anila.collection_id`` locally on it, and releases. ``SET LOCAL``
    is transactional, so the internal ``_acquire`` context manager
    wraps every operation in an explicit transaction — making it
    impossible to accidentally use the connection without RLS scope.
    """

    def __init__(self, pool: PgPool, collection_id: int) -> None:
        # Refuse anything that isn't clearly a positive Postgres BIGINT.
        # ``bool`` is a subclass of int in Python; rule it out so a
        # stray ``True`` doesn't silently scope to collection_id = 1.
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

        ``SET LOCAL`` only persists within a transaction — without
        ``BEGIN``, asyncpg auto-commits and the GUC resets after the
        first statement, defeating Layer 2.
        """
        async with self._pool.acquire() as conn:
            tr = conn.transaction()
            await tr.start()
            try:
                # ``SET LOCAL`` doesn't accept parameter binding ($1) —
                # Postgres parses it as a config command. F-string is
                # safe because ``__init__`` rejects non-int.
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
        parent_id_map: dict[str, int] | None = None,
    ) -> int:
        """Bulk-insert leaf chunks with their embeddings.

        Sprint 9 X / parent-child: each chunk's metadata may carry
        ``parent_chunk_key`` referring to a previously-inserted parent
        row. ``parent_id_map`` (returned from ``add_parent_chunks``)
        translates those keys into FK ids for the ``parent_chunk_id``
        column. Missing references silently become NULL.

        Returns the number of rows written. Caller-supplied
        ``len(chunks) == len(embeddings)`` is enforced.
        """
        if len(chunks) != len(embeddings):
            raise ValueError(
                f"index_chunks: got {len(chunks)} chunks but {len(embeddings)} "
                f"embeddings; counts must match"
            )
        if not chunks:
            return 0

        parent_id_map = parent_id_map or {}

        # JSONB codec is registered per-connection (PgPool._init_connection),
        # so asyncpg encodes the dict to JSONB on its own. HalfVector wraps
        # the float list to ship the right binary shape into halfvec(4000);
        # a bare list[float] would be interpreted as ``vector`` and rejected
        # as a type mismatch.
        rows = []
        for ch, emb in zip(chunks, embeddings):
            meta = ch.metadata or {}
            chunk_type = meta.get("chunk_type", "leaf")
            chunk_level = int(meta.get("chunk_level", 0))
            parent_key = meta.get("parent_chunk_key")
            parent_id = parent_id_map.get(parent_key) if parent_key else None
            rows.append(
                (
                    self._collection_id,
                    document_id,
                    ch.chunk_key,
                    ch.content,
                    HalfVector(emb),
                    meta,
                    ch.token_count,
                    chunk_type,
                    chunk_level,
                    parent_id,
                )
            )

        sql = """
            INSERT INTO document_chunks
                (collection_id, document_id, chunk_key,
                 content, content_tsv, embedding, metadata, token_count,
                 chunk_type, chunk_level, parent_chunk_id)
            VALUES
                ($1, $2, $3, $4,
                 to_tsvector('simple', $4),
                 $5, $6, $7, $8, $9, $10)
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

    async def add_parent_chunks(
        self,
        document_id: int,
        chunks: list,
    ) -> dict[str, int]:
        """Sprint 9 X / parent-child — insert non-leaf rows (no embedding).

        Hierarchical chunkers emit ``chunk_type='heading'`` /
        ``'document'`` rows alongside leaves. Parents have no embedding;
        their job is to be JOIN-fetched as ``parent_content`` at
        retrieval time.

        Returns ``{chunk_key: db_id}`` so the caller can resolve
        ``parent_chunk_key`` references on the leaf rows it inserts
        next via the regular ``index_chunks`` path.

        We use ``RETURNING id, chunk_key`` per row so the mapping
        comes back in one round-trip; ``executemany`` doesn't surface
        RETURNING values, hence the per-row loop.
        """
        if not chunks:
            return {}

        sql = """
            INSERT INTO document_chunks
                (collection_id, document_id, chunk_key,
                 content, content_tsv, embedding, metadata, token_count,
                 chunk_type, chunk_level, parent_chunk_id)
            VALUES
                ($1, $2, $3, $4,
                 to_tsvector('simple', $4),
                 NULL, $5, $6, $7, $8, NULL)
            RETURNING id, chunk_key
        """
        out: dict[str, int] = {}
        try:
            async with self._acquire() as conn:
                async with conn.transaction():
                    for ch in chunks:
                        meta = ch.metadata or {}
                        chunk_type = meta.get("chunk_type", "heading")
                        chunk_level = int(meta.get("chunk_level", 0))
                        row = await conn.fetchrow(
                            sql,
                            self._collection_id,
                            document_id,
                            ch.chunk_key,
                            ch.content,
                            meta,
                            ch.token_count,
                            chunk_type,
                            chunk_level,
                        )
                        out[row["chunk_key"]] = row["id"]
        except asyncpg.ConnectionDoesNotExistError as e:
            raise StoreError.pg_connect(
                user_message="資料庫連線中斷，請稍後再試",
                details={"cause": type(e).__name__},
            ) from e
        return out

    # ── Read path ───────────────────────────────────────────────────────────

    async def similarity_search(
        self,
        query_embedding: list[float],
        top_k: int = 10,
        min_score: float = 0.0,
    ) -> list[SearchHit]:
        """Vector similarity search scoped to this collection.

        Cosine *similarity* (1 - cosine_distance), so ``min_score``
        reads naturally: 0.7 = "at least 70% similar".

        Sprint 9 X: filtered to ``chunk_type='leaf'``. Heading /
        document parents have ``embedding=NULL`` so they wouldn't match
        anyway, but the explicit filter lets the planner skip them.
        """
        if top_k <= 0:
            return []

        q = HalfVector(query_embedding)
        sql = """
            SELECT id, collection_id, document_id, chunk_key,
                   content, metadata, token_count, created_at,
                   parent_chunk_id, chunk_type, chunk_level,
                   1 - (embedding <=> $1) AS score
              FROM document_chunks
             WHERE chunk_type = 'leaf'
               AND 1 - (embedding <=> $1) >= $2
             ORDER BY embedding <=> $1
             LIMIT $3
        """
        async with self._acquire() as conn:
            rows = await conn.fetch(sql, q, min_score, top_k)
            hits = [self._row_to_search_hit(r) for r in rows]
            await self._attach_parent_content(conn, hits)
        return hits

    async def keyword_search(
        self,
        query: str,
        top_k: int = 10,
        tokenized_query: str | None = None,
    ) -> list[SearchHit]:
        """Full-text keyword search via the GIN index on ``content_tsv``.

        Two ranking modes:

        - When ``tokenized_query`` is provided (e.g. CJK pre-tokenised),
          rank against it via ``plainto_tsquery``.
        - When only ``query`` is given, ``plainto_tsquery`` on raw text.

        ``score`` is ``ts_rank_cd`` — NOT a [0,1] cosine. Mixing this
        with similarity_search results requires merging by rank
        position (RRF), not by score axis.
        """
        if top_k <= 0:
            return []

        tsquery_input = tokenized_query if tokenized_query else query
        sql = """
            SELECT id, collection_id, document_id, chunk_key,
                   content, metadata, token_count, created_at,
                   parent_chunk_id, chunk_type, chunk_level,
                   ts_rank_cd(content_tsv, q) AS score
              FROM document_chunks,
                   plainto_tsquery('simple', $1) q
             WHERE chunk_type = 'leaf'
               AND content_tsv @@ q
             ORDER BY score DESC
             LIMIT $2
        """
        async with self._acquire() as conn:
            rows = await conn.fetch(sql, tsquery_input, top_k)
            hits = [self._row_to_search_hit(r) for r in rows]
            await self._attach_parent_content(conn, hits)
        return hits

    async def _attach_parent_content(
        self,
        conn,
        hits: list,
    ) -> None:
        """Sprint 9 X — fill ``hit.parent_content`` from the parent row.

        Single ``id = ANY($1::bigint[])`` round-trip pulls every
        unique parent in one shot. Hits whose parent_chunk_id is NULL
        (root rows / legacy leaves) keep ``parent_content=None``.
        """
        if not hits:
            return
        parent_ids = {
            h.chunk.parent_chunk_id
            for h in hits
            if getattr(h.chunk, "parent_chunk_id", None)
        }
        if not parent_ids:
            return
        rows = await conn.fetch(
            """
            SELECT id, content
              FROM document_chunks
             WHERE id = ANY($1::bigint[])
               AND collection_id = $2
            """,
            list(parent_ids),
            self._collection_id,
        )
        parent_map = {r["id"]: r["content"] for r in rows}
        for h in hits:
            pid = getattr(h.chunk, "parent_chunk_id", None)
            if pid and pid in parent_map:
                h.parent_content = parent_map[pid]

    async def list_by_document(
        self,
        document_id: int,
        limit: int = 100,
        offset: int = 0,
        include_embedding: bool = False,
    ) -> list[IngestionChunk]:
        """Inspector-side: list chunks belonging to one document.

        ``include_embedding`` defaults False because the inspector UI
        doesn't render the 1536-d vector. Set True only when explicitly
        asked.
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
        """Inspector-side: paginated list of chunks in this collection."""
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

        RLS scopes us to the construction-time collection automatically;
        the SQL no longer carries an explicit collection_id.
        """
        sql = "DELETE FROM document_chunks"
        async with self._acquire() as conn:
            result = await conn.execute(sql)
        return int(result.split()[-1]) if result.startswith("DELETE ") else 0

    # ── Row mappers ─────────────────────────────────────────────────────────

    @staticmethod
    def _row_to_chunk(row: asyncpg.Record, *, include_embedding: bool) -> IngestionChunk:
        # JSONB codec parses to dict. Schema defaults to {} so this can't
        # be NULL, but None-guard anyway.
        metadata = row["metadata"] or {}

        # Sprint 9 X columns are optional — some list endpoints don't
        # SELECT them. ``Record.get`` doesn't exist on asyncpg.Record;
        # use try/except for missing keys.
        def _opt(name: str, default=None):
            try:
                return row[name]
            except (KeyError, IndexError):
                return default

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
            parent_chunk_id=_opt("parent_chunk_id"),
            chunk_type=_opt("chunk_type", "leaf") or "leaf",
            chunk_level=_opt("chunk_level", 0) or 0,
        )

    @classmethod
    def _row_to_search_hit(cls, row: asyncpg.Record) -> SearchHit:
        # similarity_search SELECT does not return the embedding column.
        def _opt(name: str, default=None):
            try:
                return row[name]
            except (KeyError, IndexError):
                return default

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
                parent_chunk_id=_opt("parent_chunk_id"),
                chunk_type=_opt("chunk_type", "leaf") or "leaf",
                chunk_level=_opt("chunk_level", 0) or 0,
            ),
            score=float(row["score"]),
        )


# ── Back-compat alias ───────────────────────────────────────────────────────
# v0.5 used AgentScopedPgVectorStore; current callers should use
# CollectionScopedPgVectorStore. Alias preserved one cycle.
AgentScopedPgVectorStore = CollectionScopedPgVectorStore
