from pydantic_settings import BaseSettings
from pathlib import Path


class Settings(BaseSettings):
    # Application
    APP_NAME: str = "CSP Platform"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = False
    # /docs + /openapi.json are always registered and admin-gated in
    # app.main (require_admin). A former ENABLE_API_DOCS flag was never
    # read — removed so operators cannot believe they toggled docs off.

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

    # P3.2 — alert detectors (platform ingress / DB / disk). Gateway + agent
    # streaks are event-driven from the proxy path, not this interval.
    ALERT_CHECK_INTERVAL: int = 60

    # P3.2 — SMTP delivery (OWNER Q3: relay not available yet).
    # Leave ENABLED=false until IT provides the Outlook/relay details.
    # ANILA_ALERT_SMTP_TO should be a **group mailbox**, not a personal one
    # (same reason as PLAN 5.4 support address).
    ANILA_ALERT_SMTP_ENABLED: bool = False
    ANILA_ALERT_SMTP_HOST: str = ""
    ANILA_ALERT_SMTP_PORT: int = 587
    ANILA_ALERT_SMTP_USER: str = ""
    ANILA_ALERT_SMTP_PASSWORD: str = ""
    ANILA_ALERT_SMTP_FROM: str = ""
    ANILA_ALERT_SMTP_TO: str = ""
    ANILA_ALERT_SMTP_USE_TLS: bool = True

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

    # P1.1 — 部門樹最大層數。SYSTEM-MAP 定「院 → 所 → 組」三層,但院內實際
    # 編制若有第四層(例如處下設科),改這個值即可,不必動程式碼。
    # ⚠ 只影響新建與 re-parent 的檢查;調低不會回溯處理既有超深節點。
    ANILA_DEPARTMENT_MAX_DEPTH: int = 3

    # OW-1 — max sibling variants under the same parent_id (edit-re-ask /
    # regenerate forks). Exceed → 409. docs/plans/ow1-message-tree-blueprint.md
    ANILA_MESSAGE_MAX_SIBLINGS: int = 20

    # OW-3 — 訊息級自訂動作（宣告式 prompt 模板；無執行面）。
    # 每使用者每分鐘 invoke 上限（進程內固定視窗，非叢集）。
    ANILA_ACTION_INVOKE_PER_MIN: int = 20
    # action body 最大字元數；超過 → 413。
    ANILA_ACTION_MAX_BODY_CHARS: int = 20000

    # P1.5 — attachment context budget
    # model_registry.context_window 目前種子皆 NULL，以此為後備。
    ANILA_DEFAULT_CONTEXT_WINDOW: int = 128000
    # 對話內附件可佔用的 context window 比例。
    # DELIBERATE: conversation history is NOT subtracted dynamically from
    # the attachment budget. The 0.7 ratio exists precisely so attachments
    # can never occupy more than 70% of the window, leaving the remaining
    # 30% as the allowance for history, the current question and the
    # answer. A budget that shrank as the conversation grew would make
    # the capacity meter a moving target and could retroactively evict an
    # already-admitted document. Conversations that outgrow the remaining
    # 30% are the separate 'conversation too long' problem (out of scope).
    ANILA_ATTACHMENT_BUDGET_RATIO: float = 0.7
    # token 估算為啟發式，乘上安全係數避免低估。
    ANILA_ATTACHMENT_TOKEN_SAFETY: float = 1.15
    # Absolute ceiling (raw token estimate) on the extracted_text we will
    # persist; beyond it status=too_large and no text is stored, so a 50 MB
    # upload cannot write an unbounded row.
    # ⚠ Deliberately absolute, NOT a multiple of the attachment budget.
    # Admission is derived per request against whichever model applies, so a
    # budget-derived ceiling would be decided at extraction time (no model
    # known → default window) and could discard text that a larger-context
    # model would have admitted — unrecoverable except by re-upload. Storage
    # limits are a resource concern and must not depend on model choice.
    # Sized above what any plausible window could admit (0.7 × 1M ≈ 700K).
    ANILA_ATTACHMENT_MAX_STORED_TOKENS: int = 800_000

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
    #   完成註冊 (填單位) → admin 核准 流程。範例:``"1147259,1090868"``。
    ENABLE_CARD_LOGIN: bool = False
    REQUIRE_CARD_LOGIN_ONLY: bool = False
    CARD_INITIAL_OWNERS: str = ""

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
