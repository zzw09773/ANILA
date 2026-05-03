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

    # ── VLM-based image captioning ─────────────────────────────────────
    #
    # PDF parsers extract embedded images and leave ``[[IMAGE:<id>]]``
    # placeholders in the text. Without VLM captioning these become
    # opaque tokens that the chunker stuffs in as ``[image]`` — meaning
    # any chart, diagram, or table embedded in a text-PDF disappears
    # from retrieval.
    #
    # When ``enable_image_captions=True`` AND ``vision_url`` is non-empty
    # the worker calls the VLM endpoint per-image (concurrency-capped)
    # and rewrites every ``[[IMAGE:<id>]]`` placeholder with the caption
    # text BEFORE chunking. Both flags must be on; either off → captioning
    # is skipped and a one-line info log explains why. The placeholders
    # become "[image]" via the chunker's existing fallback so retrieval
    # still works, just without the chart's content.
    #
    # Endpoint conventions match the existing OCR fallback (vision.py):
    # ``vision_url`` is the OpenAI-compatible base URL (no
    # ``/chat/completions`` suffix). Default points at CSP's ``/v1`` proxy
    # so token usage is metered consistently with embeddings — same
    # internal API key flow.
    enable_image_captions: bool = Field(
        default=True,
        description=(
            "Master switch for VLM caption injection. Set False to skip "
            "captioning entirely (e.g. for text-only knowledge bases "
            "where the latency cost is not worth it)."
        ),
    )
    vision_url: str = Field(
        default="",
        description=(
            "OpenAI-compatible VLM endpoint base URL (no /chat/completions "
            "suffix). Empty disables image captioning even when "
            "enable_image_captions=True; this is the safe default for "
            "deployments without a configured VLM."
        ),
    )
    vision_model: str = Field(
        default="gemma4",
        description="VLM identifier passed in the chat-completions body.",
    )
    vision_api_key: str = Field(
        default="not-set",
        description=(
            "Bearer token for the VLM endpoint. Re-uses the same internal "
            "platform API key the embedding path uses by default."
        ),
    )
    vision_verify_ssl: bool = Field(
        default=False,
        description=(
            "Verify TLS for the VLM endpoint. Defaults False because the "
            "internal CSP nginx uses a self-signed cert in dev; flip to "
            "True once a real cert is in place."
        ),
    )
    vision_concurrency: int = Field(
        default=4,
        description=(
            "Max parallel VLM calls per ingest job. Higher → faster on "
            "image-heavy PDFs but risks starving the GPU if other "
            "callers (Studio QA, OCR) share the same VLM endpoint."
        ),
    )
    vision_timeout_seconds: float = Field(
        default=60.0,
        description="Per-image VLM request timeout.",
    )
    vision_max_image_bytes: int = Field(
        default=8 * 1024 * 1024,
        description=(
            "Skip captioning images larger than this (bytes). The VLM "
            "endpoint OOMs on huge PNGs; the chunker's placeholder "
            "caption is the fallback in that case."
        ),
    )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )


settings = WorkerSettings()
