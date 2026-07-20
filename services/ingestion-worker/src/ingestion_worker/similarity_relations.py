"""Durable, debounced topic-similarity recomputation.

A different KIND of edge from citations: pure vector similarity between
document-level centroids — surfaces docs that are *topically* related even with
no explicit citation. Written to the SAME ``document_relations`` table as
``source='similarity'``, ``relation_type='relates'``, ``confidence`` = cosine,
coexisting with rule/manual/llm rows (UNIQUE includes source).

Why top-K, not a threshold: same-domain corpora cluster tightly (regulations in
one domain routinely score 0.95+ pairwise), so an absolute threshold connects
everything. We take each document's K nearest neighbours instead — sparsity
comes from K; the floor only screens genuinely off-topic docs. The whole thing
is one pgvector query (``avg(halfvec)`` centroids + ``<=>`` + a per-source
``row_number()`` window), verified against pgvector 0.8.2.

Ingestion only upserts one durable request per collection.  A lease-fenced
supervisor recomputes after a quiet debounce window; requests arriving while a
run is active advance ``request_seq`` and force exactly one follow-up pass.
This avoids the former O(N²) recomputation on every document while retaining
crash recovery and multi-worker safety.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


class SimilarityLeaseLostError(RuntimeError):
    """The durable recompute claim no longer owns the mutation boundary."""

# Canonical (a < b) top-K nearest-neighbour pairs over document centroids.
_PAIRS_SQL = """
WITH centroids AS (
    SELECT document_id, avg(embedding) AS c
      FROM document_chunks
     WHERE collection_id = $1
       AND is_active_generation = true
       AND chunk_type = 'leaf'
       AND embedding IS NOT NULL
     GROUP BY document_id
),
pairs AS (
    SELECT a.document_id AS src, b.document_id AS dst, 1 - (a.c <=> b.c) AS sim
      FROM centroids a JOIN centroids b ON a.document_id <> b.document_id
),
ranked AS (
    SELECT src, dst, sim,
           row_number() OVER (PARTITION BY src ORDER BY sim DESC) AS rnk
      FROM pairs
     WHERE sim >= $2
)
SELECT least(src, dst) AS a, greatest(src, dst) AS b, max(sim) AS sim
  FROM ranked
 WHERE rnk <= $3
 GROUP BY least(src, dst), greatest(src, dst)
