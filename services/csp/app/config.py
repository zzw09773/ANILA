from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)


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
    #   完成註冊 (填單位) → admin 核准 流程。範例:``"9000001,9000002"``。
    #   ⚠ 範例一律用假編號 —— 這是 PUBLIC repo,真人的員工編號不進註解。
    ENABLE_CARD_LOGIN: bool = False
    REQUIRE_CARD_LOGIN_ONLY: bool = False
    CARD_INITIAL_OWNERS: str = ""

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()


# ── B 類設定的開機覆蓋 ─────────────────────────────────────────────────────
#
# C 類設定改完下一個請求就生效（Task 3 把讀取點搬成 per-request）。B 類做不到
# 那件事：它們的讀取點是背景迴圈的間隔、開機自動註冊的清單、附件落地的目錄
# —— 全都在開機那一刻決定。擁有者裁定的機制是**開機覆蓋**（明確否決了「把值
# 寫回 .env 再重啟」）：csp 開機、DB 可達之後、任何消費端讀到它之前，把
# ``platform_settings`` 裡的 B-可編輯覆蓋值蓋回**上面那個單例**。
#
# 為什麼是就地覆寫那一顆，不是 ``model_copy`` 出一份新的
# ======================================================
# 全樹幾百處寫的是 ``from app.config import settings``，也就是說每一個消費模組
# 在 import 期就把**那個物件的參考**抓在手上了。重建一份新的、把模組屬性換掉，
# 對它們一點效果也沒有：畫面說改好了、後端跑的還是舊值，而且不會有任何錯誤
# 訊息。所以套用一定是就地 ``setattr``；``Settings`` 沒有 ``frozen``，也沒有
# ``validate_assignment``（值在 ``resolve_setting`` 那一端就已經按登錄表的規則
# 解析並過完值域了）。
#
# 為什麼只套 DB 那一層
# ====================
# ``resolve_setting`` 的回退鏈是 DB → ``os.environ`` → 程式預設，而
# ``Settings`` 自己的值可能來自 ``.env`` **檔**（``model_config`` 有
# ``env_file``）—— 那一層 ``os.environ`` 讀不到。把 env／預設層也蓋回去，等於
# 用一個這個行程從來沒有用過的值取代佈署真正在跑的值。只有 ``source == db``
# 的才是「管理員真的存過」，也只有那些才進快照。
#
# ⚠ **凡是 hook 跑之前就被消費掉的顆，都不可以是 B-可編輯。** 那包括 import
# 期就被讀走的欄位（``DEBUG`` 進了 engine 的 ``echo``、``STATIC_DIR`` 進了
# ``/static`` 掛載）與根本不住在 ``Settings`` 上、直接讀 ``os.environ`` 的顆。
# 它們一律在登錄表降級成 B-鎖定 —— 留著就是畫面上的謊。

#: 運維 grep 用的標記。成功與失敗兩邊都會印，所以「一行都沒有」讀得出第三種
#: 狀態（開機沒走到這裡），而不是被誤讀成「沒有覆蓋」。
BOOT_OVERRIDE_LOG_TAG = "boot-override:"


@dataclass(frozen=True)
class BootOverrideSnapshot:
    """這一次開機到底套了什麼 —— 設定頁「來源」欄的唯一依據。

    ``applied``：設定 key → 真的蓋到 ``settings`` 上的值。**不在裡面的 key 就
    不是 db-boot 來的**，包括那些有列但值壞掉、已經退回 env／預設的。

    ``load_failed``：設定表整個讀不動。這時 ``applied`` 必然是空的，而畫面要
    照實說「這一次開機沒有載入覆蓋」——把載入失敗顯示成「沒有人設定過」，
    是這個包最該死的那種靜默成功。

    ``failure_reason``：只放例外的**類別名**。連線錯誤的訊息會帶著 DSN，而
    ``DATABASE_URL`` 內嵌帳密 —— 完整的細節（含 traceback）只進 log，不進
    這個會上管理員畫面的欄位。
    """

    applied: Mapping[str, Any]
    load_failed: bool
    failure_reason: str


_EMPTY_OVERRIDES: Mapping[str, Any] = MappingProxyType({})

