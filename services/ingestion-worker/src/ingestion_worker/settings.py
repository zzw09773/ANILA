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

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class WorkerSettings(BaseSettings):
    anila_deployment_profile: str = Field(default="development")
    ingestion_queue_hmac_key: str = Field(
        default=""
    )
    # Normal deployments preserve the existing inference feature set. The
    # signed-pilot overlay pins this true and the escape hatch below false.
    anila_pilot_mode: bool = Field(default=False)
    # Gate 2 signed pilot hard stop.  Direct relation/judge endpoints are
    # inventoried but not yet CSP-mediated; formal compose pins this false.
    gate2_allow_unconverged_inference: bool = Field(default=False)
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
    embedding_model_fingerprint: str = Field(
        default="",
        pattern="^$|^sha256:[0-9a-f]{64}$",
        description=(
            "Explicit SHA-256 fingerprint of deployed embedding weights. "
            "The ingestion boundary rejects an empty value."
        ),
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
    embedding_source_dim: int | None = Field(
        default=None,
        ge=1,
        le=4000,
        description=(
            "Native output dimension of the deployed embedding model, when it "
            "is smaller than the storage contract. None (default) = strict: "
            "only 4000-d or NV-Embed's native 4096-d are accepted. Set to e.g. "
            "2048 to run nemotron-3-embed-1b, whose vectors are then zero-padded "
            "to halfvec(4000) (padding zeros are cosine-neutral). "
            "This must be declared explicitly rather than accepting any short "
            "vector: if the endpoint silently starts serving a different model, "
            "unconditional padding would push semantically meaningless vectors "
            "into the index with no error, and the collection embedding "
            "fingerprint cannot detect that (it guards the *declared* model "
            "identity, not endpoint drift)."
        ),
    )
    @field_validator("embedding_source_dim", mode="before")
    @classmethod
    def _blank_source_dim_means_unset(cls, value):
        """把空字串正規化成 None(與 CSP 的 ANILA_EMBED_SOURCE_DIM 同理)。

        ⚠ 必要而非防禦性:compose 的 `${ANILA_EMBED_SOURCE_DIM_DEV:-}` 在未設定時
        展開成**空字串且仍傳入該 key**,Pydantic 對 `int | None` 收到 `""` 會丟
        ValidationError → worker 起不來。而這是「沒有部署較小維度模型」的**預設**
        情況。由 PR #52 的 Codex review 抓到。
        """
        if isinstance(value, str) and value.strip() == "":
            return None
        return value

    embedding_timeout_seconds: float = Field(
        default=30.0,
        description="Per-request embedding timeout.",
    )
    parse_timeout_seconds: float = Field(default=120.0, gt=0, le=1800)
    index_timeout_seconds: float = Field(default=120.0, gt=0, le=1800)
    job_timeout_seconds: int = Field(default=900, ge=60, le=7200)

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
    job_lease_seconds: int = Field(default=90, ge=15, le=3600)
    job_heartbeat_seconds: int = Field(default=20, ge=5, le=300)
    job_retry_backoff_seconds: int = Field(default=15, ge=1, le=3600)
    job_reaper_interval_seconds: int = Field(default=15, ge=5, le=300)
    job_reaper_batch_size: int = Field(default=25, ge=1, le=500)
    job_lease_grace_seconds: int = Field(default=15, ge=0, le=300)
    metrics_port: int = Field(default=8081, ge=1, le=65535)
    health_probe_timeout_seconds: float = Field(default=2.0, gt=0, le=10)

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

    # ── LLM-based relation extraction (document-relations Phase 2 / B) ──
    #
    # The regex citation_extractor (Phase 1 / A) only catches explicitly
    # written citations ("依○○法第X條"). The LLM extractor adds recall for
    # implicit / paraphrased relations and naming variants. It reads the
    # parsed text + the collection's document list and returns edges that
    # point DIRECTLY at a dst_document_id (the LLM picks from the list), so
    # no fragile name-matching is needed for these edges.
    #
    # Same shape as the VLM path: both flags must be on (enable + url). Edges
    # land in document_relations with source='llm' + a confidence, coexisting
    # with rule/manual rows (UNIQUE includes source). Endpoint conventions
    # match embeddings/vision: ``relation_llm_url`` is the OpenAI-compatible
    # base URL (no /chat/completions suffix), CSP /v1 proxy by default so
    # token usage is metered via the ingestion-worker system key.
    enable_relation_llm: bool = Field(
        default=True,
        description=(
            "Master switch for LLM relation extraction. False skips it "
            "entirely (regex/manual edges still work)."
        ),
    )
    relation_llm_url: str = Field(
        default="",
        description=(
            "OpenAI-compatible chat endpoint base URL (no /chat/completions "
            "suffix). Empty disables LLM extraction even when "
            "enable_relation_llm=True — the safe default."
        ),
    )
    relation_llm_model: str = Field(
        default="gemma4",
        description="Chat model identifier passed in the completions body.",
    )
    relation_llm_api_key: str = Field(
        default="not-set",
        description="Bearer token; reuses the internal platform API key.",
    )
    relation_llm_verify_ssl: bool = Field(
        default=False,
        description="Verify TLS (dev CSP nginx uses a self-signed cert).",
    )
    relation_llm_timeout_seconds: float = Field(
        default=120.0,
        description="Per-document relation-extraction LLM timeout.",
    )
    relation_llm_max_chars: int = Field(
        default=12000,
        description=(
            "Max source-text chars sent to the LLM (head of the document). "
            "Bounds prompt cost; citations cluster near the top of ROC regs."
        ),
    )
    relation_llm_max_candidates: int = Field(
        default=200,
        description=(
            "Max sibling documents listed for the LLM to pick a dst from. "
            "Beyond this the prompt is skipped (logged) — large collections "
            "need a retrieval pre-filter, a later phase."
        ),
    )

    # ── Embedding-based topic-similarity edges (document-relations / C) ──
    #
    # A different KIND of edge from citations: pure vector similarity between
    # document-level centroids (avg of leaf-chunk embeddings). Surfaces docs
    # that are topically related even with NO explicit citation. Written as
    # source='similarity', relation_type='relates', confidence=cosine.
    #
    # Same-domain corpora cluster TIGHTLY (legal/ops docs all score 0.95+),
    # so an absolute threshold would fully connect the graph. We instead take
    # each document's top-K nearest neighbours (sparsity comes from K, the
    # floor only screens out genuinely off-topic docs). Recomputed collection-
    # wide on each ingest / reresolve (a new doc shifts everyone's neighbours).
    enable_similarity_edges: bool = Field(
        default=True,
        description="Master switch for embedding topic-similarity edges.",
    )
    similarity_top_k: int = Field(
        default=3,
        ge=1,
        le=20,
        description="Each document links to its K nearest sibling documents.",
    )
    similarity_min: float = Field(
        default=0.75,
        ge=0.0,
        le=1.0,
        description=(
            "Cosine floor — screens out off-topic docs only; the real sparsity "
            "comes from top_k (same-domain corpora all score very high)."
        ),
    )
    similarity_max_docs: int = Field(
        default=500,
        description=(
            "Skip the O(N^2) recompute when a collection exceeds this many "
            "documents (logged) — a later phase adds an ANN pre-filter."
        ),
    )
    similarity_debounce_seconds: float = Field(default=10.0, ge=0.1, le=3600)
    similarity_job_poll_seconds: float = Field(default=1.0, ge=0.1, le=60)
    similarity_job_lease_seconds: int = Field(default=300, ge=30, le=7200)
    similarity_job_heartbeat_seconds: int = Field(default=30, ge=5, le=600)
    similarity_job_retry_backoff_seconds: int = Field(default=30, ge=1, le=3600)

    @model_validator(mode="after")
    def _validate_job_deadlines(self) -> "WorkerSettings":
        if self.job_heartbeat_seconds >= self.job_lease_seconds:
            raise ValueError("job heartbeat must be shorter than the job lease")
        if self.parse_timeout_seconds >= self.job_timeout_seconds:
            raise ValueError("parse timeout must be shorter than the Arq job timeout")
        if self.index_timeout_seconds >= self.job_timeout_seconds:
            raise ValueError("index timeout must be shorter than the Arq job timeout")
        if self.anila_deployment_profile.strip().lower() in {
            "prod-intranet-card",
            "prod-intranet-card-breakglass",
            "prod-public-passwd",
            "prod-military-passwd",
            "trial-military",
        } and (
            len(self.ingestion_queue_hmac_key.strip()) < 32
            or self.ingestion_queue_hmac_key.startswith("dev-")
        ):
            raise ValueError("formal profile requires a non-development ingestion queue HMAC key")
        return self

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )


settings = WorkerSettings()