"""


async def recompute_similarity_edges(
    pool: Any,
    *,
    claim: SimilarityClaim,
    collection_id: int,
    run_id: str | None,
    settings: Any,
) -> dict[str, int]:
    """Recompute the collection's ``source='similarity'`` edges. Returns
    ``{"edges": n}`` (0 when disabled / not enough docs / too many docs)."""
    if not settings.enable_similarity_edges:
        return {"edges": 0}

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                f"SET LOCAL anila.collection_id = {int(collection_id)}"
            )
            # Lock and validate the durable claim in the SAME transaction as
            # every DELETE/INSERT below.  A paused owner whose lease expired
            # must never commit stale pairs after a successor has finished.
            owned = await conn.fetchrow(
                """
                SELECT collection_id
                  FROM similarity_recompute_requests
                 WHERE collection_id=$1 AND status='running'
                   AND lease_token=$2 AND claimed_seq=$3
                   AND lease_expires_at >= now()
                 FOR UPDATE
                """,
                collection_id,
                claim.lease_token,
                claim.request_seq,
            )
            if owned is None or claim.collection_id != collection_id:
                raise SimilarityLeaseLostError(
                    "similarity recompute lease is no longer owned"
                )
            doc_count = await conn.fetchval(
                "SELECT count(DISTINCT document_id) FROM document_chunks "
                "WHERE collection_id = $1 AND is_active_generation = true "
                "AND chunk_type = 'leaf' AND embedding IS NOT NULL",
                collection_id,
            )
            if not doc_count or doc_count < 2:
                # nothing to relate yet; still clear stale similarity edges
                await conn.execute(
                    "DELETE FROM document_relations "
                    "WHERE collection_id = $1 AND source = 'similarity'",
                    collection_id,
                )
                return {"edges": 0}
            if doc_count > settings.similarity_max_docs:
                logger.info(
                    "collection %s: %d docs > similarity cap %d — skipping "
                    "similarity recompute (needs an ANN pre-filter)",
                    collection_id, doc_count, settings.similarity_max_docs,
                )
                # A previous, smaller generation may have produced edges.  A
                # cap transition must fail closed instead of leaving those
                # stale relationships indefinitely visible.
                await conn.execute(
                    "DELETE FROM document_relations "
                    "WHERE collection_id = $1 AND source = 'similarity'",
                    collection_id,
                )
                return {"edges": 0}

            pairs = await conn.fetch(
                _PAIRS_SQL,
                collection_id,
                float(settings.similarity_min),
                int(settings.similarity_top_k),
            )

            # normalized_title per involved doc → durable target_ref
            ids = sorted({r["a"] for r in pairs} | {r["b"] for r in pairs})
            norm_by_id: dict[int, str] = {}
            if ids:
                trows = await conn.fetch(
                    "SELECT id, normalized_title FROM ingestion_documents "
                    "WHERE collection_id = $1 AND id = ANY($2::int[])",
                    collection_id, ids,
                )
                norm_by_id = {r["id"]: (r["normalized_title"] or "") for r in trows}

            # collection-wide replace: similarity edges are global, not per-src
            await conn.execute(
                "DELETE FROM document_relations "
                "WHERE collection_id = $1 AND source = 'similarity'",
                collection_id,
            )
            inserted = 0
            for r in pairs:
                a, b, sim = r["a"], r["b"], float(r["sim"])
                target_ref = norm_by_id.get(b) or f"doc:{b}"
                status = await conn.execute(
                    "INSERT INTO document_relations "
                    "(collection_id, src_document_id, dst_document_id, target_ref, "
                    " relation_type, confidence, source, extractor_run_id) "
                    "VALUES ($1, $2, $3, $4, 'relates', $5, 'similarity', $6) "
                    "ON CONFLICT (collection_id, src_document_id, target_ref, relation_type, source) "
                    "DO NOTHING",
                    collection_id, a, b, target_ref, sim, run_id,
                )
                if isinstance(status, str) and status.rsplit(" ", 1)[-1] == "1":
                    inserted += 1
    return {"edges": inserted}


@dataclass(frozen=True)
class SimilarityClaim:
    collection_id: int
    request_seq: int
    lease_token: str


async def request_similarity_recompute(
    pool: Any,
    *,
    collection_id: int,
    debounce_seconds: float,
) -> int:
    """Upsert one durable debounce request and return its monotonic sequence."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO similarity_recompute_requests
              (collection_id,status,request_seq,requested_at,not_before,updated_at)
            VALUES ($1,'pending',1,now(),
                    now()+($2 * interval '1 second'),now())
            ON CONFLICT (collection_id) DO UPDATE SET
              request_seq=similarity_recompute_requests.request_seq+1,
              requested_at=now(),
              not_before=now()+($2 * interval '1 second'),
              updated_at=now()
            RETURNING request_seq
            """,
            collection_id,
            debounce_seconds,
        )
    return int(row["request_seq"])


async def claim_similarity_recompute(
    pool: Any,
    *,
    lease_seconds: int,
) -> SimilarityClaim | None:
    """Claim one due/expired collection without holding a DB lock during work."""
    token = uuid.uuid4().hex
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT collection_id, request_seq
                  FROM similarity_recompute_requests
                 WHERE (status='pending' AND not_before <= now())
                    OR (status='running' AND lease_expires_at < now())
                 ORDER BY not_before, collection_id
                 FOR UPDATE SKIP LOCKED
                 LIMIT 1
                """
            )
            if row is None:
                return None
            await conn.execute(
                """
                UPDATE similarity_recompute_requests
                   SET status='running', claimed_seq=request_seq,
                       lease_token=$2,
                       lease_expires_at=now()+($3 * interval '1 second'),
                       last_error=NULL, updated_at=now()
                 WHERE collection_id=$1
                """,
                int(row["collection_id"]),
                token,
                lease_seconds,
            )
    return SimilarityClaim(
        collection_id=int(row["collection_id"]),
        request_seq=int(row["request_seq"]),
        lease_token=token,
    )


