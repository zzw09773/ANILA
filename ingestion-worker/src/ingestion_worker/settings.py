"""Worker configuration loaded from env.

We reuse the same env-var conventions the CSP backend already exports
so the docker-compose service can share its env block with minimal
duplication.

- ``DATABASE_URL`` points at csp-db **as csp_app** (non-superuser, RLS
  enforced). The worker never needs the ``csp`` superuser — its job is
  scoped INSERTs / UPDATEs, not DDL.
- ``REDIS_URL`` is the queue. Single-DB, no auth, dev-default
  ``redis://redis:6379``.
- ``EMBEDDING_BASE_URL`` + ``EMBEDDING_MODEL`` — OpenAI-compatible
  endpoint. Sprint 1 pins the output dim to 1536 (truncated NV-embed-V2
  via Matryoshka, or any other 1536-d model). The dim must match the
  ``vector(1536)`` column or asyncpg raises at INSERT time.
- ``UPLOAD_DIR`` — shared mount where CSP writes uploaded blobs and the
  worker reads them. Both services bind-mount the same host path.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class WorkerSettings(BaseSettings):
    database_url: str = Field(
        default="postgresql://csp_app:csp@csp-db:5432/csp",
        description="asyncpg DSN to csp-db; MUST be the csp_app role for RLS.",
    )
    redis_url: str = Field(
        default="redis://redis:6379",
        description="Arq queue backing store.",
    )

    embedding_base_url: str = Field(
        default="http://host.docker.internal:7011/v1",
        description=(
            "OpenAI-compatible embedding endpoint base URL. Default points at "
            "the on-host embedding-proxy container (port 7011) which serves "
            "nvidia/NV-embed-V2 with a /v1/embeddings shape."
        ),
    )
    embedding_model: str = Field(
        default="nvidia/NV-embed-V2",
        description="Embedding model identifier passed to the endpoint.",
    )
    embedding_api_key: str = Field(
        default="not-set",
        description="Bearer token for the embedding endpoint (if required).",
    )
    embedding_dim: int = Field(
        default=4000,
        description=(
            "Target dimension after client-side truncation. Must match "
            "document_chunks.embedding column (currently halfvec(4000), "
            "see migration 0015). NV-embed-V2's 4096-d native output is "
            "truncated to 4000 because halfvec HNSW caps at 4000-d."
        ),
    )
    embedding_timeout_seconds: float = Field(
        default=30.0,
        description="Per-request embedding timeout.",
    )

    upload_dir: str = Field(
        default="/var/anila/ingestion-uploads",
        description=(
            "Filesystem location where CSP writes uploaded blobs and the "
            "worker reads them. Bind-mounted from the host into both "
            "containers."
        ),
    )

    # Pool sizing. Worker concurrency is capped at max_size so we never
    # block on connection acquire when many jobs run in parallel.
    pg_pool_min: int = 1
    pg_pool_max: int = 5

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )


settings = WorkerSettings()
