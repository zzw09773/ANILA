"""Agent-scoped pgvector store (docs/ingestion-platform-design.md §3.3 Layer 3).

The single sanctioned write/read path into ``document_chunks``. Per the
4-layer defence:

- Layer 1 (schema): ``agent_id NOT NULL`` + CHECK constraint, set in
  migration 0014.
- Layer 2 (RLS): ``CREATE POLICY chunks_agent_isolation`` filtering on
  ``current_setting('anila.agent_id')``, also from migration 0014.
- **Layer 3 (this class)**: constructor refuses anything but a positive
  int ``agent_id``; every connection acquired by this store calls
  ``SET LOCAL anila.agent_id = $self._agent_id`` so RLS is automatically
  enforced for every query, even ones the developer forgot to scope.

Constructing this class without a valid ``agent_id`` is a programming
error — fail fast at construction so we never get to a state where a
caller could accidentally run an unscoped query.

Sprint 1 scope: index / search / list / delete. Sprint 2 adds hybrid
keyword + vector search; Sprint 3 adds evaluator-side bulk operations.
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


class AgentScopedPgVectorStore:
    """Read/write pgvector chunks under a single agent's RLS scope.

    Construct one per (agent, request)-style operation. Cheap to
    construct; the heavy resource (pool) is shared across instances.

    Concurrency model: each ``index_chunks`` / ``similarity_search``
    call acquires a fresh connection from the pool, sets ``anila.agent_id``
    locally on that connection, and releases it. ``SET LOCAL`` is
    transactional, so as long as the connection isn't reused outside a
    transaction we get the right scoping. The internal ``_acquire``
    context manager wraps everything in an explicit transaction to make
    that contract impossible to violate.
    """

    def __init__(self, pool: PgPool, agent_id: int) -> None:
        # Refuse anything that isn't clearly a positive Postgres BIGINT.
        # `bool` is a subclass of `int`, so explicitly rule it out — a
        # caller passing ``True`` (the result of some accidental boolean)
        # must not silently scope to agent_id = 1.
        if isinstance(agent_id, bool) or not isinstance(agent_id, int):
            raise ValueError(
                f"agent_id must be a positive int, got {type(agent_id).__name__} "
                f"{agent_id!r}"
            )
        if agent_id <= 0:
            raise ValueError(f"agent_id must be > 0, got {agent_id}")
        self._pool = pool
        self._agent_id = agent_id

    @property
    def agent_id(self) -> int:
        return self._agent_id

    @asynccontextmanager
    async def _acquire(self) -> AsyncIterator[asyncpg.Connection]:
        """Acquire a connection scoped to this agent.

        Wraps the work in an explicit transaction because ``SET LOCAL``
        only persists within a transaction — without ``BEGIN``, asyncpg
        would auto-commit each statement and the GUC would be reset
        after the first query, defeating Layer 2.
        """
        async with self._pool.acquire() as conn:
            tr = conn.transaction()
            await tr.start()
            try:
                # ``SET LOCAL`` does not support parameter binding ($1 etc.)
                # — PostgreSQL parses it as a configuration command, not a
                # DML statement. We f-string the integer in directly.
                # Safe because ``__init__`` rejects anything that's not
                # a positive int — the value here is statically a Python int.
                await conn.execute(
                    f"SET LOCAL anila.agent_id = {int(self._agent_id)}"
                )
                yield conn
                await tr.commit()
            except BaseException:
                await tr.rollback()
                raise

    # ── Write path ──────────────────────────────────────────────────────────

    async def index_chunks(
        self,
        collection_id: int,
        document_id: int,
        chunks: list[ChunkResult],
        embeddings: list[list[float]],
    ) -> int:
        """Bulk-insert chunks with their embeddings.

        Returns the number of rows written. Caller-supplied
        ``len(chunks) == len(embeddings)`` is enforced.

        On constraint violation (e.g. duplicate ``chunk_key`` within
        collection) we re-raise the asyncpg error wrapped in
        ``StoreError`` so the worker's error taxonomy stays uniform.
        """
        if len(chunks) != len(embeddings):
            raise ValueError(
                f"index_chunks: got {len(chunks)} chunks but {len(embeddings)} "
                f"embeddings; counts must match"
            )
        if not chunks:
            return 0

        # Build a list of tuples for executemany — each row is
        # (collection_id, agent_id, document_id, chunk_key, content,
        #  embedding, metadata_dict, token_count). The jsonb codec is
        # registered on every connection (PgPool._init_connection), so
        # asyncpg encodes the dict to JSONB on its own — passing a
        # ``json.dumps`` string would double-encode.
        # Wrap embeddings in HalfVector so the registered halfvec codec on
        # the connection serialises them. Passing a bare list[float] would
        # be interpreted as ``vector`` by asyncpg's codec table and the
        # INSERT would fail with a type-mismatch on the halfvec(4000) column.
        rows = [
            (
                collection_id,
                self._agent_id,
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
                (collection_id, agent_id, document_id, chunk_key,
                 content, content_tsv, embedding, metadata, token_count)
            VALUES
                ($1, $2, $3, $4, $5,
                 to_tsvector('simple', $5),
                 $6, $7, $8)
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
        collection_id: int | None = None,
        top_k: int = 10,
        min_score: float = 0.0,
    ) -> list[SearchHit]:
        """Vector similarity search scoped to this agent.

        ``collection_id`` is optional — leaving it None searches all
        collections owned by the agent (RLS still enforces no leakage
        across agents). Most callers should pin a collection because
        prompt-side context conflation across topics hurts retrieval.

        Cosine *similarity* is what we return (1 - cosine_distance), so
        ``min_score`` reads naturally: 0.7 = "at least 70% similar".
        """
        if top_k <= 0:
            return []

        # The ANN index is on ``embedding vector_cosine_ops``; ``<=>`` is
        # cosine distance ([0, 2]). We re-rank with the explicit cosine
        # similarity expression for the score column so callers can read
        # higher-is-closer. ``ORDER BY embedding <=> $1`` keeps the
        # IVFFlat index hot.
        # Query embedding is also halfvec — wrap so the codec ships the
        # right binary shape. The cosine ``<=>`` operator works on halfvec
        # vs halfvec only after both sides are halfvec.
        q = HalfVector(query_embedding)
        if collection_id is None:
            sql = """
                SELECT id, collection_id, agent_id, document_id, chunk_key,
                       content, metadata, token_count, created_at,
                       1 - (embedding <=> $1) AS score
                  FROM document_chunks
                 WHERE 1 - (embedding <=> $1) >= $2
                 ORDER BY embedding <=> $1
                 LIMIT $3
            """
            args: tuple[Any, ...] = (q, min_score, top_k)
        else:
            sql = """
                SELECT id, collection_id, agent_id, document_id, chunk_key,
                       content, metadata, token_count, created_at,
                       1 - (embedding <=> $1) AS score
                  FROM document_chunks
                 WHERE collection_id = $2
                   AND 1 - (embedding <=> $1) >= $3
                 ORDER BY embedding <=> $1
                 LIMIT $4
            """
            args = (q, collection_id, min_score, top_k)

        async with self._acquire() as conn:
            rows = await conn.fetch(sql, *args)
        return [self._row_to_search_hit(r) for r in rows]

    async def keyword_search(
        self,
        query: str,
        collection_id: int | None = None,
        top_k: int = 10,
        tokenized_query: str | None = None,
    ) -> list[SearchHit]:
        """Full-text keyword search via the GIN index on ``content_tsv``.

        Two ranking modes:

        - When ``tokenized_query`` is provided (e.g. CJK pre-tokenized
          input, or any caller-shaped tsquery-friendly form), use
          ``plainto_tsquery`` on it and rank by ``ts_rank_cd``.
        - When only ``query`` is given, use ``plainto_tsquery`` directly
          on the raw user text. This works fine for languages with
          whitespace word boundaries; CJK callers should pass
          ``tokenized_query`` for better recall.

        ``score`` on the returned ``SearchHit`` is the ``ts_rank_cd``
        value (higher = better match) — NOT a [0,1] cosine similarity
        like ``similarity_search`` returns. Callers that mix the two
        must be explicit about which axis they're sorting by; RRF
        merges them by rank position which is dimension-agnostic.
        """
        if top_k <= 0:
            return []

        # Use tokenized form when given; otherwise let PG tokenize.
        tsquery_input = tokenized_query if tokenized_query else query

        if collection_id is None:
            sql = """
                SELECT id, collection_id, agent_id, document_id, chunk_key,
                       content, metadata, token_count, created_at,
                       ts_rank_cd(content_tsv, q) AS score
                  FROM document_chunks,
                       plainto_tsquery('simple', $1) q
                 WHERE content_tsv @@ q
                 ORDER BY score DESC
                 LIMIT $2
            """
            args: tuple[Any, ...] = (tsquery_input, top_k)
        else:
            sql = """
                SELECT id, collection_id, agent_id, document_id, chunk_key,
                       content, metadata, token_count, created_at,
                       ts_rank_cd(content_tsv, q) AS score
                  FROM document_chunks,
                       plainto_tsquery('simple', $1) q
                 WHERE collection_id = $2
                   AND content_tsv @@ q
                 ORDER BY score DESC
                 LIMIT $3
            """
            args = (tsquery_input, collection_id, top_k)

        async with self._acquire() as conn:
            rows = await conn.fetch(sql, *args)
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
            "id, collection_id, agent_id, document_id, chunk_key, content, "
            "embedding, metadata, token_count, created_at"
            if include_embedding
            else "id, collection_id, agent_id, document_id, chunk_key, content, "
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

    async def list_by_collection(
        self,
        collection_id: int,
        limit: int = 100,
        offset: int = 0,
    ) -> list[IngestionChunk]:
        """Inspector-side: paginated list of chunks in a collection."""
        sql = """
            SELECT id, collection_id, agent_id, document_id, chunk_key,
                   content, metadata, token_count, created_at
              FROM document_chunks
             WHERE collection_id = $1
             ORDER BY id
             LIMIT $2 OFFSET $3
        """
        async with self._acquire() as conn:
            rows = await conn.fetch(sql, collection_id, limit, offset)
        return [self._row_to_chunk(r, include_embedding=False) for r in rows]

    # ── Delete path ─────────────────────────────────────────────────────────

    async def delete_document(self, document_id: int) -> int:
        """Delete every chunk for one document. Returns count deleted."""
        sql = "DELETE FROM document_chunks WHERE document_id = $1"
        async with self._acquire() as conn:
            result = await conn.execute(sql, document_id)
        return int(result.split()[-1]) if result.startswith("DELETE ") else 0

    async def delete_collection(self, collection_id: int) -> int:
        """Delete every chunk for a whole collection. Returns count deleted."""
        sql = "DELETE FROM document_chunks WHERE collection_id = $1"
        async with self._acquire() as conn:
            result = await conn.execute(sql, collection_id)
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
            agent_id=row["agent_id"],
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
                agent_id=row["agent_id"],
                document_id=row["document_id"],
                chunk_key=row["chunk_key"],
                content=row["content"],
                metadata=row["metadata"] or {},
                token_count=row["token_count"],
                created_at=row["created_at"],
            ),
            score=float(row["score"]),
        )
