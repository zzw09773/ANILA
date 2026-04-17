"""Unified configuration for ANILA Core.

All settings can be overridden by environment variables (case-insensitive).
Environment variable names match the field names:
    LLM_URL, LLM_API_KEY, MODEL, EMBEDDING_MODEL, DATABASE_URL, etc.

Example .env:
    LLM_URL=https://172.16.120.35/v1
    MODEL=google/gemma4
    EMBEDDING_MODEL=Nvidia/NV-embed-V2
    DATABASE_URL=postgresql://anila:secret@localhost/anila_rag
    API_KEY=my-secret-key
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

        # ── Embedding ──────────────────────────────────────────────────
        embedding_url: str = Field(
            default="https://172.16.120.35/v1",
            description="Base URL of the embedding endpoint (OpenAI-compatible).",
        )
        embedding_api_key: str = Field(
            default="not-set",
            description="API key / Bearer token for the embedding endpoint.",
        )
        embedding_model: str = Field(
            default="Nvidia/NV-embed-V2",
            description="Embedding model identifier.",
        )
        embedding_dimension: int = Field(
            default=4096,
            description="Embedding vector dimension (NV-Embed-V2 = 4096).",
        )
        embedding_verify_ssl: bool = Field(
            default=False,
            description="Verify TLS certificates for the embedding endpoint.",
        )

        # ── PostgreSQL ────────────────────────────────────────────────
        database_url: str = Field(
            default="postgresql://anila:anila@localhost:5432/anila_rag",
            description="PostgreSQL DSN (asyncpg format).",
        )
        pg_pool_min: int = Field(default=2, description="Min asyncpg pool connections.")
        pg_pool_max: int = Field(default=10, description="Max asyncpg pool connections.")
        pg_ssl: str = Field(
            default="disable",
            description="SSL mode for PostgreSQL (disable/require/verify-ca).",
        )

        # ── Chunking ──────────────────────────────────────────────────
        chunk_size: int = Field(default=512, description="Target chunk size in tokens.")
        chunk_overlap: int = Field(default=50, description="Token overlap between chunks.")

        # ── RAG Retrieval ─────────────────────────────────────────────
        rag_top_k: int = Field(default=5, description="Number of chunks to retrieve per query.")
        rag_min_score: float = Field(
            default=0.7, description="Minimum cosine similarity score."
        )

        # ── API / Auth ────────────────────────────────────────────────
        api_key: Optional[str] = Field(
            default=None,
            description="Bearer token required for API access (None = auth disabled).",
        )
        api_dev_mode: bool = Field(
            default=False,
            description="Disable auth checks when True (development only).",
        )
        upload_dir: str = Field(
            default="/tmp/anila_uploads",
            description="Temporary directory for uploaded files.",
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
        embedding_url: str = "https://172.16.120.35/v1"
        embedding_api_key: str = "not-set"
        embedding_model: str = "Nvidia/NV-embed-V2"
        embedding_dimension: int = 4096
        embedding_verify_ssl: bool = False
        database_url: str = "postgresql://anila:anila@localhost:5432/anila_rag"
        pg_pool_min: int = 2
        pg_pool_max: int = 10
        pg_ssl: str = "disable"
        chunk_size: int = 512
        chunk_overlap: int = 50
        rag_top_k: int = 5
        rag_min_score: float = 0.7
        api_key: Optional[str] = None
        api_dev_mode: bool = False
        upload_dir: str = "/tmp/anila_uploads"


# Module-level singleton — import and use directly:
#   from anila_core.config import settings
settings = Settings()
