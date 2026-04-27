"""Application factory for anila-core — pure chat / agent runtime.

Sprint 1 boundary cleanup (anila-core-boundary.md §2.3) collapsed this
factory from a "RAG stack" to a chat-only / agent-only runtime. The
heavy lifting that used to live here — pg_pool init, pgvector schema
bootstrap, IngestionService composition, NvidiaEmbeddingProvider wiring,
chunker construction — is gone. RAG agents fork the AgenticRAG template,
which carries its own `app_factory.py` with the full ingestion stack.

Usage::

    uvicorn anila_core.app_factory:app --host 0.0.0.0 --port 8000

Environment variables (see config.py for the full list)::

    LLM_URL = https://172.16.120.35/v1
    MODEL   = google/gemma4
    API_KEY = (optional bearer token for ApiKeyMiddleware)
"""

from __future__ import annotations

import logging

from fastapi import FastAPI

from .config import settings
from .providers.openai_compat import OpenAICompatProvider
from .router.tool_router import ToolRegistry
from .api.server import create_app

logger = logging.getLogger(__name__)


def build_app() -> FastAPI:
    """Build the FastAPI application — chat / agent runtime only.

    Hosts that need RAG (or any other set of tools) construct their own
    ToolRegistry and pass it via ``create_app``. This factory does not
    register any tools by default.
    """
    logger.info(
        "Starting anila-core (chat-only) — LLM: %s / model: %s",
        settings.llm_url,
        settings.model,
    )

    llm_provider = OpenAICompatProvider(
        base_url=settings.llm_url,
        api_key=settings.llm_api_key,
    )

    tool_registry = ToolRegistry()

    return create_app(
        provider=llm_provider,
        tool_registry=tool_registry,
        api_key=settings.api_key,
        api_dev_mode=settings.api_dev_mode,
    )


# ASGI entry point
app = build_app()
