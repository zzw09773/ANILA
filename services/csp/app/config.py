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

    # 出向模型 gateway 的 API key (選配,預設空 = 不注入,行為不變)。
    # 模型不直連，走治理中心登記的 https /v1 gateway。該 gateway 的
    # /v1 要 Authorization: Bearer。只注入 model 呼叫 (llm/embedding);
    # agent dispatch 不帶，避免 key 外流給第三方 agent。
    MODEL_GATEWAY_API_KEY: str = ""

    # 警報寄信在治理中心「警報」頁設定，不讀環境變數。

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

    # CORS。逗號分隔。正式預設空字串＝只允許同源。
    # 本機開發來源寫在 infra/compose/dev.yml，不放在這個預設值。
    # 不允許 "*" 搭配 credentials。
    ALLOWED_ORIGINS: str = ""

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

    # Auto-seed API keys/users on startup (JSON string)
    # Format: '[{"username":"smoke-user","key":"sk-...","models":["gpt-4o-mini"],"agents":["rag-agent"]}]'
    AUTO_SEED_API_KEYS: str = ""

    # OW-1 — max sibling variants under the same parent_id (edit-re-ask /
    # regenerate forks). Exceed → 409. docs/plans/ow1-message-tree-blueprint.md

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
