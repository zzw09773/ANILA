from pydantic_settings import BaseSettings
from pathlib import Path


class Settings(BaseSettings):
    # Application
    APP_NAME: str = "CSP Platform"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = False

    # Database
    DATABASE_URL: str = "postgresql://csp:csp_password@localhost:5432/csp"

    # JWT
    SECRET_KEY: str = "your-secret-key-change-this-in-production"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # Admin Account
    ADMIN_USERNAME: str = "admin"
    ADMIN_PASSWORD: str = "changeme"

    # Proxy Timeouts (seconds)
    EMBEDDING_TIMEOUT: int = 30
    LLM_TIMEOUT: int = 120

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

    # 中科院憑證卡登入（branch: SSO）
    # 院內 production 必須走憑證卡；dev/staging 透過 cht/ mock 容器模擬。
    # ENABLE_CARD_LOGIN: 是否註冊 /api/auth/card/* endpoints。預設 False，
    #   避免 prod 環境誤暴露 LOOSE mode；要啟用必須顯式 set true。
    # CARD_VERIFY_MODE: 'loose' 信任 cht/ mock（dev 用）；'strict' 完整驗
    #   chain + OCSP（prod 用，目前 STRICT impl pending cryptography>=43）。
    # REQUIRE_CARD_LOGIN_ONLY: 中科院內網 production 設 True。一旦啟用：
    #   - POST /api/auth/login (本機帳密) → 404
    #   - POST /api/auth/register (自助註冊) → 404
    #   - GET  /api/auth/oidc/{id}/{start,callback} → 404
    #   - /api/auth/providers 不再列出 OIDC providers
    #   - 啟動時 assert ENABLE_CARD_LOGIN 同時也是 True，否則拒絕啟動
    #     （避免「所有登入路都被鎖死」的 bricked 狀態）。
    ENABLE_CARD_LOGIN: bool = False
    CARD_VERIFY_MODE: str = "loose"
    REQUIRE_CARD_LOGIN_ONLY: bool = False
    # STRICT mode 配套：
    # - NCSIST_CA_BUNDLE_PATH: PEM bundle 路徑（含 CSPKI Root + Intermediate）。
    #   空字串時 fallback 到 backend/data/ncsist-ca-bundle.pem（從 cht/ mock
    #   抽出的 default，dev/staging 用）。Production 應指向 IT 配發版本。
    # - CARD_CHECK_REVOCATION: 是否打 OCSP 撤銷檢查。Dev / 外網部署設 False
    #   （ocsp.ncsist.org.tw 是內網 only domain）；內網 prod 才開 True。
    NCSIST_CA_BUNDLE_PATH: str = ""
    CARD_CHECK_REVOCATION: bool = False
    # 卡片登入 user provisioning policy：
    # - CARD_INITIAL_OWNERS: CSV 員工編號清單；列在裡面的人第一次刷卡會被
    #   建立為 ``role="owner"`` + ``is_approved=True`` 的帳號，**直接登入**。
    #   解決 bootstrap 問題（沒有第一個 owner 沒人能 approve 其他人）。
    #   其他員工第一次刷卡建為 ``role="user"`` + ``is_approved=False``，
    #   走「等待核准」流程：強制完成註冊 (填單位) → admin 介面審查 → 核准
    #   後下次刷卡才能真正登入。
    #   範例：``CARD_INITIAL_OWNERS=1147259,1090868``
    CARD_INITIAL_OWNERS: str = ""

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
