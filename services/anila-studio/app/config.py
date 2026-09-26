"""anila-studio runtime settings.

All values come from environment variables with sensible dev defaults.
Production deployments MUST override CSP_BASE_URL / REDIS_URL.

Wave-1 subagents will extend this file with their own additions; this is the
base shape they import against. Anything added later must keep the
pydantic-settings compatibility.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Service identity
    APP_NAME: str = "anila-studio"
    APP_VERSION: str = "0.1.0"
    LOG_LEVEL: str = "INFO"

    # csp (control plane) HTTP base — used by csp_client for RAG, model
    # roles, LLM proxy, image generation, and revocations cold-start sync.
    CSP_BASE_URL: str = "http://csp:8000"
    # Service-to-service shared secret (legacy). Sent as
    # ``X-CSP-Service-Token`` by job_reporting.py / revocation_cache.py.
    CSP_SERVICE_TOKEN: str = ""

    # pptx-renderer HTTP base (Node.js service)
    RENDERER_BASE_URL: str = "http://pptx-renderer:7100"

    # Redis URL — same instance csp publishes token-revoke events on.
    REDIS_URL: str = "redis://redis:6379/0"
    REDIS_REVOCATION_CHANNEL: str = "anila:auth:token-revoke"

    # JWT verify — only public key needed (csp signs with private).
    JWT_KID: str = "anila-v1"  # default until JWKS fetch overrides
    JWT_ALGORITHMS: tuple[str, ...] = ("RS256",)
    # Sub-second drift tolerance for the iat/exp checks.
    JWT_LEEWAY_SECONDS: int = 60

    # HTTP timeouts for csp_client.
    INTERNAL_TIMEOUT_SECONDS: float = 30.0
    INTERNAL_TIMEOUT_CONNECT: float = 5.0
    # LLM proxy via csp /v1/chat/completions is long-running. Studio deck
    # generation prompts can hit 10K+ input tokens (RAG chunks + spec
    # schema + hierarchy bullet examples + theme rules) and emit 2-5K
    # output tokens. Observed gemma4 wall-clock: 60-240s per call.
    # csp itself defaults LLM_TIMEOUT=300 internally — production stack
    # must override that env to ≥300 alongside this setting.
    INTERNAL_LLM_TIMEOUT_SECONDS: float = 300.0

    # Persistence root for non-PPTX artifacts (report HTML/PDF/DOCX, mindmap
    # SVG, infographic HTML/PNG, datatable CSV/XLSX/HTML). Each job writes
    # {job_id}.{ext} here; download endpoints read back from this dir.
    ARTIFACTS_DIR: str = "/var/anila/anila-studio-artifacts"

    # Behavioural toggles
    # In tests / dev we may want JWKS cache to refresh faster; default 1hr.
    JWKS_REFRESH_SECONDS: int = 3600
    # Revocation cache TTL (matches csp /api/auth/revocations retention).
    REVOCATION_CACHE_TTL_SECONDS: int = 30 * 24 * 3600

    # ── Durable job store (Slice 8b) ─────────────────────────────────────
    # The five artifact pipelines used to hold job state purely in process
    # memory, so a studio restart lost every in-flight job (doc 02 failure
    # model: "Studio restart → job 不應丟失"). We persist job metadata to
    # the SAME Redis instance the revocation cache uses (doc 02 §1 topology
    # lists Redis as "queue + revocation + jobs"). Keys are prefixed and
    # carry a generous TTL so restarts can still answer status queries.
    JOB_STORE_KEY_PREFIX: str = "anila-studio:jobs:"
    JOB_STORE_TTL_SECONDS: int = 7 * 24 * 3600  # 7 days

    # ── CSP artifact-job / artifact reporting (Slice 8b) ─────────────────
    # When enabled, each job create → POST {csp}/v1/artifact-jobs, terminal
    # states → PATCH, and persisted artifacts → POST {csp}/v1/artifacts.
    # All fire-and-forget (retry-once, log-not-raise): CSP being down must
    # never break generation. Toggle off to fully silence the outbound
    # reporting (spans included) in constrained environments.
    STUDIO_ARTIFACT_REPORTING: bool = True

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
