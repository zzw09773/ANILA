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
    # registry, LLM proxy, revocations cold-start sync, and (2026-07-06)
    # flux_image_primary's runtime image-primary fetcher.
    CSP_BASE_URL: str = "http://csp:8000"
    # Service-to-service shared secret (legacy) — eventually agent-credential
    # bearer will replace this. Same env name as csp side. Sent as
    # ``X-CSP-Service-Token`` by job_reporting.py / revocation_cache.py, and
    # (2026-07-06) reused as-is by flux_image_primary.get_image_primary() —
    # no separate service-token env was introduced for the image-primary
    # fetcher.
    CSP_SERVICE_TOKEN: str = ""

    # FLUX 圖像生成 — OpenAI 相容 Images API(POST {base}/v1/images/generations)。
    # base URL 指到伺服器根或含 /v1 皆可(client 會自動補版本段)。
    # 注意:studio_render.get_flux_provider() / get_active_flux_provider()
    # 直接讀 os.environ,這裡的欄位僅作文件用途;另有 FLUX_MODEL /
    # FLUX_API_KEY / FLUX_MAX_CONCURRENT / FLUX_TIMEOUT_SECONDS 只在 env、
    # 不在本檔。這組 env 現在是「csp image-primary 未設定時」的 fallback —
    # get_active_flux_provider() 執行期優先向 csp 拉 admin 標記的主圖像模型
    # (見 app/services/flux_image_primary.py),csp 沒設定才落回這裡。
    FLUX_BACKEND_URL: str = "http://flux2-dev:8000"

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
    # Formal browser sessions use the host-only ``__Host-`` cookie name.
    # False is reserved for explicit HTTP-only unit/local development and
    # selects a distinct ``anila_dev_*`` name, never the legacy cookie.
    COOKIE_SECURE: bool = True
    # Studio verifies browser JWTs locally. Formal intranet routes therefore
    # must repeat CSP's smart-card assurance check instead of assuming every
    # valid RS256 token was issued by the current login policy.
    REQUIRE_CARD_LOGIN_ONLY: bool = False

    # HTTP timeouts for csp_client.
    INTERNAL_TIMEOUT_SECONDS: float = 30.0
    INTERNAL_TIMEOUT_CONNECT: float = 5.0
    # LLM proxy via csp /v1/chat/completions is long-running. Studio deck
    # generation prompts can hit 10K+ input tokens (RAG chunks + spec
    # schema + hierarchy bullet examples + theme rules) and emit 2-5K
    # output tokens. Observed gemma4 wall-clock: 60-240s per call.
    # csp itself defaults LLM_TIMEOUT=120 internally — production stack
    # must override that env to ≥300 alongside this setting.
    INTERNAL_LLM_TIMEOUT_SECONDS: float = 300.0

    # FLUX cache dir — local volume on the anila-studio container.
    FLUX_CACHE_DIR: str = "/var/anila/anila-studio-flux-cache"

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
