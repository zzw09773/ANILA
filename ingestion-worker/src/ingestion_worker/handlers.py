"""Arq job handlers.

Currently one handler: ``ingest_document``. The handler is the integration
point where the pieces come together — parser, chunker registry,
embedder, and the agent-scoped store. Each piece raises
``IngestionError`` subclasses; the handler catches and persists the
structured failure into ``ingestion_jobs`` so the dev UI can render a
useful message.

Concurrency note: this handler is async and will run in the same event
loop as the Arq worker's main loop. A long-running embedding call
doesn't block other jobs — they're awaited not blocked on. That's why
the parser uses pure-Python (no thread offload) for now: the bottleneck
is the embedding endpoint, not parsing.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

import asyncpg

from anila_core.ingestion.chunking_plugins import get_chunker
from anila_core.ingestion.errors import IngestionError, StoreError
from anila_core.storage.adapters.pg_pool import PgPool
from anila_core.storage.adapters.pgvector_store import AgentScopedPgVectorStore

from ingestion_worker.embedder import Embedder
from ingestion_worker.parsers import extract_text
from ingestion_worker.settings import settings


async def _load_document_meta(
    pool: PgPool, document_id: int
) -> dict[str, Any]:
    """Read the document row + its collection's chunking config + agent_id.

    A single SQL fetch joins the two tables so we don't pay an extra
    round trip. Returns a dict the rest of the handler treats as
    immutable input.
    """
    sql = """
        SELECT d.id            AS document_id,
               d.collection_id AS collection_id,
               d.filename      AS filename,
               d.mime_type     AS mime_type,
               d.storage_path  AS storage_path,
               c.agent_id      AS agent_id,
               c.chunking_config AS chunking_config
          FROM ingestion_documents d
          JOIN ingestion_collections c ON c.id = d.collection_id
         WHERE d.id = $1
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(sql, document_id)
    if row is None:
        raise StoreError(
            code="E_PG_CONSTRAINT",
            retryable=False,
            severity="error",
            user_message=f"Document {document_id} 不存在；可能已被刪除。",
            details={"document_id": document_id},
        )
    return dict(row)


async def _update_document_status(
    pool: PgPool,
    document_id: int,
    status: str,
    *,
    chunk_count: int | None = None,
    error_message: str | None = None,
) -> None:
    """Update one document row's status. Called at every transition."""
    # Explicit ::text casts on $2 so asyncpg doesn't trip on the
    # parameter being used in both ``SET status = $2`` (varchar column)
    # and ``CASE WHEN $2 = 'indexed'`` (text literal compare). Without
    # the cast it raises AmbiguousParameterError.
    sql = """
        UPDATE ingestion_documents
           SET status = $2::text,
               chunk_count = COALESCE($3, chunk_count),
               error_message = $4,
               indexed_at = CASE WHEN $2::text = 'indexed' THEN now() ELSE indexed_at END
         WHERE id = $1
    """
    async with pool.acquire() as conn:
        await conn.execute(sql, document_id, status, chunk_count, error_message)


async def _bump_collection_counters(
    pool: PgPool, collection_id: int, document_count_delta: int, chunk_count_delta: int
) -> None:
    """Adjust collection-level counters atomically.

    Denormalized counters keep the list page snappy without a JOIN
    aggregation per render. The worker is the only writer so there's
    no contention concern.
    """
    sql = """
        UPDATE ingestion_collections
           SET document_count = document_count + $2,
               chunk_count    = chunk_count    + $3,
               updated_at     = now()
         WHERE id = $1
    """
    async with pool.acquire() as conn:
        await conn.execute(sql, collection_id, document_count_delta, chunk_count_delta)


async def _record_job_failure(
    pool: PgPool, arq_job_id: str | None, err: IngestionError
) -> None:
    """Mark the matching ingestion_jobs row as failed with the error code.

    Best-effort — failure to update the job row should never re-raise out
    of the handler (would mask the original error).
    """
    if arq_job_id is None:
        return
    sql = """
        UPDATE ingestion_jobs
           SET status = 'failed',
               progress_pct = 100,
               error_code = $2,
               error_message = $3,
               completed_at = now()
         WHERE arq_job_id = $1
    """
    try:
        async with pool.acquire() as conn:
            await conn.execute(sql, arq_job_id, err.code, err.user_message)
    except Exception:
        # Don't shadow the original IngestionError.
        pass


# ── Handler ─────────────────────────────────────────────────────────────────


