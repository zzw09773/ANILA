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
- ``max_tries=3`` — covers transient embedding timeouts and DB
  connection blips. Anything terminal (E_PARSE_*, E_PG_RLS_VIOLATION,
  E_EMBED_DIM_MISMATCH) doesn't benefit from retries; the handler marks
  the document as 'failed' and the retry happens but does the same work
  with the same outcome. A future Sprint will key retries off
  ``IngestionError.retryable`` to skip non-retryable codes entirely.
  ⚠ ``E_PARSE_REMOTE_DOWN`` 是 E_PARSE_* 前綴的**例外**:遠端 parser 端點
  (docling) 的故障是基礎設施、retryable=True,**會**受益於重試。未來照這個
  前綴規則實作重試時,別把它歸進 terminal 列舉。
- ``job_timeout=300`` — 5 minutes per ingest. A 50 MB PDF with 5k chunks
  through a slow embedding endpoint can comfortably take 2-3 minutes.
"""

from __future__ import annotations

from arq.connections import RedisSettings

from anila_core.storage.adapters.pg_pool import PgPool

from ingestion_worker.embedder import Embedder
from ingestion_worker.evaluator import evaluate_strategies
from ingestion_worker.handlers import ingest_document, reresolve_collection_relations
from ingestion_worker.settings import settings


async def on_startup(ctx: dict) -> None:
    """Open pool + embedder once per worker process."""
    import logging

    from ingestion_worker.credential_file import credential_health

    snap = credential_health()
    logging.getLogger(__name__).info(
        "ingestion-worker credential token_source=%s", snap["token_source"]
    )
    ctx["credential_health"] = snap
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


async def on_shutdown(ctx: dict) -> None:
    """Drain pool + embedder. Idempotent."""
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
    max_tries = 3
    job_timeout = 300  # seconds
    keep_result = 3600  # 1h — let CSP poll completion within an hour
