from __future__ import annotations

import logging

from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    # Database
    DATABASE_URL: str = "postgresql://csp:csp_password@localhost:5432/csp"

    # JWT
    # SECRET_KEY 在 RS256 cutover 後不再用於 access/refresh JWT 簽發,
    # 但保留供 startup_security guard 與 credential_crypto 等模組使用。
    SECRET_KEY: str = "your-secret-key-change-this-in-production"

    # 金鑰圈是空的時候，第一次啟動把 secrets/jwt-{private,public}.pem
    # 以這個 kid 匯入成 active。之後簽名不再讀 PEM；檔案可以留作備份。
    JWT_KID: str = "anila-v1"

    # Admin Account
    ADMIN_PASSWORD: str = "changeme"

    # Proxy Timeouts (seconds)
    EMBEDDING_TIMEOUT: int = 30
    LLM_TIMEOUT: int = 300

    # 出向模型 gateway 的 API key (選配,預設空 = 不注入,行為不變)。
    # 內網拓撲下模型不直連 — 走 10.53.100.12 My-OpenAI-Frontend 的
    # https /v1 gateway,該 gateway 的 /v1 全路由要 Authorization: Bearer。
    # 只注入 model 呼叫 (llm/vlm/embedding);agent dispatch 不帶,
    # 避免 key 外流給第三方 agent。
    MODEL_GATEWAY_API_KEY: str = ""

    # P3.2 — SMTP delivery (OWNER Q3: relay not available yet).
    # Leave ENABLED=false until IT provides the Outlook/relay details.
    # ANILA_ALERT_SMTP_TO should be a **group mailbox**, not a personal one
    # (same reason as PLAN 5.4 support address).
    # 目前只寫 log，未寄信：這組欄位無人讀取；開告警走
    # UnwiredSmtpNotifier 打 WARNING，不會真的連 SMTP。
    ANILA_ALERT_SMTP_ENABLED: bool = False
    ANILA_ALERT_SMTP_HOST: str = ""
    ANILA_ALERT_SMTP_PORT: int = 587
    ANILA_ALERT_SMTP_USER: str = ""
    ANILA_ALERT_SMTP_PASSWORD: str = ""
    ANILA_ALERT_SMTP_FROM: str = ""
    ANILA_ALERT_SMTP_TO: str = ""
    ANILA_ALERT_SMTP_USE_TLS: bool = True

    # 舊的艦隊共用祕密。自動核發開啟時，這把值不再是任何服務身分。
    # CSP 不再把它加進打給 agent 的 header。留著是為了認得資料庫裡
    # 尚未換掉的舊雜湊，以及自動核發關掉時的測試後援。
    CSP_SERVICE_TOKEN: str = ""

    # 內部憑證。CSP 核發並把明文寫進
    # ANILA_SERVICE_CLIENT_DIR/<client_name>/token。子目錄擁有者是
    # uid 10005。ANILA_SERVICE_CLIENT_FILE_GID 是上層目錄的群組
    # （router，10002）。
    # 空的 ANILA_INTERNAL_SERVICE_CLIENTS 用內建名單：router-primary、
    # anila-studio、ingestion-worker（後者是 sk- API key）。
    ANILA_SERVICE_CLIENT_DIR: str = "/run/anila/service-clients"
    ANILA_SERVICE_CLIENT_ROTATE_INTERVAL_SECONDS: int = 30 * 24 * 3600
    ANILA_SERVICE_CLIENT_GRACE_SECONDS: int = 24 * 3600
    ANILA_SERVICE_CLIENT_PROVISION_INTERVAL_SECONDS: int = 3600
    ANILA_SERVICE_CLIENT_FILE_GID: int = 10002
    ANILA_INTERNAL_SERVICE_CLIENTS: str = ""

    # Site URL (for external access, used by platform links)
    SITE_URL: str = "http://localhost"

    # CORS allowlist. Comma-separated origins the browser is allowed to
    # send credentialed requests from. Required when serving the SPA from
    # a different origin than the API (e.g. Vite dev server on :5173).
    # Wildcard "*" is not allowed together with credentials, so this must
    # be an explicit list in any deployment that uses the cookie flow.
    ALLOWED_ORIGINS: str = "http://localhost:5173,http://localhost:3001,http://localhost:80,http://localhost,https://localhost,https://localhost:4443"

    # Incoming Host-header allow-list (anti Host-header-injection /
    # cache-poisoning). Comma-separated hostnames; "*" disables the check.
    # Distinct from ANILA_TRUSTED_HOSTS, which is the *outgoing* SSRF
    # allow-list.
    #
    # Trade-off, deliberate: the *library* default stays "*" (check off)
    # and the *deployment* turns it on — infra/compose/platform.yml passes
    # ALLOWED_HOSTS with the real ingress set as its compose-level default,
    # so every `up -d` is protected even with an empty .env. The reverse
    # (a restrictive default here) locks out callers this file cannot
    # enumerate: starlette's TestClient alone speaks `Host: testserver`
    # (tests/conftest.py:128 drives the whole suite through it), and a bare
    # uvicorn dev loop is reached under whatever name the operator typed.
    # Owner rule ③ "will it block US in the future" — a default that only
    # a container knows how to satisfy would.
    #
    # Whatever this is set to, app.main._INTERNAL_HOSTS is unioned in, so
    # narrowing it can never cut the healthcheck or the in-network callers.
    ALLOWED_HOSTS: str = "*"

    # Auto-register models on startup (JSON string)
    # Format: '[{"name":"llama3-70b","display_name":"Llama 3 70B","model_type":"llm","endpoint_url":"http://vllm:8000","api_version":"v1"}]'
    AUTO_REGISTER_MODELS: str = ""

    # Auto-register agents on startup (JSON string)
    # Format: '[{"name":"rag-agent","endpoint_url":"http://rag-agent:24786","description_for_router":"RAG agent"}]'
    AUTO_REGISTER_AGENTS: str = ""

    # Auto-seed API keys/users on startup (JSON string)
    # Format: '[{"username":"smoke-user","key":"sk-...","models":["gpt-4o-mini"],"agents":["rag-agent"]}]'
    AUTO_SEED_API_KEYS: str = ""

    # P1.1 — 部門樹最大層數。SYSTEM-MAP 定「院 → 所 → 組」三層,但院內實際
    # 編制若有第四層(例如處下設科),改這個值即可,不必動程式碼。
    # ⚠ 只影響新建與 re-parent 的檢查;調低不會回溯處理既有超深節點。
    ANILA_DEPARTMENT_MAX_DEPTH: int = 3

    # OW-1 — max sibling variants under the same parent_id (edit-re-ask /
    # regenerate forks). Exceed → 409. docs/plans/ow1-message-tree-blueprint.md
    # OW-3 — 訊息級自訂動作（宣告式 prompt 模板；無執行面）。
    # 每使用者每分鐘 invoke 上限（進程內固定視窗，非叢集）。
    ANILA_ACTION_INVOKE_PER_MIN: int = 20

    # P1.5 — attachment context budget
    # model_registry.context_window 目前種子皆 NULL，以此為後備。
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

    # 中科院憑證卡登入 (branch: SSO)
    # 內網 production:唯一登入方式 = 憑證卡 (中華電信 HiPKI 本機元件 + 中科院
    # PKI 卡)。Trust chain 由使用者 PC + HiPKI driver + 卡片硬體建立,backend
    # 收到 PKCS#7 即視為「持卡人 + PIN 驗過」,parse 抽 employee_id 即可。
    # Dev:用 ``cht/`` mock 容器假裝 localhost:16888。
    #
    # ANILA_AUTH_MODE: password / mixed / card-only。這一個部署事實同時
    # 決定是否註冊 card endpoints，以及 card 是否為唯一登入路徑。
    #   - POST /api/auth/login (本機帳密) → 404
    #   - POST /api/auth/register (自助註冊) → 404
    #   - GET  /api/auth/oidc/{id}/{start,callback} → 404
    #   - /api/auth/providers 不再列出 OIDC providers
    # CARD_INITIAL_OWNERS: CSV 員工編號清單。列在裡面的第一次刷卡建為
    #   ``role="owner"`` + ``is_approved=True``,**直接登入** (bootstrap)。
    #   其他員工建為 ``role="user"`` + ``is_approved=False``,走 pending →
    #   完成註冊 (填單位) → admin 核准 流程。範例:``"9000001,9000002"``。
    #   ⚠ 範例一律用假編號 —— 這是 PUBLIC repo,真人的員工編號不進註解。
    ANILA_AUTH_MODE: str = "password"
    CARD_INITIAL_OWNERS: str = ""

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
