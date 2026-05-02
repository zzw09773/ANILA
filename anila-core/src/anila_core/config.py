"""Unified configuration for anila-core (chat / agent runtime only).

Sprint 1 boundary cleanup (anila-core-boundary.md §2.3) removed the
RAG-specific fields:

    embedding_*       (moved to AgenticRAG template)
    database_url      (anila-core no longer talks to PG directly)
    pg_pool_*, pg_ssl
    chunk_size, chunk_overlap
    rag_top_k, rag_min_score
    upload_dir        (the /upload endpoint is gone)

Forks (e.g. AgenticRAG) carry their own config module with these fields.

All settings can be overridden by environment variables (case-insensitive).
Field names map directly to env vars: LLM_URL, MODEL, API_KEY, etc.

Example .env::

    LLM_URL = https://172.16.120.35/v1
    MODEL   = google/gemma4
    API_KEY = my-secret-key
"""

from __future__ import annotations

from typing import Optional

try:
    from pydantic_settings import BaseSettings  # type: ignore[import]
    from pydantic import Field
    from pydantic_settings import SettingsConfigDict  # type: ignore[import]

    class Settings(BaseSettings):
        """Application-wide configuration loaded from environment variables."""

        # ── LLM Provider ──────────────────────────────────────────────
        llm_url: str = Field(
            default="https://172.16.120.35/v1",
            description="Base URL of the OpenAI-compatible LLM endpoint.",
        )
        llm_api_key: str = Field(
            default="not-set",
            description="API key / Bearer token for the LLM endpoint.",
        )
        model: str = Field(
            default="google/gemma4",
            description="Model identifier sent to the LLM provider.",
        )

        # ── CSP Platform (Data Plane) ─────────────────────────────────
        csp_base_url: str = Field(
            default="http://localhost:8000",
            description="Base URL of the myCSPPlatform data plane.",
        )
        csp_api_key: str = Field(
            default="not-set",
            description="CSP API Key (sk-...) used by Router / agents to call CSP proxy.",
        )
        csp_service_token: Optional[str] = Field(
            default=None,
            description="Service-to-service token CSP injects; agents verify this header.",
        )

        # ── API / Auth ────────────────────────────────────────────────
        api_key: Optional[str] = Field(
            default=None,
            description="Legacy bearer token (kept for local dev without CSP).",
        )
        api_dev_mode: bool = Field(
            default=False,
            description="Disable auth checks when True (development only).",
        )

        # ── Sessions (Sprint 9) ───────────────────────────────────────
        session_db_path: str = Field(
            default="./.anila/sessions.db",
            description=(
                "SQLite path for the default Session adapter. Override "
                "with ANILA_SESSION_DB_PATH or set to ':memory:' for "
                "tests. Multi-process deployments should swap to a "
                "Postgres / Redis Session implementation."
            ),
        )

        model_config = SettingsConfigDict(
            env_file=".env",
            env_file_encoding="utf-8",
            case_sensitive=False,
        )

except ImportError:
    # Fallback when pydantic-settings is not installed
    from pydantic import BaseModel, Field  # type: ignore[assignment]

    class Settings(BaseModel):  # type: ignore[no-redef]
        llm_url: str = "https://172.16.120.35/v1"
        llm_api_key: str = "not-set"
        model: str = "google/gemma4"
        csp_base_url: str = "http://localhost:8000"
        csp_api_key: str = "not-set"
        csp_service_token: Optional[str] = None
        api_key: Optional[str] = None
        api_dev_mode: bool = False
        session_db_path: str = "./.anila/sessions.db"


# Module-level singleton — import and use directly:
#   from anila_core.config import settings
settings = Settings()