async def heartbeat_similarity_claim(
    pool: Any,
    *,
    claim: SimilarityClaim,
    lease_seconds: int,
) -> bool:
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE similarity_recompute_requests
               SET lease_expires_at=now()+($3 * interval '1 second'),
                   updated_at=now()
             WHERE collection_id=$1 AND status='running'
               AND lease_token=$2 AND claimed_seq=$4
            """,
            claim.collection_id,
            claim.lease_token,
            lease_seconds,
            claim.request_seq,
        )
    return result == "UPDATE 1"


async def _heartbeat_similarity_work(
    pool: Any,
    *,
    claim: SimilarityClaim,
    lease_seconds: int,
    heartbeat_seconds: int,
    owner_task: asyncio.Task[Any],
) -> None:
    try:
        while True:
            await asyncio.sleep(heartbeat_seconds)
            if not await heartbeat_similarity_claim(
                pool, claim=claim, lease_seconds=lease_seconds
            ):
                owner_task.cancel("similarity recompute lease lost")
                return
    except asyncio.CancelledError:
        raise
    except Exception:
        owner_task.cancel("similarity recompute heartbeat failed")


async def finish_similarity_recompute(pool: Any, *, claim: SimilarityClaim) -> bool:
    """Acknowledge one pass; preserve a newer request as one pending follow-up."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT request_seq, claimed_seq, not_before
                  FROM similarity_recompute_requests
                 WHERE collection_id=$1 AND status='running'
                   AND lease_token=$2
                 FOR UPDATE
                """,
                claim.collection_id,
                claim.lease_token,
            )
            if row is None or int(row["claimed_seq"]) != claim.request_seq:
                return False
            if int(row["request_seq"]) > claim.request_seq:
                await conn.execute(
                    """
                    UPDATE similarity_recompute_requests
                       SET status='pending', claimed_seq=NULL, lease_token=NULL,
                           lease_expires_at=NULL, last_error=NULL, updated_at=now()
                     WHERE collection_id=$1
                    """,
                    claim.collection_id,
                )
            else:
                await conn.execute(
                    "DELETE FROM similarity_recompute_requests "
                    "WHERE collection_id=$1",
                    claim.collection_id,
                )
    return True


async def fail_similarity_recompute(
    pool: Any,
    *,
    claim: SimilarityClaim,
    error: Exception,
    backoff_seconds: int,
) -> bool:
    """Release an owned failure for a bounded durable retry."""
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE similarity_recompute_requests
               SET status='pending', claimed_seq=NULL, lease_token=NULL,
                   lease_expires_at=NULL,
                   not_before=now()+($3 * interval '1 second'),
                   last_error=$4, updated_at=now()
             WHERE collection_id=$1 AND status='running' AND lease_token=$2
            """,
            claim.collection_id,
            claim.lease_token,
            backoff_seconds,
            f"{type(error).__name__}: {error}"[:1000],
        )
    return result == "UPDATE 1"


async def _run_similarity_claim(pool: Any, claim: SimilarityClaim, settings: Any) -> None:
    owner = asyncio.current_task()
    assert owner is not None
    heartbeat_task = asyncio.create_task(
        _heartbeat_similarity_work(
            pool,
            claim=claim,
            lease_seconds=settings.similarity_job_lease_seconds,
            heartbeat_seconds=settings.similarity_job_heartbeat_seconds,
            owner_task=owner,
        )
    )
    try:
        await recompute_similarity_edges(
            pool,
            claim=claim,
            collection_id=claim.collection_id,
            run_id=f"similarity-{claim.collection_id}-{claim.request_seq}"[:40],
            settings=settings,
        )
        if not await finish_similarity_recompute(pool, claim=claim):
            raise RuntimeError("similarity recompute lease lost before acknowledgement")
    except asyncio.CancelledError:
        # A lost lease must not mutate the new owner's state.  Otherwise leave
        # the row running; expiry gives another worker a deterministic replay.
        raise
    except Exception as exc:
        await fail_similarity_recompute(
            pool,
            claim=claim,
            error=exc,
            backoff_seconds=settings.similarity_job_retry_backoff_seconds,
        )
        logger.warning(
            "similarity recompute failed for collection %s: %s",
            claim.collection_id,
            type(exc).__name__,
        )
    finally:
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass


async def similarity_recompute_loop(pool: Any, *, settings: Any) -> None:
    """Continuously drain durable per-collection recompute requests."""
    while True:
        try:
            claim = await claim_similarity_recompute(
                pool, lease_seconds=settings.similarity_job_lease_seconds
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("similarity recompute queue claim failed")
            await asyncio.sleep(settings.similarity_job_poll_seconds)
            continue
        if claim is None:
            await asyncio.sleep(settings.similarity_job_poll_seconds)
            continue
        work = asyncio.create_task(_run_similarity_claim(pool, claim, settings))
        try:
            await work
        except asyncio.CancelledError:
            supervisor = asyncio.current_task()
            if supervisor is not None and supervisor.cancelling():
                work.cancel()
                try:
                    await work
                except asyncio.CancelledError:
                    pass
                raise
            if work.cancelled():
                # Lease loss only cancels this child; the supervisor remains
                # alive and another worker can recover after expiry.
                continue
            raise
        except Exception:
            # `_run_similarity_claim` normally converts work failures back to
            # a pending row.  If even that DB release failed, keep the
            # supervisor alive; the lease-expiry path will recover the row.
            logger.exception("similarity recompute worker failed")
            await asyncio.sleep(settings.similarity_job_poll_seconds)
