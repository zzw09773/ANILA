"""Startup-time guard for known dev defaults.

Sprint 5 X security review (M1): the platform shipped with several env
vars whose defaults are publicly committed in ``.env.example`` and
``infra/compose/platform.yml`` (``SECRET_KEY``, ``ADMIN_PASSWORD``,
``CSP_SERVICE_TOKEN``, DB credentials embedded in ``DATABASE_URL``).
``credential_crypto`` already refuses the dev default unless the operator
opts in via ``ANILA_ALLOW_DEV_SECRET=1``; we now extend the same gate to
the rest of the secrets so a typo'd / forgotten override fails loudly at
boot rather than going to production with `admin/changeme`.

Activation: imported and called once from ``main.lifespan`` BEFORE
``auto_seed`` runs. When ``ANILA_ALLOW_DEV_SECRET=1`` (the docker-compose
default for local stacks), every check downgrades to a warning. In every
other environment the function raises ``RuntimeError`` and the API never
starts.
"""
from __future__ import annotations

import logging
import os
from urllib.parse import urlparse

from app.config import settings


logger = logging.getLogger(__name__)


# Known dev placeholders shipped in .env.example / docker-compose. Any
# env-resolved value matching one of these — case-insensitive, stripped —
# fails the startup check unless ANILA_ALLOW_DEV_SECRET=1.
#
# 兩類 placeholder:
#   1. dev defaults (e.g. "dev-secret-key-change-in-prod") — 給 dev 一鍵跑用
#   2. prod template placeholders (e.g. "<openssl rand -hex 32>") — .env.example
#      的「請你 replace 我」標記。Ops 忘了 replace 時 startup 直接 fail,而不是
#      用字面字串當 SECRET_KEY 跑起來。
_PROD_PLACEHOLDERS = frozenset({
    "<openssl rand -hex 32>",
    "<openssl rand -base64 24>",
    "<openssl rand -base64 32>",
    "<sk-internal-<openssl rand -hex 24>>",
    "<your-employee-id,or-csv-list>",
    "<your-intranet-fqdn-or-ip>",
})

_KNOWN_DEFAULTS: dict[str, frozenset[str]] = {
    "SECRET_KEY": frozenset({
        "your-secret-key-change-this-in-production",
        "dev-secret-key-change-in-prod",
        "change-me",
        "change_me",
        "secret",
    }) | _PROD_PLACEHOLDERS,
    "ADMIN_PASSWORD": frozenset({"changeme", "password", "admin"}) | _PROD_PLACEHOLDERS,
    "CSP_SERVICE_TOKEN": frozenset({"dev-service-token", "changeme"}) | _PROD_PLACEHOLDERS,
    "DB_PASSWORD": frozenset({"csp_password", "csp", "postgres", "password"}) | _PROD_PLACEHOLDERS,
    "INTERNAL_PLATFORM_API_KEY": frozenset({
        "sk-internal-worker-changeme",
        "sk-changeme",
    }) | _PROD_PLACEHOLDERS,
    "CODESERVER_PASSWORD": frozenset({"changeme-codeserver", "changeme"}) | _PROD_PLACEHOLDERS,
    # ANILA_HOST 沒有真正的 dev default (compose 端用 ${ANILA_HOST:?} 強制設值),
    # 只需擋 .env.example 的「請填我」placeholder。空值不算 offender — compose
    # 階段就會 fail-fast,輪不到這裡判。
    "ANILA_HOST": _PROD_PLACEHOLDERS,
    # CARD_INITIAL_OWNERS 同 ANILA_HOST:compose ${CARD_INITIAL_OWNERS:?} 已擋空值,
    # 但 placeholder 字面 (`<your-employee-id,or-csv-list>`) 是非空字串會通過 compose,
    # runtime ``_parse_initial_owners()`` 會解出兩個假員工編號,真實刷卡者不在
    # set 內被當 pending → 沒人能 approve → bricked。這層擋在 startup 比 runtime 早。
    "CARD_INITIAL_OWNERS": _PROD_PLACEHOLDERS,
}


def _is_dev_mode() -> bool:
    return os.environ.get("ANILA_ALLOW_DEV_SECRET", "").strip() == "1"


