"""Collection-scoped pgvector store (docs/ingestion/ingestion-platform-design.md §3.3 Layer 3).

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
from typing import AsyncIterator

import asyncpg
from anila_contracts import Classification
from pgvector import HalfVector

from anila_core.ingestion.chunking_plugins.base import ChunkResult
from anila_core.ingestion.errors import StoreError
from anila_core.models.ingestion import IngestionChunk, SearchHit
from anila_core.storage.adapters.pg_pool import PgPool


_INGESTION_CLASSIFICATION_SOURCE = "ingestion_effective"


def _classification_storage_value(classification: Classification) -> str:
    """Return the canonical storage value and reject untyped callers."""
    if not isinstance(classification, Classification):
        raise ValueError(
            "classification must be an anila_contracts.Classification value, "
            f"got {type(classification).__name__}"
        )
    return classification.to_storage()


def _classification_values_at_or_below(
    ceiling: Classification,
) -> list[str]:
    _classification_storage_value(ceiling)
    return [
        level.to_storage()
        for level in Classification
        if level <= ceiling
    ]


def _classification_from_storage(*, field: str, raw: object) -> Classification:
    """Parse trusted DB state without ever defaulting an invalid value low."""
    if not isinstance(raw, str):
        raise StoreError(
            code="E_CLASSIFICATION_INVALID",
            retryable=False,
            severity="critical",
            user_message="資料庫分類資料無效，已拒絕存取。",
            details={"field": field, "value_type": type(raw).__name__},
        )
    try:
        return Classification.from_storage(raw)
    except ValueError as exc:
        raise StoreError(
            code="E_CLASSIFICATION_INVALID",
            retryable=False,
            severity="critical",
            user_message="資料庫分類資料無效，已拒絕存取。",
            details={"field": field, "value": raw},
        ) from exc


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

    async def _resolve_write_classification(
        self,
        conn: asyncpg.Connection,
        *,
        document_id: int,
        caller_floor: Classification,
    ) -> Classification:
        """Lock current sources and return max(caller, document, collection).

        Lock order is always collection then document for both parent and leaf
        writes. ``FOR SHARE`` conflicts with classification UPDATEs: if ingest
        wins, a later parent upgrade waits then cascades to the new rows; if the
        upgrade wins, this read waits and observes the raised source level.
        """
        collection = await conn.fetchrow(
            """
            SELECT id, classification_level
              FROM ingestion_collections
             WHERE id = $1
               FOR SHARE
            """,
            self._collection_id,
        )
        if collection is None:
            raise StoreError(
                code="E_PG_CONSTRAINT",
                retryable=False,
                severity="error",
                user_message="知識庫不存在或已刪除，已拒絕寫入 chunk。",
                details={"collection_id": self._collection_id},
            )

        document = await conn.fetchrow(
            """
            SELECT id, collection_id, classification_level
              FROM ingestion_documents
             WHERE id = $1
               FOR SHARE
            """,
            document_id,
        )
        if document is None:
            raise StoreError(
                code="E_PG_CONSTRAINT",
                retryable=False,
                severity="error",
                user_message="文件不存在或已刪除，已拒絕寫入 chunk。",
                details={"document_id": document_id},
            )
        if document["collection_id"] != self._collection_id:
            raise StoreError.rls_violation(
                user_message="文件不屬於目前知識庫，已拒絕跨知識庫寫入。",
                details={
                    "document_id": document_id,
                    "expected_collection_id": self._collection_id,
                    "actual_collection_id": document["collection_id"],
                },
            )

        collection_level = _classification_from_storage(
            field="ingestion_collections.classification_level",
            raw=collection["classification_level"],
        )
        document_level = _classification_from_storage(
            field="ingestion_documents.classification_level",
            raw=document["classification_level"],
        )
        return Classification.max_of(
            [caller_floor, collection_level, document_level]
        )

    # ── Write path ──────────────────────────────────────────────────────────

    async def index_chunks(
        self,
        document_id: int,
        chunks: list[ChunkResult],
        embeddings: list[list[float]],
        *,
        classification_level: Classification,
        parent_id_map: dict[str, int] | None = None,
    ) -> int:
        """Bulk-insert leaf chunks with their embeddings.

        Sprint 4: ``collection_id`` is no longer a per-call argument —
        the store is constructed against one collection, so all writes
        land in that scope. Pass ``document_id`` only.

        Sprint 9 X / parent-child: each chunk's metadata may carry
        ``parent_chunk_key`` referring to a previously-inserted parent
        row. ``parent_id_map`` (returned from ``add_parent_chunks``)
        translates those keys into FK ids written into the
        ``parent_chunk_id`` column. Missing references are silently
        treated as NULL — caller can validate ahead of time.

        Returns the number of rows written. Caller-supplied
        ``len(chunks) == len(embeddings)`` is enforced.

        On constraint violation (duplicate ``chunk_key`` within the
        document) we re-raise the asyncpg error wrapped in
        ``StoreError`` so the worker's error taxonomy stays uniform.
        """
        # Validate the public contract even on an empty batch. The actual
        # persisted level is re-derived from locked DB sources below.
        _classification_storage_value(classification_level)
        if len(chunks) != len(embeddings):
            raise ValueError(
                f"index_chunks: got {len(chunks)} chunks but {len(embeddings)} "
                f"embeddings; counts must match"
            )
        if not chunks:
            return 0

        parent_id_map = parent_id_map or {}

        # JSONB codec is registered on every connection (PgPool.
        # _init_connection), so asyncpg encodes the dict to JSONB
        # on its own — passing a ``json.dumps`` string would
        # double-encode. HalfVector wraps the float list so the halfvec
        # codec ships the right binary shape into the halfvec(4000)
        # column; a bare list[float] would be interpreted as ``vector``
        # and rejected as a type mismatch.
        sql = """
            INSERT INTO document_chunks
                (collection_id, document_id, chunk_key,
                 content, content_tsv, embedding, metadata, token_count,
                 chunk_type, chunk_level, parent_chunk_id,
                 classification_level, classification_latched_at,
                 classification_source)
            VALUES
                ($1, $2, $3, $4,
                 to_tsvector('simple', $4),
                 $5, $6, $7, $8, $9, $10,
                 $11, CURRENT_TIMESTAMP, $12)
        """
        try:
            async with self._acquire() as conn:
                effective = await self._resolve_write_classification(
                    conn,
                    document_id=document_id,
                    caller_floor=classification_level,
                )
                classification_value = effective.to_storage()
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
                            classification_value,
                            _INGESTION_CLASSIFICATION_SOURCE,
                        )
                    )
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
        #
        # Sprint 9 X / parent-child: vector search restricted to
        # ``chunk_type='leaf'`` rows. Heading / document parent rows
        # have ``embedding=NULL`` so they wouldn't match the cosine
        # operator anyway, but the explicit filter lets the planner
        # skip them without computing distance.
        q = HalfVector(query_embedding)
        sql = """
            SELECT id, collection_id, document_id, chunk_key,
                   content, metadata, token_count, created_at,
                   parent_chunk_id, chunk_type, chunk_level,
                   classification_level, classification_latched_at,
                   classification_source,
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

    async def similarity_search_per_document(
        self,
        query_embedding: list[float],
        document_ids: list[int],
        k: int = 1,
        min_score: float = 0.0,
    ) -> list[SearchHit]:
        """Top-``k`` leaf chunks **per document** for a doc-id set, in ONE query.

        Relation expansion (design v2 §7): once the main top-k surfaces a set of
        related documents, fetch each related document's best chunk(s) to drop
        into context — WITHOUT N separate full scans, and WITHOUT losing a
        target chunk that simply isn't in the global top-k (the failure mode of
        post-filtering the main results).

        ``RANK() OVER (PARTITION BY document_id ORDER BY <=>)`` ranks chunks
        within each document independently; the outer ``rnk <= k`` keeps the top
        ``k`` of each. Ties share a rank (RANK, not ROW_NUMBER), so a document
        with two equally-close chunks at k=1 returns both — acceptable for a
        retrieval aid. RLS auto-scopes to this collection.
        """
        if k <= 0 or not document_ids:
            return []

        q = HalfVector(query_embedding)
        sql = """
            WITH ranked AS (
                SELECT id, collection_id, document_id, chunk_key,
                       content, metadata, token_count, created_at,
                       parent_chunk_id, chunk_type, chunk_level,
                       classification_level, classification_latched_at,
                       classification_source,
                       1 - (embedding <=> $1) AS score,
                       RANK() OVER (
                           PARTITION BY document_id
                           ORDER BY embedding <=> $1
                       ) AS rnk
                  FROM document_chunks
                 WHERE chunk_type = 'leaf'
                   AND document_id = ANY($2::bigint[])
                   AND 1 - (embedding <=> $1) >= $3
            )
            SELECT * FROM ranked
             WHERE rnk <= $4
             ORDER BY document_id, rnk
        """
        async with self._acquire() as conn:
            rows = await conn.fetch(sql, q, document_ids, min_score, k)
            hits = [self._row_to_search_hit(r) for r in rows]
            await self._attach_parent_content(conn, hits)
        return hits

    async def similarity_search_scoped_documents(
        self,
        query_embedding: list[float],
        document_ids: list[int],
        top_k: int = 10,
        min_score: float = 0.0,
        *,
        classification_ceiling: Classification,
    ) -> list[SearchHit]:
        """Global top-k constrained to an authoritative document allow-list.

        Clearance evaluation happens in CSP against persisted grants and
        compartments.  This storage boundary consumes only the resulting
        positive document IDs and applies them in SQL *before* ranking; it
        never accepts a caller-supplied clearance level or performs a
        post-filter that could widen scope.
        """

        if top_k <= 0 or not document_ids:
            return []
        if any(
            not isinstance(document_id, int)
            or isinstance(document_id, bool)
            or document_id <= 0
            for document_id in document_ids
        ) or len(document_ids) != len(set(document_ids)):
            raise ValueError("document_ids must be unique positive integers")

        query = HalfVector(query_embedding)
        allowed_levels = _classification_values_at_or_below(
            classification_ceiling
        )
        sql = """
            SELECT id, collection_id, document_id, chunk_key,
                   content, metadata, token_count, created_at,
                   parent_chunk_id, chunk_type, chunk_level,
                   classification_level, classification_latched_at,
                   classification_source,
                   1 - (embedding <=> $1) AS score
              FROM document_chunks
             WHERE chunk_type = 'leaf'
               AND document_id = ANY($2::bigint[])
               AND classification_level = ANY($3::text[])
               AND 1 - (embedding <=> $1) >= $4
             ORDER BY embedding <=> $1
             LIMIT $5
        """
        async with self._acquire() as conn:
            rows = await conn.fetch(
                sql,
                query,
                document_ids,
                allowed_levels,
                min_score,
                top_k,
            )
            hits = [self._row_to_search_hit(row) for row in rows]
            await self._attach_parent_content(
                conn, hits, classification_ceiling=classification_ceiling
            )
        return hits

    async def similarity_search_per_document_authorized(
        self,
        query_embedding: list[float],
        document_ids: list[int],
        *,
        classification_ceiling: Classification,
        k: int = 1,
        min_score: float = 0.0,
    ) -> list[SearchHit]:
        """Per-document ranking with a canonical chunk-level ceiling."""

        if k <= 0 or not document_ids:
            return []
        if any(
            not isinstance(document_id, int)
            or isinstance(document_id, bool)
            or document_id <= 0
            for document_id in document_ids
        ) or len(document_ids) != len(set(document_ids)):
            raise ValueError("document_ids must be unique positive integers")
        query = HalfVector(query_embedding)
        allowed_levels = _classification_values_at_or_below(
            classification_ceiling
        )
        sql = """
            WITH ranked AS (
                SELECT id, collection_id, document_id, chunk_key,
                       content, metadata, token_count, created_at,
                       parent_chunk_id, chunk_type, chunk_level,
                       classification_level, classification_latched_at,
                       classification_source,
                       1 - (embedding <=> $1) AS score,
                       RANK() OVER (
                           PARTITION BY document_id
                           ORDER BY embedding <=> $1
                       ) AS rnk
                  FROM document_chunks
                 WHERE chunk_type = 'leaf'
                   AND document_id = ANY($2::bigint[])
                   AND classification_level = ANY($3::text[])
                   AND 1 - (embedding <=> $1) >= $4
            )
            SELECT * FROM ranked
             WHERE rnk <= $5
             ORDER BY document_id, rnk
        """
        async with self._acquire() as conn:
            rows = await conn.fetch(
                sql,
                query,
                document_ids,
                allowed_levels,
                min_score,
                k,
            )
            hits = [self._row_to_search_hit(row) for row in rows]
            await self._attach_parent_content(
                conn, hits, classification_ceiling=classification_ceiling
            )
        return hits

    async def add_parent_chunks(
        self,
        document_id: int,
        chunks: list,
        *,
        classification_level: Classification,
    ) -> dict[str, int]:
        """Sprint 9 X / parent-child — insert non-leaf rows (no embedding).

        ``HierarchicalChunker`` emits ``chunk_type='heading'`` and
        ``chunk_type='document'`` rows alongside leaves. Parents have
        no embedding; their job is to be JOIN-fetched as
        ``parent_content`` at retrieval time.

        Returns ``{chunk_key: db_id}`` so the caller can resolve
        ``parent_chunk_key`` references on the leaf rows it inserts
        next via the regular ``index_chunks`` path.

        We use ``INSERT ... RETURNING id, chunk_key`` so the mapping
        comes back in one round-trip; ``executemany`` doesn't surface
        RETURNING values, hence the per-row loop.
        """
        _classification_storage_value(classification_level)
        if not chunks:
            return {}

        sql = """
            INSERT INTO document_chunks
                (collection_id, document_id, chunk_key,
                 content, content_tsv, embedding, metadata, token_count,
                 chunk_type, chunk_level, parent_chunk_id,
                 classification_level, classification_latched_at,
                 classification_source)
            VALUES
                ($1, $2, $3, $4,
                 to_tsvector('simple', $4),
                 NULL, $5, $6, $7, $8, NULL,
                 $9, CURRENT_TIMESTAMP, $10)
            RETURNING id, chunk_key
        """
        out: dict[str, int] = {}
        try:
            async with self._acquire() as conn:
                effective = await self._resolve_write_classification(
                    conn,
                    document_id=document_id,
                    caller_floor=classification_level,
                )
                classification_value = effective.to_storage()
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
                        classification_value,
                        _INGESTION_CLASSIFICATION_SOURCE,
                    )
                    out[row["chunk_key"]] = row["id"]
        except asyncpg.ConnectionDoesNotExistError as e:
            raise StoreError.pg_connect(
                user_message="資料庫連線中斷，請稍後再試",
                details={"cause": type(e).__name__},
            ) from e
        return out

    async def replace_document_chunks(
        self,
        document_id: int,
        *,
        parent_chunks: list[ChunkResult],
        leaf_chunks: list[ChunkResult],
        embeddings: list[list[float]],
        classification_level: Classification,
    ) -> int:
        """Atomically replace every indexed chunk for one document.

        Re-indexing used to commit parents and leaves in separate
        transactions.  A leaf insert failure therefore left partial parents,
        and the next retry collided with the document/chunk-key uniqueness
        constraint.  This boundary locks the current classification sources,
        deletes the previous generation, inserts parents, resolves their IDs,
        and inserts leaves in one transaction.  Any exception restores the
        complete prior generation.
        """

        _classification_storage_value(classification_level)
        if len(leaf_chunks) != len(embeddings):
            raise ValueError(
                "replace_document_chunks: got "
                f"{len(leaf_chunks)} leaf chunks but {len(embeddings)} "
                "embeddings; counts must match"
            )

        all_keys = [chunk.chunk_key for chunk in (*parent_chunks, *leaf_chunks)]
        if len(all_keys) != len(set(all_keys)):
            raise ValueError("replace_document_chunks: duplicate chunk_key")

        parent_keys = {chunk.chunk_key for chunk in parent_chunks}
        for leaf in leaf_chunks:
            metadata = leaf.metadata or {}
            parent_key = metadata.get("parent_chunk_key")
            if parent_key is not None and parent_key not in parent_keys:
                raise ValueError(
                    "replace_document_chunks: leaf references an unknown "
                    f"parent_chunk_key {parent_key!r}"
                )

        parent_sql = """
            INSERT INTO document_chunks
                (collection_id, document_id, chunk_key,
                 content, content_tsv, embedding, metadata, token_count,
                 chunk_type, chunk_level, parent_chunk_id,
                 classification_level, classification_latched_at,
                 classification_source)
            VALUES
                ($1, $2, $3, $4,
                 to_tsvector('simple', $4),
                 NULL, $5, $6, $7, $8, NULL,
                 $9, CURRENT_TIMESTAMP, $10)
            RETURNING id, chunk_key
        """
        leaf_sql = """
            INSERT INTO document_chunks
                (collection_id, document_id, chunk_key,
                 content, content_tsv, embedding, metadata, token_count,
                 chunk_type, chunk_level, parent_chunk_id,
                 classification_level, classification_latched_at,
                 classification_source)
            VALUES
                ($1, $2, $3, $4,
                 to_tsvector('simple', $4),
                 $5, $6, $7, $8, $9, $10,
                 $11, CURRENT_TIMESTAMP, $12)
        """

        try:
            async with self._acquire() as conn:
                effective = await self._resolve_write_classification(
                    conn,
                    document_id=document_id,
                    caller_floor=classification_level,
                )
                classification_value = effective.to_storage()
                await conn.execute(
                    "DELETE FROM document_chunks WHERE document_id = $1",
                    document_id,
                )

                parent_id_map: dict[str, int] = {}
                for chunk in parent_chunks:
                    metadata = chunk.metadata or {}
                    row = await conn.fetchrow(
                        parent_sql,
                        self._collection_id,
                        document_id,
                        chunk.chunk_key,
                        chunk.content,
                        metadata,
                        chunk.token_count,
                        metadata.get("chunk_type", "heading"),
                        int(metadata.get("chunk_level", 0)),
                        classification_value,
                        _INGESTION_CLASSIFICATION_SOURCE,
                    )
                    parent_id_map[str(row["chunk_key"])] = int(row["id"])

                leaf_rows: list[tuple] = []
                for chunk, embedding in zip(leaf_chunks, embeddings):
                    metadata = chunk.metadata or {}
                    parent_key = metadata.get("parent_chunk_key")
                    leaf_rows.append(
                        (
                            self._collection_id,
                            document_id,
                            chunk.chunk_key,
                            chunk.content,
                            HalfVector(embedding),
                            metadata,
                            chunk.token_count,
                            metadata.get("chunk_type", "leaf"),
                            int(metadata.get("chunk_level", 0)),
                            parent_id_map.get(parent_key),
                            classification_value,
                            _INGESTION_CLASSIFICATION_SOURCE,
                        )
                    )
                if leaf_rows:
                    await conn.executemany(leaf_sql, leaf_rows)
        except asyncpg.ConnectionDoesNotExistError as exc:
            raise StoreError.pg_connect(
                user_message="資料庫連線中斷，請稍後再試",
                details={"cause": type(exc).__name__},
            ) from exc
        return len(parent_chunks) + len(leaf_chunks)

    async def reconcile_collection_counters(self) -> None:
        """Recompute indexed document/chunk counters under the scope lock.

        Re-index replaces a generation, so increment-only counters drift on
        every retry. Keeping the chunk-table aggregation in this canonical
        store also preserves the invariant that worker handlers never grow a
        second raw ``document_chunks`` SQL boundary.
        """

        try:
            async with self._acquire() as conn:
                async with conn.transaction():
                    locked = await conn.fetchrow(
                        """
                        SELECT id
                          FROM ingestion_collections
                         WHERE id = $1
                           FOR UPDATE
                        """,
                        self._collection_id,
                    )
                    if locked is None:
                        raise StoreError(
                            code="E_PG_SCOPE_MISSING",
                            retryable=False,
                            severity="error",
                            user_message="知識庫不存在，無法校正索引計數。",
                            details={"collection_id": self._collection_id},
                        )
                    await conn.execute(
                        """
                        UPDATE ingestion_collections
                           SET document_count = (
                                   SELECT count(*)
                                     FROM ingestion_documents
                                    WHERE collection_id = $1
                                      AND status = 'indexed'
                               ),
                               chunk_count = (
                                   SELECT count(*)
                                     FROM document_chunks
                                    WHERE collection_id = $1
                               ),
                               updated_at = now()
                         WHERE id = $1
                        """,
                        self._collection_id,
                    )
        except asyncpg.ConnectionDoesNotExistError as exc:
            raise StoreError.pg_connect(
                user_message="資料庫連線中斷，請稍後再試",
                details={"cause": type(exc).__name__},
            ) from exc

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
        # Sprint 9 X / parent-child: same leaf-only filter as
        # similarity_search. Parent rows carry only the heading title
        # which would inflate keyword-match noise (every doc would
        # match its own chapter titles); leaves carry the substantive
        # text users actually search for.
        sql = """
            SELECT id, collection_id, document_id, chunk_key,
                   content, metadata, token_count, created_at,
                   parent_chunk_id, chunk_type, chunk_level,
                   classification_level, classification_latched_at,
                   classification_source,
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
        *,
        classification_ceiling: Classification | None = None,
    ) -> None:
        """Sprint 9 X — fill ``hit.parent_content`` from the parent row's content.

        Single ``id = ANY($1::bigint[])`` round-trip pulls every
        unique parent in one shot. Hits whose parent_chunk_id is NULL
        (root rows / legacy leaves without a parent) are left with
        ``parent_content=None``.
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
        if classification_ceiling is None:
            rows = await conn.fetch(
                """
            SELECT id, content, classification_level
              FROM document_chunks
             WHERE id = ANY($1::bigint[])
               AND collection_id = $2
                """,
                list(parent_ids),
                self._collection_id,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT id, content, classification_level
                  FROM document_chunks
                 WHERE id = ANY($1::bigint[])
                   AND collection_id = $2
                   AND classification_level = ANY($3::text[])
                """,
                list(parent_ids),
                self._collection_id,
                _classification_values_at_or_below(classification_ceiling),
            )
        # A malformed parent must block retrieval too. Returning its content
        # while validating only the matched leaf would be a fail-open path.
        for row in rows:
            self._classification_from_row(row)
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
        doesn't render the 1536-d vector — sending it bloats the payload.
        Set True only when the dev explicitly asks (e.g. embedding-norm
        debug column behind the inspector's "show vector debug" toggle).
        """
        cols = (
            "id, collection_id, document_id, chunk_key, content, "
            "embedding, metadata, token_count, created_at, "
            "classification_level, classification_latched_at, classification_source"
            if include_embedding
            else "id, collection_id, document_id, chunk_key, content, "
            "metadata, token_count, created_at, classification_level, "
            "classification_latched_at, classification_source"
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
                   content, metadata, token_count, created_at,
                   classification_level, classification_latched_at,
                   classification_source
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
    def _classification_from_row(row: asyncpg.Record) -> Classification:
        """Parse a required DB classification with canonical fail-closed rules."""
        raw = row["classification_level"]
        if not isinstance(raw, str):
            raise ValueError(
                "document_chunks.classification_level must be a non-null string, "
                f"got {type(raw).__name__}"
            )
        return Classification.from_storage(raw)

    @staticmethod
    def _row_to_chunk(row: asyncpg.Record, *, include_embedding: bool) -> IngestionChunk:
        # JSONB codec parses asynchronously into a dict. Defaults to {}
        # in the schema so this can never be NULL, but None-guard regardless.
        metadata = row["metadata"] or {}
        # Sprint 9 X new columns are optional — list endpoints that
        # don't SELECT them won't have the keys in the asyncpg.Record.
        # ``Record.get`` doesn't exist; use a try/except dance.
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
            classification_level=CollectionScopedPgVectorStore._classification_from_row(row),
            classification_latched_at=_opt("classification_latched_at"),
            classification_source=_opt("classification_source"),
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
                classification_level=cls._classification_from_row(row),
                classification_latched_at=_opt("classification_latched_at"),
                classification_source=_opt("classification_source"),
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
# Sprint 4 renamed the class from AgentScopedPgVectorStore. Existing
# imports survive one cycle through this alias; new code should use
# CollectionScopedPgVectorStore directly.
AgentScopedPgVectorStore = CollectionScopedPgVectorStore