async def ingest_document(ctx: dict[str, Any], document_id: int) -> dict[str, Any]:
    """Parse → chunk → embed → index one document.

    ``ctx`` is Arq's per-call context; the worker config injects the
    shared ``pool`` and ``embedder`` into ctx during ``startup``.
    Returns a small summary dict so the job result row carries
    "11 chunks indexed in 4.2s" without re-querying the DB.
    """
    pool: PgPool = ctx["pool"]
    embedder: Embedder = ctx["embedder"]
    arq_job_id: str | None = ctx.get("job_id")

    started_at = datetime.now(timezone.utc)
    try:
        meta = await _load_document_meta(pool, document_id)
        agent_id = int(meta["agent_id"])
        collection_id = int(meta["collection_id"])
        storage_path = meta["storage_path"]
        if not storage_path or not os.path.exists(storage_path):
            raise StoreError(
                code="E_INTERNAL",
                retryable=False,
                severity="error",
                user_message=(
                    f"Uploaded blob missing on disk: {storage_path or '(no path)'}"
                ),
                details={"storage_path": storage_path},
            )

        # 1. Parse — pure function, fast.
        await _update_document_status(pool, document_id, "parsing")
        with open(storage_path, "rb") as f:
            blob = f.read()
        text, parse_meta = extract_text(meta["filename"], blob, meta["mime_type"])

        # 2. Chunk — bounded by document size, also fast.
        # Semantic strategies need embeddings up-front: pre-split into
        # candidate segments, embed each, then call ``chunk()`` with the
        # embeddings stuffed into params. This keeps the chunker
        # interface pure-sync at the cost of a second embedding pass
        # (whose tokens we'd compute anyway).
        await _update_document_status(pool, document_id, "chunking")
        chunking_config = meta["chunking_config"] or {"strategy": "hierarchical"}
        strategy = chunking_config.get("strategy", "hierarchical")
        params = dict(chunking_config.get("params", {}))
        chunker = get_chunker(strategy)
        if getattr(chunker, "requires_embedder", False):
            from anila_core.ingestion.chunking_plugins.builtins import SemanticChunker

            min_tok = int(params.get("min_segment_tokens", 128))
            segments = SemanticChunker.split_segments(text, min_tokens=min_tok)
            params["_segments"] = segments
            if len(segments) >= 2:
                # Real path: embed every candidate segment, semantic
                # chunker does the boundary detection.
                params["_embeddings"] = await embedder.embed(segments)
            elif len(segments) == 1:
                # Single-segment short-circuit. The chunker checks
                # ``len(segments) == 1`` early and returns one chunk
                # without touching the embeddings list, but we still
                # need the count to match (or emit a dummy entry to
                # satisfy the mismatch guard).
                params["_embeddings"] = [[]]
            else:
                params["_embeddings"] = []
        chunks = chunker.chunk(text, parse_meta, params)
        if not chunks:
            await _update_document_status(
                pool, document_id, "indexed",
                chunk_count=0,
                error_message=None,
            )
            return {"chunk_count": 0, "warning": "no chunks produced"}

        # 3. Embed — the slow part; everything else is microseconds.
        await _update_document_status(pool, document_id, "embedding")
        embeddings = await embedder.embed([c.content for c in chunks])

        # 4. Index — single transaction via the agent-scoped store.
        store = AgentScopedPgVectorStore(pool, agent_id=agent_id)
        await store.index_chunks(
            collection_id=collection_id,
            document_id=document_id,
            chunks=chunks,
            embeddings=embeddings,
        )

        # 5. Status + counters.
        await _update_document_status(
            pool, document_id, "indexed",
            chunk_count=len(chunks),
            error_message=None,
        )
        await _bump_collection_counters(
            pool, collection_id, document_count_delta=1, chunk_count_delta=len(chunks)
        )

        return {
            "chunk_count": len(chunks),
            "elapsed_seconds": (
                datetime.now(timezone.utc) - started_at
            ).total_seconds(),
        }

    except IngestionError as err:
        # Persist the structured failure for the dev UI / inspector.
        await _update_document_status(
            pool, document_id, "failed",
            error_message=err.user_message or err.code,
        )
        await _record_job_failure(pool, arq_job_id, err)
        # Re-raise so Arq's retry policy sees the failure too.
        raise
    except Exception as e:
        # Unknown failure → wrap as E_INTERNAL with bounded leakage.
        wrapped = StoreError(
            code="E_INTERNAL",
            retryable=False,
            severity="error",
            user_message="內部錯誤，請聯絡管理員。",
            details={"cause": type(e).__name__, "message": str(e)[:200]},
        )
        await _update_document_status(
            pool, document_id, "failed",
            error_message=wrapped.user_message,
        )
        await _record_job_failure(pool, arq_job_id, wrapped)
        raise
