from pydantic_settings import BaseSettings
from pathlib import Path


class Settings(BaseSettings):
    # Application
    APP_NAME: str = "CSP Platform"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = False
    # Swagger UI (/docs) + OpenAPI schema (/openapi.json) exposure. These have
    # no auth and leak the full API surface, so they are OFF by default
    # (secure-by-default); dev environments opt in via ENABLE_API_DOCS=true.
    ENABLE_API_DOCS: bool = False

    # Public read-only share endpoint (/api/public/share/{token}) is
    # unauthenticated by design. Air-gapped / card-only deployments that want
    # zero unauthenticated surface set this False to disable it entirely.
    ENABLE_PUBLIC_SHARE: bool = True

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
    # When True the JWT module will auto-generate a keypair at the
    # configured paths if missing. Dev / test only — production must
    # provision keys out-of-band so ``kid`` rotation is explicit.
    ALLOW_AUTO_KEYGEN: bool = False

    # Admin Account
    ADMIN_USERNAME: str = "admin"
    ADMIN_PASSWORD: str = "changeme"

    # Proxy Timeouts (seconds)
    EMBEDDING_TIMEOUT: int = 30
    LLM_TIMEOUT: int = 120

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

    # Usage Writer
    USAGE_BATCH_SIZE: int = 100
    USAGE_FLUSH_INTERVAL: int = 5

    # Service-to-service token sent to downstream agents so they can verify
    # requests originate from CSP. Set to a long random string in production.
    CSP_SERVICE_TOKEN: str = ""

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

    # Auto-register platform links on startup (JSON string)
    # Format: '[{"name":"n8n","url":"http://n8n:5678","icon":"workflow","description":"自動化工作流程"}]'
    AUTO_REGISTER_LINKS: str = ""

    # 中科院憑證卡登入 (branch: SSO)
    # 內網 production:唯一登入方式 = 憑證卡 (中華電信 HiPKI 本機元件 + 中科院
    # PKI 卡)。Trust chain 由使用者 PC + HiPKI driver + 卡片硬體建立,backend
    # 收到 PKCS#7 即視為「持卡人 + PIN 驗過」,parse 抽 employee_id 即可。
    # Dev:用 ``cht/`` mock 容器假裝 localhost:16888。
    #
    # ENABLE_CARD_LOGIN: 是否註冊 /api/auth/card/* endpoints。預設 False;prod
    #   必須 set true (見 docker-compose.yml 預設)。
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
    #   完成註冊 (填單位) → admin 核准 流程。範例:``"1147259,1090868"``。
    ENABLE_CARD_LOGIN: bool = False
    REQUIRE_CARD_LOGIN_ONLY: bool = False
    CARD_INITIAL_OWNERS: str = ""

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
