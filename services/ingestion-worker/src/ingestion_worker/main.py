"""Arq worker entry point.

Run with::

    arq ingestion_worker.main.WorkerSettings

Lifecycle:
- ``on_startup``: open the shared PgPool and construct the Embedder.
  Both go into ``ctx`` so handlers can grab them without per-job init
  overhead.
- ``on_shutdown``: close pool + embedder cleanly so docker stop signals
  graceful shutdown rather than half-closed connections.

Job retry policy:
- ``retry_jobs=False`` / ``max_tries=1`` prevents Arq from creating an
  independent retry stream. The DB lease state machine owns every retry and
  creates one deterministic outbox intent per logical attempt.
- ``job_timeout`` is explicit posture. Cancellation records a durable retry;
  hard process loss is recovered by the expired-lease reaper.
"""

from __future__ import annotations

import asyncio
import time
from arq.connections import RedisSettings

from anila_core.storage.adapters.pg_pool import PgPool

from ingestion_worker.embedder import Embedder
from ingestion_worker.evaluator import evaluate_strategies
from ingestion_worker.handlers import ingest_document, reresolve_collection_relations
from ingestion_worker import job_state
from ingestion_worker.health_server import start_server
from ingestion_worker.similarity_relations import similarity_recompute_loop
from ingestion_worker.settings import settings


async def on_startup(ctx: dict) -> None:
    """Open pool + embedder once per worker process."""
    pool = PgPool(
        settings.database_url,
        min_size=settings.pg_pool_min,
        max_size=settings.pg_pool_max,
    )
    await pool.open()
    ctx["pool"] = pool
    # Sprint 5 / Chunk W: usage tracking moved to CSP-side. Embedder
    # routes through ``http://csp:8000/v1/embeddings`` with the
    # ``ingestion-worker`` system API key; CSP's proxy_service writes
    # token_usage rows with request_type='embedding' on every call.
    ctx["embedder"] = Embedder(settings)
    ctx["reaper_task"] = asyncio.create_task(
        job_state.reaper_loop(
            pool,
            interval_seconds=settings.job_reaper_interval_seconds,
            limit=settings.job_reaper_batch_size,
            backoff_seconds=settings.job_retry_backoff_seconds,
            lease_grace_seconds=settings.job_lease_grace_seconds,
        )
    )
    ctx["similarity_recompute_task"] = asyncio.create_task(
        similarity_recompute_loop(pool, settings=settings)
    )
    ctx["health_server"] = await start_server(
        ctx,
        port=settings.metrics_port,
        timeout=settings.health_probe_timeout_seconds,
    )

    async def heartbeat_process() -> None:
        while True:
            ctx["process_heartbeat"] = time.monotonic()
            await asyncio.sleep(5)

    ctx["process_heartbeat_task"] = asyncio.create_task(heartbeat_process())


async def on_shutdown(ctx: dict) -> None:
    """Drain pool + embedder. Idempotent."""
    for key in (
        "reaper_task",
        "similarity_recompute_task",
        "process_heartbeat_task",
    ):
        task = ctx.get(key)
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    server = ctx.get("health_server")
    if server is not None:
        server.close()
        await server.wait_closed()
    pool: PgPool | None = ctx.get("pool")
    if pool is not None:
        await pool.close()
    embedder: Embedder | None = ctx.get("embedder")
    if embedder is not None:
        await embedder.close()


class WorkerSettings:
    """Arq config — discovered by ``arq <module>:WorkerSettings``."""

    functions = [ingest_document, evaluate_strategies, reresolve_collection_relations]
    on_startup = on_startup
    on_shutdown = on_shutdown
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    # Arq retries would race the durable DB/outbox attempt state machine.
    retry_jobs = False
    max_tries = 1
    job_timeout = settings.job_timeout_seconds
    keep_result = 3600  # 1h — let CSP poll completion within an hour