def _value_for(name: str) -> str | None:
    if name == "SECRET_KEY":
        return settings.SECRET_KEY
    if name == "ADMIN_PASSWORD":
        return settings.ADMIN_PASSWORD
    if name == "CSP_SERVICE_TOKEN":
        return settings.CSP_SERVICE_TOKEN
    if name == "DB_PASSWORD":
        # Pull the password out of DATABASE_URL — that's the only place ops
        # configures it in this stack.
        try:
            parsed = urlparse(settings.DATABASE_URL)
            return parsed.password
        except Exception:
            return None
    return os.environ.get(name)


def assert_no_dev_defaults() -> None:
    """Raise unless every protected secret is overridden, or dev opt-in is set.

    In dev mode (``ANILA_ALLOW_DEV_SECRET=1``) we log warnings instead of
    raising — so docker-compose still boots locally without a per-developer
    .env, but production deployments get a hard failure at startup.

    Two failure modes:

    - ``offenders``：fatal regardless of dev_mode（例如 ``SECRET_KEY`` 為
      空 — 完全沒有加密金鑰，dev 也不該允許）。
    - ``warnings``：與已知 dev 預設值字面相同；dev_mode 下 log warning，
      production 直接 raise。
    """
    dev_mode = _is_dev_mode()
    offenders: list[str] = []
    warnings: list[str] = []

    for name, defaults in _KNOWN_DEFAULTS.items():
        value = _value_for(name)
        if value is None:
            continue
        normalized = value.strip().lower()
        if not normalized:
            # 空 SECRET_KEY 在加密路徑上等於沒設，永遠 fatal。
            if name == "SECRET_KEY":
                offenders.append(f"{name} 為空")
            continue
        if normalized in defaults:
            warnings.append(name)

    if offenders:
        raise RuntimeError(
            "Refusing to start: " + "; ".join(offenders)
        )

    if not warnings:
        return

    summary = ", ".join(sorted(warnings))
    if dev_mode:
        logger.warning(
            "[startup_security] 偵測到使用 dev 預設值: %s — "
            "ANILA_ALLOW_DEV_SECRET=1 已開啟，僅警告。production 必須關閉此 flag。",
            summary,
        )
        return

    raise RuntimeError(
        "Refusing to start: 下列環境變數仍為 dev 預設值，"
        f"請於 production 環境覆寫: {summary}. "
        "若僅做本機開發可暫時設 ANILA_ALLOW_DEV_SECRET=1。"
    )


