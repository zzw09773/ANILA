"""Application factory for the AgenticRAG framework.

Usage:
    uvicorn agentic_rag.app_factory:app --host 0.0.0.0 --port 8000

Environment variables (see config.py for full list):
    LLM_URL         = https://172.16.120.35/v1
    MODEL           = google/gemma4
    EMBEDDING_URL   = https://172.16.120.35/v1
    EMBEDDING_MODEL = nvidia/NV-embed-V2
    VISION_URL      = https://172.16.120.35/v1
    VISION_MODEL    = meta/llama-4-maverick
    DATABASE_URL    = postgresql://csp_app:csp@csp-db:5432/csp
    RAG_AGENT_ID    = (positive int — the agent this container serves)
    API_KEY         = (optional bearer token)

Phase 2 Sprint 1 / Chunk F changes:
- Local ``storage.adapters.pg_pool`` and ``storage.adapters.pgvector_store``
  retired in favour of the central ``anila_core`` SDK. Same semantics,
  but the central path enforces RLS via ``SET LOCAL anila.agent_id``.
- Local ``ingestion.service.IngestionService`` (and the chunker / parser
  / docling pipeline behind it) is no longer wired into the FastAPI app
  — the ingestion-worker container is the canonical ingestion path now.
  Legacy code is left in the tree for evaluator reference but isn't
  imported by the entry points; CSP ``/api/ingestion/*`` + the worker
  pipeline replace it.
- ``init_pg_schema`` (sessions / messages tables only — never touched
  document_chunks) still runs to keep AgenticRAG's chat-side persistence
  working.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

# Central SDK — anila-core owns the document_chunks schema and its
# RLS-scoped accessor. AgenticRAG retrieves through these classes; it
# never imports the legacy local adapters anymore.
from anila_core.storage.adapters import AgentScopedPgVectorStore, PgPool

from .config import settings
from .providers.openai_compat import OpenAICompatProvider
from .providers.reranker import build_reranker_from_env
from .providers.vision import VisionProvider
from .router.tool_router import ToolRegistry
from .storage.adapters.postgres_store import initialize_schema as init_pg_schema
from .api.server import create_app

logger = logging.getLogger(__name__)

# Agent the container serves — used as RLS scope for every retrieval.
# Defaults to 0 (RAG disabled) if unset; ops MUST set this per deployment.
_RAG_AGENT_ID = int(os.environ.get("RAG_AGENT_ID", "0"))

# ---------------------------------------------------------------------------
# Shared state (initialised in lifespan)
# ---------------------------------------------------------------------------
_pg_pool: PgPool | None = None
_pgvector_store: AgentScopedPgVectorStore | None = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Initialize shared resources on startup, release on shutdown."""
    global _pg_pool, _pgvector_store

    logger.info(
        "Starting AgenticRAG — LLM: %s / model: %s",
        settings.llm_url,
        settings.model,
    )
    logger.info(
        "Embedding: %s / model: %s",
        settings.embedding_url,
        settings.embedding_model,
    )
    logger.info(
        "Vision:    %s / model: %s (enabled=%s)",
        settings.vision_url,
        settings.vision_model,
        settings.vision_enabled,
    )

    # Central anila-core PgPool. The ``open()`` API matches what was
    # previously called ``initialize()`` on the legacy adapter — same
    # semantics, asyncpg pool with vector + halfvec + jsonb codecs.
    _pg_pool = PgPool(
        dsn=settings.database_url,
        min_size=settings.pg_pool_min,
        max_size=settings.pg_pool_max,
    )
    await _pg_pool.open()

    # AgenticRAG's chat-side persistence (sessions / messages /
    # retrieval_traces) still uses postgres_store; init that schema.
    # ``document_chunks`` is NOT covered here — it's owned by CSP
    # alembic migration 0014/0015 and the central worker.
    await init_pg_schema(_pg_pool)

    if _RAG_AGENT_ID > 0:
        _pgvector_store = AgentScopedPgVectorStore(
            _pg_pool, agent_id=_RAG_AGENT_ID
        )
        logger.info(
            "AgentScopedPgVectorStore ready (agent_id=%d, dim=%d)",
            _RAG_AGENT_ID,
            settings.embedding_dimension,
        )
    else:
        logger.warning(
            "RAG_AGENT_ID not set — AgenticRAG running without retrieval."
        )

    yield

    if _pg_pool:
        await _pg_pool.close()


def build_app() -> FastAPI:
    """Build the FastAPI application with all RAG components wired up."""

    # LLM provider
    llm_provider = OpenAICompatProvider(
        base_url=settings.llm_url,
        api_key=settings.llm_api_key,
    )

    # Vision provider (VLM — maverick4 / gemma4-vision / etc.)
    vision_provider = (
        VisionProvider(
            base_url=settings.vision_url,
            api_key=settings.vision_api_key,
            model=settings.vision_model,
            verify_ssl=settings.vision_verify_ssl,
            max_image_bytes=settings.vision_max_image_bytes,
        )
        if settings.vision_enabled
        else None
    )
    _ = vision_provider  # currently unused outside legacy ingestion path.

    # Tool registry (empty by default — register tools externally)
    tool_registry = ToolRegistry()

    # Lazy references to stores (initialised in lifespan).
    # AgentScopedPgVectorStore replaces the legacy PgVectorStore but
    # exposes ``similarity_search`` / ``keyword_search`` rather than the
    # old DocumentStore Protocol — server.py's retrieval-provider
    # consumer adapts on attribute access.
    class _LazyStoreProxy:
        def __getattr__(self, name: str):
            if _pgvector_store is None:
                raise RuntimeError(
                    "AgentScopedPgVectorStore not initialized — set "
                    "RAG_AGENT_ID and ensure the lifespan has run."
                )
            return getattr(_pgvector_store, name)

    lazy_store = _LazyStoreProxy()

    class _LazyPoolProxy:
        def __getattr__(self, name: str):
            if _pg_pool is None:
                raise RuntimeError("pg pool not yet initialized")
            return getattr(_pg_pool, name)

    lazy_pool = _LazyPoolProxy()

    # Ingestion service is RETIRED in v0.6: the central ingestion-worker
    # container is the canonical ingestion path. Leaving these as None
    # turns off the ``/upload`` etc. endpoint registration in
    # server.create_app — see server.py's ``if ingestion_service is not
    # None`` gates.
    ingestion_service = None
    embedding_provider = None  # only the legacy ingestion path used it.

    # Optional cross-encoder reranker hosted on the internal vLLM server
    # (e.g. mxbai-rerank-large-v1 via /v1/score). Returns None when
    # RAG_RERANKER_ENABLED != "true" or when URL/MODEL is missing.
    reranker = build_reranker_from_env()
    if reranker is not None:
        logger.info("Reranker enabled in app_factory: %s", type(reranker).__name__)

    return create_app(
        provider=llm_provider,
        tool_registry=tool_registry,
        ingestion_service=ingestion_service,
        document_store=lazy_store,
        embedding_provider=embedding_provider,
        retrieval_provider=lazy_store,
        db_pool=lazy_pool,
        reranker=reranker,
        rerank_pool_multiplier=int(os.getenv("RAG_RERANK_POOL_MULTIPLIER", "3")),
        api_key=settings.api_key,
        api_dev_mode=settings.api_dev_mode,
        upload_dir=settings.upload_dir,
        csp_service_token=settings.csp_service_token,
    )


# ASGI entry point
app = build_app()
app.router.lifespan_context = lifespan
