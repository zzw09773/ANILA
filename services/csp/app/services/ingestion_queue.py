"""Arq queue client used by the CSP API to enqueue ingestion jobs.

The CSP backend never imports ingestion_worker code directly — it only
needs to push job IDs into redis. Arq's ``redis.create_pool`` returns a
client that knows how to enqueue functions registered on the worker
side (the worker package must declare those function names; we keep
ours identical to the importable symbol on the worker).

Lifecycle: a single pool is opened on FastAPI startup and reused
across requests. There's no graceful close on shutdown in Sprint 1
(redis closes connections itself); Sprint 2 will plumb a proper
lifespan when we also add SSE for progress.
"""

from __future__ import annotations

import os
from typing import Any

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings
from anila_security import create_queue_proof

from app.config import settings


_REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379")

# Module-level singleton, created lazily on first enqueue.
_pool: ArqRedis | None = None


async def _get_pool() -> ArqRedis:
    global _pool
    if _pool is None:
        _pool = await create_pool(RedisSettings.from_dsn(_REDIS_URL))
    return _pool


def _require_positive_int(value: object, *, field: str) -> int:
    """Validate an integer identity used by the durable worker contract."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


async def enqueue_ingest_document(
    document_id: int,
    *,
    ingestion_job_id: int,
    attempt_number: int,
) -> str:
    """Enqueue one *fenced* ``ingest_document`` attempt.

    This compatibility helper is kept for callers that still publish
    directly, but it may not create a legacy job with no durable identity.
    The job identity and attempt are included in both the worker arguments
    and the HMAC payload, matching the transactional ingestion outbox
    contract.  The worker then claims the corresponding lease before any
    ingestion mutation.
    """
    document_id = _require_positive_int(document_id, field="document_id")
    ingestion_job_id = _require_positive_int(
        ingestion_job_id, field="ingestion_job_id"
    )
    attempt_number = _require_positive_int(
        attempt_number, field="attempt_number"
    )
    pool = await _get_pool()
    payload = {
        "document_id": document_id,
        "ingestion_job_id": ingestion_job_id,
        "attempt_number": attempt_number,
    }
    proof = create_queue_proof(
        settings.INGESTION_QUEUE_HMAC_KEY,
        task_name="ingest_document",
        payload=payload,
    )
    job = await pool.enqueue_job(
        "ingest_document",
        document_id,
        ingestion_job_id,
        attempt_number,
        proof,
        _job_id=f"ingest-job-{ingestion_job_id}-attempt-{attempt_number}",
    )
    if job is None:
        # Arq returns None when the deterministic durable job id already
        # exists. Surface as a clear error rather than returning an
        # unverified enqueue result to the caller.
        raise RuntimeError(
            "Arq returned no job — possible duplicate id collision. "
            "Investigate the redis 'arq:' keys."
        )
    return job.job_id


async def enqueue_with_metadata(
    document_id: int,
    *,
    ingestion_job_id: int,
    attempt_number: int,
) -> dict[str, Any]:
    """Return the Arq identity for a fenced ingestion attempt."""
    job_id = await enqueue_ingest_document(
        document_id,
        ingestion_job_id=ingestion_job_id,
        attempt_number=attempt_number,
    )
    return {"arq_job_id": job_id}


async def enqueue_evaluator_run(eval_run_id: int) -> str:
    """Enqueue an ``evaluate_strategies`` job (Sprint 3 Chunk N).

    The worker reads ``ingestion_eval_runs`` and writes results back
    in the same row — caller polls via the GET endpoint.
    """
    pool = await _get_pool()
    payload = {"eval_run_id": eval_run_id}
    proof = create_queue_proof(
        settings.INGESTION_QUEUE_HMAC_KEY,
        task_name="evaluate_strategies",
        payload=payload,
    )
    job = await pool.enqueue_job("evaluate_strategies", eval_run_id, proof)
    if job is None:
        raise RuntimeError(
            "Arq returned no job — possible duplicate id collision."
        )
    return job.job_id


async def enqueue_reresolve_relations(collection_id: int, actor_user_id: int) -> str:
    """Enqueue a ``reresolve_collection_relations`` job (document-relations §8).

    The worker re-parses every document in the collection, re-extracts rule
    citation edges (delete-then-insert, manual untouched) and reconciles. The
    API has already run the synchronous reconcile; this refreshes the '重抽'
    half asynchronously.
    """
    pool = await _get_pool()
    payload = {
        "collection_id": collection_id,
        "actor_user_id": actor_user_id,
    }
    proof = create_queue_proof(
        settings.INGESTION_QUEUE_HMAC_KEY,
        task_name="reresolve_collection_relations",
        payload=payload,
    )
    job = await pool.enqueue_job(
        "reresolve_collection_relations", collection_id, actor_user_id, proof,
    )
    if job is None:
        raise RuntimeError(
            "Arq returned no job — possible duplicate id collision."
        )
    return job.job_id
