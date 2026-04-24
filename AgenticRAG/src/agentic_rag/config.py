"""Unified configuration for AgenticRAG Framework.

All settings can be overridden by environment variables (case-insensitive).
Environment variable names match the field names:
    LLM_URL, LLM_API_KEY, MODEL, EMBEDDING_MODEL, VISION_MODEL,
    DATABASE_URL, etc.

Example .env:
    LLM_URL=https://172.16.120.35/v1
    MODEL=google/gemma4
    EMBEDDING_MODEL=nvidia/NV-embed-V2
    VISION_URL=https://172.16.120.35/v1
    VISION_MODEL=meta/llama-4-maverick
    DATABASE_URL=postgresql://agentic:secret@localhost/agentic_rag
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
            default="nvidia/NV-embed-V2",
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

        # ── Vision (VLM) ──────────────────────────────────────────────
        # Independent endpoint so teams can point at a VLM (maverick4 /
        # gemma4 with vision) without swapping the text LLM.
        vision_url: str = Field(
            default="https://172.16.120.35/v1",
            description="Base URL of the vision-capable LLM endpoint (OpenAI-compatible).",
        )
        vision_api_key: str = Field(
            default="not-set",
            description="API key / Bearer token for the vision endpoint.",
        )
        vision_model: str = Field(
            default="meta/llama-4-maverick",
            description="Vision-capable model identifier (e.g. maverick4, gemma4-vision).",
        )
        vision_verify_ssl: bool = Field(
            default=False,
            description="Verify TLS certificates for the vision endpoint.",
        )
        vision_max_image_bytes: int = Field(
            default=8 * 1024 * 1024,
            description="Maximum image size sent to the VLM (bytes).",
        )
        vision_enabled: bool = Field(
            default=True,
            description="Globally enable/disable VLM calls during ingestion.",
        )

        # ── PostgreSQL ────────────────────────────────────────────────
        database_url: str = Field(
            default="postgresql://agentic:agentic@localhost:5432/agentic_rag",
            description="PostgreSQL DSN (asyncpg format).",
        )
        pg_pool_min: int = Field(default=2, description="Min asyncpg pool connections.")
        pg_pool_max: int = Field(default=10, description="Max asyncpg pool connections.")
        pg_ssl: str = Field(
            default="disable",
            description="SSL mode for PostgreSQL (disable/require/verify-ca).",
        )

        # ── Chunking ──────────────────────────────────────────────────
        # The hierarchical chunker splits by document structure (headings →
        # sub-headings → paragraphs → images), not by fixed size. These
        # values are fall-back caps for oversized paragraphs only.
        chunk_size: int = Field(
            default=1024,
            description="Soft cap for oversized leaf chunks (tokens).",
        )
        chunk_overlap: int = Field(
            default=64,
            description="Token overlap when a leaf has to be sub-split by size.",
        )

        # ── RAG Retrieval ─────────────────────────────────────────────
        rag_top_k: int = Field(default=5, description="Number of chunks to retrieve per query.")
        rag_min_score: float = Field(
            default=0.7, description="Minimum cosine similarity score."
        )
        rag_include_parent_context: bool = Field(
            default=True,
            description="Attach the full parent chunk content to every Citation.",
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
            default="/tmp/agentic_rag_uploads",
            description="Temporary directory for uploaded files.",
        )

        # ── ANILA / CSP Platform Integration ──────────────────────────
        # Populated automatically when this agent is registered behind
        # myCSPPlatform. When empty the CSP middleware runs in
        # pass-through dev mode so the template stays clone-and-run.
        csp_service_token: Optional[str] = Field(
            default=None,
            description="Expected value of the X-CSP-Service-Token header "
            "injected by myCSPPlatform. None/empty disables the check.",
        )
        csp_base_url: str = Field(
            default="",
            description="When set, LLM / embedding / vision calls are routed "
            "through CSP instead of a direct vLLM endpoint.",
        )
        csp_api_key: str = Field(
            default="",
            description="Bearer token presented to CSP for proxied LLM calls.",
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
        embedding_model: str = "nvidia/NV-embed-V2"
        embedding_dimension: int = 4096
        embedding_verify_ssl: bool = False
        vision_url: str = "https://172.16.120.35/v1"
        vision_api_key: str = "not-set"
        vision_model: str = "meta/llama-4-maverick"
        vision_verify_ssl: bool = False
        vision_max_image_bytes: int = 8 * 1024 * 1024
        vision_enabled: bool = True
        database_url: str = "postgresql://agentic:agentic@localhost:5432/agentic_rag"
        pg_pool_min: int = 2
        pg_pool_max: int = 10
        pg_ssl: str = "disable"
        chunk_size: int = 1024
        chunk_overlap: int = 64
        rag_top_k: int = 5
        rag_min_score: float = 0.7
        rag_include_parent_context: bool = True
        api_key: Optional[str] = None
        api_dev_mode: bool = False
        upload_dir: str = "/tmp/agentic_rag_uploads"
        csp_service_token: Optional[str] = None
        csp_base_url: str = ""
        csp_api_key: str = ""


# Module-level singleton — import and use directly:
#   from agentic_rag.config import settings
settings = Settings()
