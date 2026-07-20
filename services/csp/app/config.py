from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    INGESTION_JOB_MAX_ATTEMPTS: int = Field(default=3, ge=1, le=10)
    INGESTION_QUEUE_HMAC_KEY: str = "dev-ingestion-queue-hmac-key-change-me"
    INGESTION_OUTBOX_STALE_SECONDS: int = Field(default=120, ge=30, le=3600)
    # Application
    APP_NAME: str = "CSP Platform"
    APP_VERSION: str = "1.0.0"
    # Machine-readable deployment identity. Formal compose profiles must set
    # this explicitly; startup_security verifies the resolved flags against
    # the named contract before migrations or background work begin.
    ANILA_DEPLOYMENT_PROFILE: str = "development"
    DEBUG: bool = False
    # Swagger UI (/docs) + OpenAPI schema (/openapi.json) exposure. These have
    # no auth and leak the full API surface, so they are OFF by default
    # (secure-by-default); dev environments opt in via ENABLE_API_DOCS=true.
    ENABLE_API_DOCS: bool = False

    # Public read-only share endpoint (/api/public/share/{token}) is
    # unauthenticated by design. Air-gapped / card-only deployments that want
    # zero unauthenticated surface set this False to disable it entirely.
    ENABLE_PUBLIC_SHARE: bool = True
    # Share links may never be permanent. When a caller omits ``expires_at``
    # the service assigns this TTL; callers may request a shorter lifetime but
    # never a longer one. Keep the upper validation bound finite so a malformed
    # deployment profile cannot silently restore permanent public links.
    PUBLIC_SHARE_MAX_TTL_HOURS: int = Field(default=168, ge=1, le=8760)

    # Per-user long-term memory injects recalled content into the system prompt
    # and asynchronously stores every completed turn. Until classification and
    # provenance are enforced end-to-end, both paths are secure-by-default OFF.
    # Development/test profiles may explicitly opt in with ENABLE_MEMORY=true.
    ENABLE_MEMORY: bool = False
    # Gate 2 pilot posture. Turning pilot mode on is meaningful only after
    # the signed-profile verifier succeeds; unconverged inference surfaces
    # remain independently closed at their runtime boundaries.
    ANILA_PILOT_MODE: bool = False
    ENABLE_PILOT_PROMPT_GENERATOR: bool = False
    ENABLE_PILOT_INGESTION_JUDGE: bool = False
    ENABLE_PILOT_STUDIO_ARTIFACTS: bool = False
    PILOT_FIRST_PARTY_AGENT_ALLOWLIST: str = ""
    GATE2_PILOT_PROFILE_PATH: str = "secrets/gate2-pilot-profile.json"
    GATE2_PILOT_TRUST_STORE_PATH: str = "secrets/gate2-pilot-trust.json"
    GATE2_INFERENCE_INVENTORY_PATH: str = "policy/inference-callsites.v1.json"
    # Exact Docker content ID of the executing CSP image. The signed Gate 2
    # profile must bind this value, preventing approval replay on other code.
    GATE2_CSP_IMAGE_ID: str = ""
    # Compose-level pilot posture marker. Only the reviewed Gate 2 overlay
    # injects this value; setting ANILA_PILOT_MODE in the host dotenv alone
    # must never be sufficient to claim the signed pilot posture.
    GATE2_PILOT_COMPOSE_POSTURE: str = ""

    # Gate 5 model-governance runtime.  The feature is opt-in so existing
    # development/test profiles remain usable; once enabled, readiness is
    # fail-closed until every explicit signed material/facts path verifies.
    GATE5_MODEL_GOVERNANCE_ENABLED: bool = False
    GATE5_MODEL_GOVERNANCE_STARTUP_REQUIRED: bool = False
    GATE5_MODEL_GOVERNANCE_INVENTORY_PATH: str = ""
    GATE5_MODEL_GOVERNANCE_PROFILE_PATH: str = ""
    GATE5_MODEL_GOVERNANCE_TRUST_STORE_PATH: str = ""
    GATE5_MODEL_GOVERNANCE_OBSERVED_FACTS_PATH: str = ""
    GATE5_MODEL_GOVERNANCE_OBSERVED_DEPLOYMENT_FACTS_PATH: str = ""
    GATE5_MODEL_GATEWAY_ENDPOINT: str = ""

    # Unit/dev harness escape hatch only. Formal deployments must run Alembic
    # and the legacy idempotent migration pass before becoming ready. Startup
    # security rejects this flag unless the explicit dev posture is enabled.
    SKIP_STARTUP_MIGRATIONS: bool = False

    # Database
    DATABASE_URL: str = "postgresql://csp:csp_password@localhost:5432/csp"

    # JWT
    # SECRET_KEY 在 RS256 cutover 後不再用於 access/refresh JWT 簽發,
    # 但保留供 startup_security guard 與 credential_crypto 等模組使用。
    SECRET_KEY: str = "your-secret-key-change-this-in-production"
    ALGORITHM: str = "HS256"  # Legacy; access/refresh tokens use RS256 now.
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30

    # RS256 asymmetric signing material. Private key is PKCS#8 PEM,
    # public key is SPKI PEM. JWKS endpoint serves the public key under
    # ``kid = JWT_KID`` so anila-studio (and any future verifier) can
    # validate CSP-signed JWTs without sharing a symmetric secret.
    #
    # Paths are resolved relative to the backend working directory
    # (where uvicorn / pytest is launched). For docker the volume mount
    # places ``/app/secrets/`` so the defaults Just Work.
    JWT_PRIVATE_KEY_PATH: str = "secrets/jwt-private.pem"
    JWT_PUBLIC_KEY_PATH: str = "secrets/jwt-public.pem"
    JWT_KID: str = "anila-v1"
    # Stable trust-domain identifiers shared by every JWT consumer.  Tokens
    # without these exact values are rejected even when the RS256 signature is
    # otherwise valid.
    JWT_ISSUER: str = "https://anila.internal/csp"
    JWT_AUDIENCE: str = "anila-platform"
    # Future-iat tolerance only; exp verification remains strict.
    JWT_LEEWAY_SECONDS: int = Field(default=60, ge=0, le=300)
    # When True the JWT module will auto-generate a keypair at the
    # configured paths if missing. Dev / test only — production must
    # provision keys out-of-band so ``kid`` rotation is explicit.
    ALLOW_AUTO_KEYGEN: bool = False

    # Admin Account
    ADMIN_USERNAME: str = "admin"
    ADMIN_PASSWORD: str = "changeme"

    # Proxy Timeouts (seconds)
    EMBEDDING_TIMEOUT: int = 30
    # Explicit deployed-weight identity.  Collection creation binds to this
    # value; a model name is never accepted as a weight fingerprint.
    EMBEDDING_MODEL_FINGERPRINT: str = ""
    LLM_TIMEOUT: int = 120
    # Hard ceilings for outbound SSE.  For Task-bound calls the seconds budget
    # starts at TaskRun.started_at, so hidden retrieval/memory work and the
    # downstream stream share one deadline.  Legacy streams start the same
    # budget when proxying begins.  The httpx read timeout only limits an idle
    # socket; a peer that keeps sending heartbeats could otherwise hold a
    # request and its resources forever.
    PROXY_STREAM_MAX_SECONDS: float = Field(
        default=300.0,
        allow_inf_nan=False,
    )
    PROXY_STREAM_MAX_EVENTS: int = 10000
    PROXY_STREAM_MAX_BYTES: int = 16 * 1024 * 1024

    # 出向模型 gateway 的 API key (選配,預設空 = 不注入,行為不變)。
    # 內網拓撲下模型不直連 — 走 10.53.100.12 My-OpenAI-Frontend 的
    # https /v1 gateway,該 gateway 的 /v1 全路由要 Authorization: Bearer。
    # 只注入 model 呼叫 (llm/vlm/embedding);agent dispatch 不帶,
    # 避免 key 外流給第三方 agent。
    MODEL_GATEWAY_API_KEY: str = ""

    # Proxy Retry
    PROXY_MAX_RETRIES: int = 3
    PROXY_RETRY_BASE_DELAY: float = 0.5

    # Health Check
    HEALTH_CHECK_INTERVAL: int = 60
    # Readiness freshness is a governance TTL, not a UI polling interval.  A
    # target with no timestamp (or a timestamp older than these bounds) is
    # never dispatchable.  Keep the values explicit so production profiles
    # can review them instead of inheriting a wall-clock constant in code.
    AGENT_HEALTH_FRESHNESS_SECONDS: int = Field(default=300, ge=1, le=86400)
    MODEL_HEALTH_FRESHNESS_SECONDS: int = Field(default=300, ge=1, le=86400)
    AGENT_TRACE_TEST_FRESHNESS_SECONDS: int = Field(default=86400, ge=1, le=604800)
    AGENT_REGISTRY_SNAPSHOT_TTL_SECONDS: int = Field(default=60, ge=1, le=3600)
    # Legacy /v1 agent callers do not carry a registry snapshot or manifest
    # revision.  Formal dispatch must keep this disabled; development can opt
    # in explicitly while the Router v2 consumer is being rolled out.
    ALLOW_LEGACY_AGENT_DISPATCH: bool = False
    TASK_RUN_STALE_SECONDS: int = Field(default=900, ge=60, le=86400)

    # Usage Writer
    USAGE_BATCH_SIZE: int = 100
    USAGE_FLUSH_INTERVAL: int = 5

    # Service-to-service token sent to downstream agents so they can verify
    # requests originate from CSP. Set to a long random string in production.
    CSP_SERVICE_TOKEN: str = ""
    # Dedicated named Service Client credential for anila-studio's artifact
    # control-plane writer. It must not reuse the legacy fleet token above.
    STUDIO_ARTIFACT_SERVICE_TOKEN: str = ""
    STUDIO_RUNTIME_SERVICE_TOKEN: str = ""
    # CSP-owned immutable ArtifactVersion bytes. Formal deployments mount
    # this outside the repository and back it up together with the DB.
    ARTIFACT_BLOB_STORAGE_PATH: str = "/var/lib/anila/artifact-blobs"
    INGESTION_UPLOAD_DIR: str = "/var/anila/ingestion-uploads"

    # Gate 3 A5: executable retention posture. Formal profiles must keep the
    # reaper enabled; lifecycle timestamps are persisted in DB and this worker
    # is the only component authorised to erase CSP/ingestion bytes.
    RETENTION_ENABLED: bool = True
    RETENTION_ARTIFACT_ACTIVE_DAYS: int = Field(default=30, ge=1, le=3650)
    RETENTION_ARTIFACT_ARCHIVE_DAYS: int = Field(default=365, ge=1, le=3650)
    RETENTION_INGESTION_ACTIVE_DAYS: int = Field(default=365, ge=1, le=3650)
    RETENTION_INGESTION_ARCHIVE_DAYS: int = Field(default=365, ge=1, le=3650)
    RETENTION_REAPER_INTERVAL_SECONDS: int = Field(default=300, ge=10, le=86400)
    RETENTION_REAPER_LEASE_SECONDS: int = Field(default=900, ge=30, le=86400)
    RETENTION_REAPER_BATCH_SIZE: int = Field(default=50, ge=1, le=500)
    RETENTION_ALLOW_ARCHIVED_DOWNLOADS: bool = False

    # Site URL (for external access, used by platform links)
    SITE_URL: str = "http://localhost"

    # CORS allowlist. Comma-separated origins the browser is allowed to
    # send credentialed requests from. Required when serving the SPA from
    # a different origin than the API (e.g. Vite dev server on :5173).
    # Wildcard "*" is not allowed together with credentials, so this must
    # be an explicit list in any deployment that uses the cookie flow.
    ALLOWED_ORIGINS: str = "http://localhost:5173,http://localhost:3001,http://localhost:80,http://localhost,https://localhost,https://localhost:4443"

    # Incoming Host-header allow-list (anti Host-header-injection /
    # cache-poisoning). Comma-separated hostnames; "*" disables the check
    # (default, non-breaking). Production should pin this to the real
    # ingress host(s), e.g. "anila.ncsist.org.tw,172.16.120.35". Distinct
    # from ANILA_TRUSTED_HOSTS, which is the *outgoing* SSRF allow-list.
    ALLOWED_HOSTS: str = "*"

    # Mark session cookies as Secure (HTTPS-only). Defaults to True; set
    # to False in local HTTP dev / test harnesses where cookies must
    # traverse http:// (the TestClient, a bare dev loop without nginx,
    # etc). In production behind nginx TLS termination leave this True.
    COOKIE_SECURE: bool = True

    # Static files
    STATIC_DIR: str = str(Path(__file__).parent / "static")

    # Auto-register models on startup (JSON string)
    # Format: '[{"name":"llama3-70b","display_name":"Llama 3 70B","model_type":"llm","endpoint_url":"http://vllm:8000","api_version":"v1"}]'
    AUTO_REGISTER_MODELS: str = ""

    # Auto-register agents on startup (JSON string)
    # Format: '[{"name":"rag-agent","endpoint_url":"http://rag-agent:24786","description_for_router":"RAG agent"}]'
    AUTO_REGISTER_AGENTS: str = ""

    # Auto-seed API keys/users on startup (JSON string)
    # Format: '[{"username":"smoke-user","key":"sk-...","models":["gpt-4o-mini"],"agents":["rag-agent"]}]'
    AUTO_SEED_API_KEYS: str = ""

    # Attachment storage (local filesystem)
    ATTACHMENT_STORAGE_PATH: str = "data/attachments"

    # Gate 2 G11: sealed retrieval payloads.  Formal Compose mounts this
    # path from the external ANILA state directory; the repository-relative
    # default is only for dev/TestClient.  Files contain the exact bounded
    # source slabs sent to the model and therefore must be treated as
    # classified application data (0700 directory, 0600 files).
    SOURCE_SNAPSHOT_STORAGE_PATH: str = "data/source-snapshots"

    # Auto-register platform links on startup (JSON string)
    # Format: '[{"name":"n8n","url":"http://n8n:5678","icon":"workflow","description":"自動化工作流程"}]'
    AUTO_REGISTER_LINKS: str = ""

    # 中科院憑證卡登入 (branch: SSO)
    # 內網 production:唯一登入方式 = 憑證卡 (中華電信 HiPKI 本機元件 + 中科院
    # PKI 卡)。使用者 PC 負責 PIN/私鑰運算；backend 仍驗 CMS SignerInfo、
    # challenge nonce 與釘選 CSPKI 憑證鏈，通過後才抽 employee_id。
    # Dev:``cht/`` synthetic emulator 在記憶體產生測試 PKI 並簽實際 nonce。
    #
    # ENABLE_CARD_LOGIN: 是否註冊 /api/auth/card/* endpoints。預設 False;prod
    #   必須 set true (見 infra/compose/platform.yml 預設)。
    # REQUIRE_CARD_LOGIN_ONLY: 內網 production 必設 True。一旦啟用:
    #   - POST /api/auth/login (本機帳密) → 404
    #   - POST /api/auth/register (自助註冊) → 404
    #   - GET  /api/auth/oidc/{id}/{start,callback} → 404
    #   - /api/auth/providers 不再列出 OIDC providers
    #   - 啟動時 assert ENABLE_CARD_LOGIN 同時 True,否則拒絕啟動 (避免
    #     「所有登入路都被鎖死」的 bricked 狀態)。
    # CARD_INITIAL_OWNERS: CSV 員工編號清單。列在裡面的第一次刷卡建為
    #   ``role="owner"`` + ``is_approved=True``,**直接登入** (bootstrap)。
    #   其他員工建為 ``role="user"`` + ``is_approved=False``,走 pending →
    #   完成註冊 (填單位) → admin 核准 流程。範例:``"990000002,990000001"``。
    ENABLE_CARD_LOGIN: bool = False
    REQUIRE_CARD_LOGIN_ONLY: bool = False
    CARD_INITIAL_OWNERS: str = ""
    # Offline certificate-revocation/profile enforcement. Formal card-only
    # deployments mount an operator-refreshed PEM CRL bundle read-only and
    # startup refuses a missing/stale-policy configuration.
    CARD_CRL_REQUIRED: bool = False
    CARD_CRL_BUNDLE_PATH: str = ""
    CARD_CRL_MAX_AGE_HOURS: int = Field(default=24, ge=1, le=168)
    CARD_CRL_SOURCE: str = ""
    CARD_REQUIRED_EKU_OID: str = "1.3.6.1.5.5.7.3.2"  # id-kp-clientAuth
    CARD_REQUIRED_CERT_POLICY_OIDS: str = ""

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
