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


_REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379")

# Module-level singleton, created lazily on first enqueue.
_pool: ArqRedis | None = None


async def _get_pool() -> ArqRedis:
    global _pool
    if _pool is None:
        _pool = await create_pool(RedisSettings.from_dsn(_REDIS_URL))
    return _pool


async def enqueue_ingest_document(document_id: int) -> str:
    """Enqueue an ``ingest_document`` job for one document.

    Returns the Arq job id (a UUID string) so the API can surface it in
    the ``ingestion_jobs`` row and let the dev UI poll for completion.
    """
    pool = await _get_pool()
    job = await pool.enqueue_job("ingest_document", document_id)
    if job is None:
        # Arq returns None when a duplicate job_id collides; we don't
        # set an explicit one, so this branch is theoretically
        # unreachable. Surface as a clear error if it ever fires.
        raise RuntimeError(
            "Arq returned no job — possible duplicate id collision. "
            "Investigate the redis 'arq:' keys."
        )
    return job.job_id


async def enqueue_with_metadata(
    document_id: int,
) -> dict[str, Any]:
    """Convenience wrapper returning what the API row insert needs."""
    job_id = await enqueue_ingest_document(document_id)
    return {"arq_job_id": job_id}
