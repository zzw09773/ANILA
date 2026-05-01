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
    RAG_COLLECTION_ID    = (positive int — the agent this container serves)
    API_KEY         = (optional bearer token)

Phase 0 decoupling (2026-05-02):
- Local ``storage.adapters.pg_pool`` and
  ``storage.adapters.pgvector_store`` re-introduced; we no longer
  import from ``anila_core``. AgenticRAG is a fork-template and must
  not pull platform-internal packages into the dev's environment.
- Platform deployments that want anila-core's RLS-aware variant inject
  it via the ``vector_store_override`` arg to ``build_app()`` — the
  default is the local ``CollectionScopedPgVectorStore`` here.
- ``init_pg_schema`` (sessions / messages tables only — never touches
  document_chunks) still runs to keep chat-side persistence working.
- The legacy ingestion service path remains retired in favour of the
  central ingestion-worker container; the leftover code is for
  evaluator reference only.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from .config import settings
from .providers.openai_compat import OpenAICompatProvider
from .providers.reranker import build_reranker_from_env
from .providers.vision import VisionProvider
from .router.tool_router import ToolRegistry
from .storage.adapters import CollectionScopedPgVectorStore, PgPool
from .storage.adapters.postgres_store import initialize_schema as init_pg_schema
from .storage.protocols import VectorStore
from .api.server import create_app

logger = logging.getLogger(__name__)

# Agent the container serves — used as RLS scope for every retrieval.
# Defaults to 0 (RAG disabled) if unset; ops MUST set this per deployment.
_RAG_COLLECTION_ID = int(os.environ.get("RAG_COLLECTION_ID", "0"))

# ---------------------------------------------------------------------------
# Shared state (initialised in lifespan)
# ---------------------------------------------------------------------------
_pg_pool: PgPool | None = None
_pgvector_store: VectorStore | None = None
_vector_store_factory: "_VectorStoreFactory | None" = None


# Plugin hook signature: given the open pool and the collection id,
# return any object satisfying the VectorStore Protocol. Set by
# ``build_app(vector_store_override=...)``; consumed inside lifespan.
class _VectorStoreFactory:
    def __init__(self, fn) -> None:
        self.fn = fn

    def build(self, pool: PgPool, collection_id: int) -> VectorStore:
        return self.fn(pool, collection_id)


def _default_vector_store_factory(pool: PgPool, collection_id: int) -> VectorStore:
    return CollectionScopedPgVectorStore(pool, collection_id=collection_id)


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

    # Local PgPool — same asyncpg pool with vector + halfvec + jsonb
    # codecs registered. Was briefly delegated to anila-core; reclaimed
    # in Phase 0 so the fork-template stays self-contained.
    _pg_pool = PgPool(
        dsn=settings.database_url,
        min_size=settings.pg_pool_min,
        max_size=settings.pg_pool_max,
    )
    await _pg_pool.open()

    # AgenticRAG's chat-side persistence (sessions / messages /
    # retrieval_traces) initialises here. ``document_chunks`` is NOT
    # covered — it's owned by CSP migration 0014/0015 and the central
    # ingestion-worker container.
    await init_pg_schema(_pg_pool)

    if _RAG_COLLECTION_ID > 0:
        factory = _vector_store_factory or _VectorStoreFactory(
            _default_vector_store_factory
        )
        _pgvector_store = factory.build(_pg_pool, _RAG_COLLECTION_ID)
        logger.info(
            "Vector store ready: %s (collection_id=%d, dim=%d)",
            type(_pgvector_store).__name__,
            _RAG_COLLECTION_ID,
            settings.embedding_dimension,
        )
    else:
        logger.warning(
            "RAG_COLLECTION_ID not set — AgenticRAG running without retrieval."
        )

    yield

    if _pg_pool:
        await _pg_pool.close()


def build_app(
    *,
    vector_store_override=None,
) -> FastAPI:
    """Build the FastAPI application with all RAG components wired up.

    Args:
        vector_store_override: optional callable
            ``(pool: PgPool, collection_id: int) -> VectorStore``.
            Platform deployments inject anila-core's RLS-aware impl
            here. When ``None``, the local
            ``CollectionScopedPgVectorStore`` is used.
    """
    global _vector_store_factory
    if vector_store_override is not None:
        _vector_store_factory = _VectorStoreFactory(vector_store_override)
    else:
        _vector_store_factory = _VectorStoreFactory(_default_vector_store_factory)

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
    # CollectionScopedPgVectorStore replaces the legacy PgVectorStore but
    # exposes ``similarity_search`` / ``keyword_search`` rather than the
    # old DocumentStore Protocol — server.py's retrieval-provider
    # consumer adapts on attribute access.
    class _LazyStoreProxy:
        def __getattr__(self, name: str):
            if _pgvector_store is None:
                raise RuntimeError(
                    "CollectionScopedPgVectorStore not initialized — set "
                    "RAG_COLLECTION_ID and ensure the lifespan has run."
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
