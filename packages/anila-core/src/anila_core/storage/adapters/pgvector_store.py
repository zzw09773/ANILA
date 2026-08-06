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
from dataclasses import dataclass
from datetime import datetime
from typing import Any, AsyncIterator

import asyncpg
from pgvector import HalfVector

from anila_core.ingestion.chunking_plugins.base import ChunkResult
from anila_core.ingestion.errors import StoreError
from anila_core.models.ingestion import IngestionChunk, SearchHit
from anila_core.storage.adapters.pg_pool import PgPool


# How many leaf rows ``source_model_coverage`` may look at. Bounds the
# healthy-corpus case, which is the one that would otherwise pay a full
# collection scan on every zero-hit query. See that method's docstring
# for the detection accuracy this buys and what it gives up.
_COVERAGE_SAMPLE_ROWS = 200


@dataclass(frozen=True)
class SourceModelCoverage:
    """Which embedding model(s) a collection's leaf chunks were indexed under.

    Exists so a caller staring at an empty ``similarity_search`` result
    can name the cause instead of guessing. The three fields do NOT carry
    equal weight, and mixing them up is how this becomes a check that
    refuses legitimate work:

    * ``has_matching`` — **exact**. True when the collection holds at
      least one leaf chunk under the source model the caller filtered
      on, so the filter is not what emptied the set. This is the only
      field allowed to decide a status code.
    * ``has_other`` — **approximate**. True when at least one leaf chunk
      *in a bounded sample* carries a different source model (NULL
      counts: legacy rows predate P4.8 provenance). False does NOT mean
      "no other model exists anywhere". Operator logging only.
    * ``sample_other_model`` — one such name, for the operator log only.
      Never put it in an end-user response; it is inventory detail, and
      it is None whenever the sampled row's provenance is NULL.

    ``has_other and not has_matching`` is the collection stranded in
    another semantic space — an empty result there is a lie. The
    load-bearing half of that conjunction is ``not has_matching``, which
    is exact: a collection mid-migration, holding both old and new
    vectors, always reports ``has_matching=True`` and so can never be
    refused.
    """

    has_matching: bool
    has_other: bool
    sample_other_model: str | None


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
        parent_id_map: dict[str, int] | None = None,
        *,
        embedding_source_model: str | None = None,
        embedding_native_dim: int | None = None,
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

        P4.8: ``embedding_source_model`` / ``embedding_native_dim`` record
        which model produced the vectors so retrieval can exclude rows
        from a different semantic space.

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

        parent_id_map = parent_id_map or {}

        # JSONB codec is registered on every connection (PgPool.
        # _init_connection), so asyncpg encodes the dict to JSONB
        # on its own — passing a ``json.dumps`` string would
        # double-encode. HalfVector wraps the float list so the halfvec
        # codec ships the right binary shape into the halfvec(4000)
        # column; a bare list[float] would be interpreted as ``vector``
        # and rejected as a type mismatch.
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
                    embedding_source_model,
                    embedding_native_dim,
                )
            )

        sql = """
            INSERT INTO document_chunks
                (collection_id, document_id, chunk_key,
                 content, content_tsv, embedding, metadata, token_count,
                 chunk_type, chunk_level, parent_chunk_id,
                 embedding_source_model, embedding_native_dim)
            VALUES
                ($1, $2, $3, $4,
                 to_tsvector('simple', $4),
                 $5, $6, $7, $8, $9, $10, $11, $12)
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
        *,
        source_model: str | None = None,
    ) -> list[SearchHit]:
        """Vector similarity search scoped to this collection.

        Sprint 4: dropped the legacy ``collection_id`` per-call argument
        — the store IS the collection scope. RLS auto-filters via
        ``anila.collection_id`` GUC set inside ``_acquire()``.

        Cosine *similarity* is what we return (1 - cosine_distance), so
        ``min_score`` is a floor on that value. Do NOT read it as a
        percentage of meaning, and in particular do not reach for 0.7 as
        a "reasonable default": this platform's embedder is
        **asymmetric** — query-side and document-side encodings of the
        *same sentence* measure roughly 0.60–0.83 cosine, never 1.0. A
        0.7 floor therefore sits on top of the identical-sentence band
        and would filter every real passage out, forever, while looking
        like a sane setting. Nothing on this platform runs a floor above
        0.3; anyone who wants one must measure it against their own
        corpus first.

        P4.8: when ``source_model`` is set, only rows whose
        ``embedding_source_model`` matches are considered — vectors from
        a different model live in a different semantic space. A caller
        that gets zero hits under a ``source_model`` filter cannot tell
        "nothing matched" from "everything is indexed under a different
        model"; ``source_model_coverage`` below answers that question.

        The match is **case-insensitive**, and that is load-bearing, not
        tidiness. Historically ``ingestion_collections.embedding_model``
        defaulted to ``nvidia/NV-embed-V2`` while the same model
        registers as ``nvidia/nv-embed-v2``, and migration ``r1_0018``
        backfilled chunk provenance straight from that column
        (``SET embedding_source_model = ic.embedding_model``). A
        case-sensitive ``=`` therefore hid a correctly-indexed corpus
        behind its own column default — every chunk invisible, forever,
        on a knowledge base nobody touched.

        ⚠ **That default is gone and this leniency still is not
        optional.** Migration ``r1_0032`` drops the column default and
        recases the rows it can vouch for, but it aligns them only to
        what ``model_registry`` held at upgrade time, and
        ``model_registry.name`` still has no case-insensitive uniqueness
        (FAKE-CONTROLS #56 item 8) — two spellings can be registered
        side by side tomorrow. Rows the migration deliberately left
        alone (ambiguous or naming an unregistered model) also still
        depend on this. Tighten to ``=`` only after item 8 is closed.
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
        if source_model is not None:
            sql = """
                SELECT id, collection_id, document_id, chunk_key,
                       content, metadata, token_count, created_at,
                       parent_chunk_id, chunk_type, chunk_level,
                       1 - (embedding <=> $1) AS score
                  FROM document_chunks
                 WHERE chunk_type = 'leaf'
                   AND lower(embedding_source_model) = lower($4)
                   AND 1 - (embedding <=> $1) >= $2
                 ORDER BY embedding <=> $1
                 LIMIT $3
            """
            async with self._acquire() as conn:
                rows = await conn.fetch(sql, q, min_score, top_k, source_model)
                hits = [self._row_to_search_hit(r) for r in rows]
                await self._attach_parent_content(conn, hits)
            return hits

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

    async def source_model_coverage(self, source_model: str) -> SourceModelCoverage:
        """Answer "empty corpus, or wrong index?" for this collection.

        ``similarity_search`` filters on ``embedding_source_model`` when
        a platform embedding is designated. Re-designating a different
        model is a supported operator action, and the moment it happens
        every pre-existing row stops matching: the table is still full,
        the query still succeeds, and the result set is empty with no
        error and no log line. This method is the only way to tell that
        state apart from a genuinely empty collection.

        **Cost, and which half is allowed to be approximate.** The two
        fields do very different jobs, so they get different queries.

        ``has_matching`` decides whether the caller gets a hard 409, so
        it must be exact — a sampled answer could refuse a collection
        that is only *mostly* stale, which is precisely the shape of a
        deliberate mid-migration corpus. It is a bare ``EXISTS``, and
        the direction it short-circuits in is the one that matters:
        finding a single row under the designated model stops the scan
        immediately. Healthy corpus → first row answers it. Corpus
        mid-reindex → the first freshly-written row answers it. The only
        case that pays a full collection scan is the one where the
        answer is genuinely "nothing here is retrievable", which is a
        broken state an operator is about to be told to fix, not a
        steady state anyone searches in for long.

        ``has_other`` and ``sample_other_model`` only drive an operator
        log line, never a status code, so they read a bounded sample of
        at most ``_COVERAGE_SAMPLE_ROWS`` rows. Proving "no other model
        anywhere" is the expensive direction and buys nothing a log
        line needs. What that gives up, stated rather than absorbed:

        * A partially reindexed collection whose stale rows all fall
          outside the window will not produce the "partially indexed"
          WARNING. The user-visible behaviour is identical either way.
        * There is no ``ORDER BY``; the window is whatever the scan
          yields first. Reindexing appends rather than rewriting in
          place, which biases toward seeing stale rows rather than
          missing them, but it is a bias, not a guarantee.
        * ``has_other`` can therefore be False while stale rows exist.
          It must never be used to decide that a corpus is healthy —
          only ``has_matching`` carries that weight.

        An earlier revision of this method had it backwards: two bare
        ``EXISTS`` probes, so the healthy corpus paid a full scan on
        every zero-hit query to prove a negative that only fed a log.

        RLS scopes every read to this collection via ``_acquire()``, so
        the answer is per-collection: one stranded knowledge base does
        not make a healthy neighbour look broken.
        """
        sql = f"""
            WITH sample AS (
                SELECT embedding_source_model AS src
                  FROM document_chunks
                 WHERE chunk_type = 'leaf'
                   AND embedding IS NOT NULL
                 LIMIT {int(_COVERAGE_SAMPLE_ROWS)}
            )
            SELECT
                EXISTS (
                    SELECT 1
                      FROM document_chunks
                     WHERE chunk_type = 'leaf'
                       AND embedding IS NOT NULL
                       AND lower(embedding_source_model) = lower($1)
                ) AS has_matching,
                COALESCE(bool_or(lower(src) IS DISTINCT FROM lower($1)), false)
                    AS has_other,
                (
                    SELECT src FROM sample
                     WHERE lower(src) IS DISTINCT FROM lower($1)
                     LIMIT 1
                ) AS sample_other_model
              FROM sample
        """
        async with self._acquire() as conn:
            row = await conn.fetchrow(sql, source_model)
        if row is None:
            return SourceModelCoverage(
                has_matching=False, has_other=False, sample_other_model=None
            )
        sample = row["sample_other_model"]
        return SourceModelCoverage(
            has_matching=bool(row["has_matching"]),
            has_other=bool(row["has_other"]),
            sample_other_model=str(sample) if sample is not None else None,
        )

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

    async def add_parent_chunks(
        self,
        document_id: int,
        chunks: list,
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
# Sprint 4 renamed the class from AgentScopedPgVectorStore. Existing
# imports survive one cycle through this alias; new code should use
# CollectionScopedPgVectorStore directly.
AgentScopedPgVectorStore = CollectionScopedPgVectorStore