def assert_audit_ledger_locked_down() -> None:
    """P2.7:稽核表必須不歸 runtime role 所有,且 runtime role 不能改/刪它。

    為什麼要在開機檢查:``0014`` 留下的
    ``ALTER DEFAULT PRIVILEGES ... GRANT ALL ON TABLES TO csp_app``
    還在對**未來新建的表**自動發全權限。哪天有人不小心把稽核表重建、
    或把 ownership 又轉回去,不會有任何錯誤訊息 —— 稽核帳只是安靜地退回
    裸奔,而且要等到六個月後在稽核現場才會發現。這裡跟「secrets 還是 dev
    預設值就不給起」用同一個 fail-closed 模式:當天就炸,操作者成本為零。

    只在 PostgreSQL 上檢查(SQLite 單元測試沒有 role 語意);
    表還不存在時跳過(migration 尚未跑到)。dev 模式只警告。

    **查不出來也算不合格。** 這支檢查本來包在 try/except 裡「查詢失敗就 warning
    然後照樣開機」—— 那是一個看不見就放行的門衛。它守的東西是「稽核帳有沒有
    在保護中」,而「我不知道有沒有在保護」正是最不該放行的狀況。所以 production
    改成連查不出來都拒絕啟動;dev(``ANILA_ALLOW_DEV_SECRET=1``)維持只警告,
    本機沒有 Postgres/權限的情境不會被卡住。
    """
    from sqlalchemy import text

    from app.database import engine
    from app.services.audit_ledger import AUDIT_EVENT_TABLES, AUDIT_LEDGER_TABLES

    if engine.dialect.name != "postgresql":
        return

    offenders: list[str] = []
    try:
        with engine.connect() as conn:
            runtime_role = conn.execute(text("SELECT current_user")).scalar()
            for table in AUDIT_LEDGER_TABLES:
                row = conn.execute(
                    text(
                        "SELECT c.relname, pg_get_userbyid(c.relowner) AS owner "
                        "  FROM pg_class c "
                        "  JOIN pg_namespace n ON n.oid = c.relnamespace "
                        " WHERE n.nspname = 'public' AND c.relname = :t"
                    ),
                    {"t": table},
                ).first()
                if row is None:
                    continue  # migration 還沒跑到這張表
                if row.owner == runtime_role:
                    offenders.append(
                        f"{table} 的 owner 還是 runtime role {runtime_role}"
                        "(owner 隱含全部權限,而且拆得掉 append-only 觸發器)"
                    )
                # audit_checkpoints 連 INSERT 都不該有:有 INSERT 就能塞一列
                # 未來日期的檢查點,把每日封存永久卡死(見 audit_ledger)。
                forbidden = ("UPDATE", "DELETE", "TRUNCATE")
                if table not in AUDIT_EVENT_TABLES:
                    forbidden = forbidden + ("INSERT",)
                for priv in forbidden:
                    has = conn.execute(
                        text(
                            "SELECT has_table_privilege(:role, :t, :priv)"
                        ),
                        {"role": runtime_role, "t": table, "priv": priv},
                    ).scalar()
                    if has:
                        offenders.append(
                            f"{table}:runtime role {runtime_role} 仍有 {priv} 權限"
                        )
    except Exception as exc:
        if _is_dev_mode():
            logger.warning(
                "[startup_security] 稽核帳權限檢查無法執行: %s — "
                "ANILA_ALLOW_DEV_SECRET=1 已開啟,僅警告。", exc,
            )
            return
        raise RuntimeError(
            "Refusing to start: 無法確認稽核帳(P2.7)是否受保護 —— "
            f"權限檢查本身失敗: {exc}. "
            "「查不出來」不等於「沒問題」;請修好資料庫連線或權限後再啟動,"
            "本機開發可暫時設 ANILA_ALLOW_DEV_SECRET=1。"
        ) from exc

    if not offenders:
        return

    summary = "; ".join(offenders)
    if _is_dev_mode():
        logger.warning(
            "[startup_security] ⚠ 稽核帳目前**未受保護**(防竄改姿態不完整): %s — "
            "ANILA_ALLOW_DEV_SECRET=1 已開啟,僅警告。這台機器上的稽核紀錄"
            "現在可以被任意改寫,不要拿它的內容當證據。", summary,
        )
        return

    raise RuntimeError(
        "Refusing to start: 稽核帳(P2.7)防竄改姿態不完整,"
        f"執行帳號對稽核表的權限過大: {summary}. "
        "請確認 alembic 已升到含 r1_0027 的 head,且 DATABASE_URL 用的是 "
        "非 superuser 的 runtime role。"
    )


def assert_intranet_lockdown_consistency() -> None:
    """Branch ``SSO``:``REQUIRE_CARD_LOGIN_ONLY`` 與其他 auth flag 的相容性。

    中科院內網 production 政策是「**卡片登入是唯一活路**」 — 本機帳密、
    OIDC、自助註冊全部禁用。要 enforce 這個政策,必須 ``ENABLE_CARD_LOGIN``
    同時啟用,否則整個系統會處於「沒人能登入」的 bricked 狀態。

    本檢查在 ``lifespan`` 啟動時跑;不通過直接拒絕啟動 — secure by default
    at deployment time,比 runtime check 強。
    """
    if not settings.REQUIRE_CARD_LOGIN_ONLY:
        return

    if not settings.ENABLE_CARD_LOGIN:
        raise RuntimeError(
            "Refusing to start: REQUIRE_CARD_LOGIN_ONLY=True 但 "
            "ENABLE_CARD_LOGIN=False — 將無人能登入。請同時啟用 "
            "ENABLE_CARD_LOGIN=true,或關閉 REQUIRE_CARD_LOGIN_ONLY。"
        )