#: 模組層唯讀狀態。每一次 ``apply_boot_overrides`` **整份取代**它，不累加：
#: 同一個行程可能跑很多次 lifespan（測試的 ``TestClient`` 就是），累加會讓來源
#: 欄記著上一輪的事。
_current_snapshot = BootOverrideSnapshot(
    applied=_EMPTY_OVERRIDES, load_failed=False, failure_reason=""
)


def boot_override_snapshot() -> BootOverrideSnapshot:
    """這一次開機的套用紀錄。沒有它，設定頁的「來源」欄只能用猜的。"""
    return _current_snapshot


def record_boot_override_failure(reason: str) -> BootOverrideSnapshot:
    """記下「這一次開機根本沒能去讀設定表」。

    ``apply_boot_overrides`` 只擋得住它自己看得到的例外 —— 連 session 都開不起來
    的時候（DB 整個不通），它**沒有被呼叫過**，於是快照會停在初始值：
    ``load_failed=False``、``applied={}``。那正好是「沒有人設定過」的長相，而事
    實是「我們沒有去看」。開機端因此要在自己的保險絲裡呼叫這一支，讓來源欄不會
    把一次失敗說成一次乾淨的開機。
    """
    global _current_snapshot

    _current_snapshot = BootOverrideSnapshot(
        applied=_EMPTY_OVERRIDES, load_failed=True, failure_reason=reason
    )
    return _current_snapshot


def apply_boot_overrides(db) -> BootOverrideSnapshot:
    """把 ``platform_settings`` 裡的 B-可編輯覆蓋值蓋回 ``settings``。

    ``db`` 是一個已經開好的 session（呼叫端負責關）。回傳的就是新的模組層快照。

    **開機絕不因為這張表掛掉。** 讀取階段任何一個例外都會被收成「這一次沒有
    載入覆蓋」：欄位一顆都不動（＝以 env 值開機）、留一則大聲的 ERROR、快照記
    載失敗。讀與寫刻意分兩段，所以失敗時不會留下「套了一半」那種最難查的狀態。
    """
    global _current_snapshot

    from app.models.platform_setting import SOURCE_DB, resolve_setting
    from app.services.settings_registry import SETTINGS, SettingClass

    pending: list[tuple[str, str, Any]] = []  # (設定 key, Settings 欄位, 值)
    try:
        for spec in SETTINGS:
            if spec.setting_class is not SettingClass.B_EDIT:
                continue
            field = spec.env_name
            if field is None or field not in Settings.model_fields:
                # 登錄表把一顆蓋不到的設定標成可編輯 = 畫面上的假控制項。
                # 測試（test_every_b_edit_entry_is_a_field_on_settings）擋在前面，
                # 所以真的走到這裡就是登錄表出事了，要看得見。
                logger.error(
                    "%s 登錄表把 %s 標成 B-可編輯，但 %r 不是 Settings 的欄位 —— "
                    "這一顆改了不會生效",
                    BOOT_OVERRIDE_LOG_TAG,
                    spec.key,
                    field,
                )
                continue
            value, source = resolve_setting(db, spec.key)
            if source == SOURCE_DB:
                pending.append((spec.key, field, value))
    except Exception as exc:
        _current_snapshot = BootOverrideSnapshot(
            applied=_EMPTY_OVERRIDES,
            load_failed=True,
            failure_reason=f"讀取 platform_settings 失敗（{type(exc).__name__}）",
        )
        logger.error(
            "%s 讀取 platform_settings 失敗 —— 本次開機一律沿用環境變數與程式預設值，"
            "管理員存過的 B 類覆蓋一顆都沒有套用",
            BOOT_OVERRIDE_LOG_TAG,
            exc_info=True,
        )
        return _current_snapshot

    for _key, field, value in pending:
        setattr(settings, field, value)

    _current_snapshot = BootOverrideSnapshot(
        applied=MappingProxyType({key: value for key, _field, value in pending}),
        load_failed=False,
        failure_reason="",
    )
    logger.info(
        "%s 套用 %d 顆管理員存過的 B 類設定%s",
        BOOT_OVERRIDE_LOG_TAG,
        len(pending),
        ("：" + "、".join(key for key, _f, _v in pending)) if pending else "",
    )
    return _current_snapshot
