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
    DATABASE_URL    = postgresql://agentic:agentic@localhost:5432/agentic_rag
    API_KEY         = (optional bearer token)
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

import os

from .config import settings
from .ingestion.service import IngestionService
from .ingestion.chunker import HierarchicalChunker
from .providers.openai_compat import OpenAICompatProvider
from .providers.embedding_nvidia import NvidiaEmbeddingProvider
from .providers.reranker import build_reranker_from_env
from .providers.vision import VisionProvider
from .router.tool_router import ToolRegistry
from .storage.adapters.pg_pool import PgPool
from .storage.adapters.pgvector_store import PgVectorStore
from .storage.adapters.postgres_store import initialize_schema as init_pg_schema
from .api.server import create_app

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared state (initialised in lifespan)
# ---------------------------------------------------------------------------
_pg_pool: PgPool | None = None
_pgvector_store: PgVectorStore | None = None


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

    # PostgreSQL pool
    _pg_pool = PgPool(
        dsn=settings.database_url,
        min_size=settings.pg_pool_min,
        max_size=settings.pg_pool_max,
        ssl=settings.pg_ssl,
    )
    await _pg_pool.initialize()
    await init_pg_schema(_pg_pool)

    # pgvector store (doubles as DocumentStore + RetrievalProvider)
    _pgvector_store = PgVectorStore(pool=_pg_pool, dimension=settings.embedding_dimension)
    await _pgvector_store.initialize_schema()

    yield

    # Shutdown
    if _pg_pool:
        await _pg_pool.close()


def build_app() -> FastAPI:
    """Build the FastAPI application with all RAG components wired up."""

    # LLM provider
    llm_provider = OpenAICompatProvider(
        base_url=settings.llm_url,
        api_key=settings.llm_api_key,
    )

    # Embedding provider (NV-Embed-V2)
    embedding_provider = NvidiaEmbeddingProvider(
        base_url=settings.embedding_url,
        api_key=settings.embedding_api_key,
        model=settings.embedding_model,
        verify_ssl=settings.embedding_verify_ssl,
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

    # Tool registry (empty by default — register tools externally)
    tool_registry = ToolRegistry()

    # Hierarchical chunker — tree shape follows document structure, not
    # a fixed token budget; chunk_size is only a soft cap for oversized
    # paragraphs.
    chunker = HierarchicalChunker(
        max_leaf_tokens=settings.chunk_size,
        overlap_tokens=settings.chunk_overlap,
    )

    # Lazy references to stores (initialised in lifespan)
    class _LazyStoreProxy:
        """Proxy that forwards calls to _pgvector_store after lifespan init."""

        def __getattr__(self, name: str):
            if _pgvector_store is None:
                raise RuntimeError("pgvector store not yet initialized")
            return getattr(_pgvector_store, name)

    lazy_store = _LazyStoreProxy()

    class _LazyPoolProxy:
        """Proxy that forwards calls to _pg_pool after lifespan init."""

        def __getattr__(self, name: str):
            if _pg_pool is None:
                raise RuntimeError("pg pool not yet initialized")
            return getattr(_pg_pool, name)

    lazy_pool = _LazyPoolProxy()

    # Ingestion service
    ingestion_service = IngestionService(
        embedding_provider=embedding_provider,
        document_store=lazy_store,
        retrieval_provider=lazy_store,
        vision_provider=vision_provider,
        chunker=chunker,
    )

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
    )


# ASGI entry point
app = build_app()
app.router.lifespan_context = lifespan
