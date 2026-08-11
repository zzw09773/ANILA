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


def assert_card_dev_bypass_not_in_a_real_boot() -> None:
    """``CARD_DEV_SKIP_NONCE_BINDING`` 只准活在 dev-card 模式裡。

    這顆旗標開啟 = ``card_auth`` 的 **nonce 綁定(反 replay)整條關掉**;
    簽章與憑證鏈驗證照跑,所以它唯一的後果就是「攔到一次成功的刷卡簽章 →
    無限重放」。它的兄弟 ``CARD_DEV_TRUST_TEST_CA`` 有 compose 明列 ＋
    ``_reject_dev_test_ca_in_production`` 的 fail-closed 檢查;這一顆在
    2026-08-08 的環境盤點之前**沒有任何程式層攔截** —— 任何人在 compose
    overlay 加一行就靜默生效,唯一的防線是
    ``docs/runbooks/intranet-deployment-runbook.md:26`` 那句「內網一律不可設」。
    這支函式把那句話變成開機硬檢查。

    **主判準是「這個行程已經凍結成什麼」,不是「環境現在寫什麼」。** 驗章那一行讀
    的是 ``card_auth`` 在 import 當下凍結的 ``_SKIP_NONCE_BINDING``,所以主判準是
    ``card_dev_skip_nonce_binding_frozen()``;``card_dev_skip_nonce_binding_enabled()``
    (重讀環境)是第二個維度,涵蓋「環境已設、``card_auth`` 還沒被 import」的設定
    意圖。**任一為真就進入判斷**(只讀環境會漏掉哪個視窗,見下方 frozen/live 註解)。

    **兩支都在 ``card_auth``,真值解析不自己寫。** 守衛與消費端只要各寫一份
    「差不多」的解析,就會在邊緣形狀上分岔:守衛較窄(例如只認
    ``== "true"``)→ ``=yes`` 守衛放行、消費端啟用,**旁路照開**;守衛較寬
    (例如自己補了 ``strip()``)→ ``=" true "`` 擋住開機,而消費端其實是關的。

    **「dev-card 模式」不自己定義。** 直接呼叫
    ``card_auth._dev_test_ca_explicitly_allowed()`` —— 那是這棵樹裡唯一一份
    dev 卡登路徑的定義(``CARD_DEV_TRUST_TEST_CA`` 開啟 ∧
    ``ANILA_AUTH_MODE`` 不是 ``card-only``,``platform.yml`` 是同一句話的
    部署面說法)。刻意呼叫這個底線開頭的名字而不是包一層公開別名:多一個名字
    就多一個會漂開的定義,而這裡要的正是「只有一個」。

    ⚠ **``ANILA_ALLOW_DEV_SECRET=1`` 不是這一條的逃生口**(本模組其他檢查是)。
    那顆旗標守的是「祕密還是不是預設值」,dev 機器降級成警告很合理;這一條守的
    是反 replay 綁定有沒有被關掉,而 ``ANILA_ALLOW_DEV_SECRET`` 誤帶進內網是
    **已知會發生**的事(``platform.yml:8`` 特地為它寫了一段)。給第二把鑰匙
    等於讓一個設錯的 dev 旗標把紅線一起帶開。真的需要用舊的固定簽章素材時,
    出口寫在錯誤訊息裡:把那兩個 dev-card 旗標明確設好。
    """
    from app.services.card_auth import (
        _dev_test_ca_explicitly_allowed,
        card_dev_skip_nonce_binding_enabled,
        card_dev_skip_nonce_binding_frozen,
    )

    # frozen = 這個行程**現在就是**什麼姿態(驗章那一行讀的那顆常數);
    # live   = 現在的環境**要求**什麼(下一次 import 會凍結成的樣子)。
    # 兩個都要看。只看 live 會漏掉「以 =1 import、開機前把變數移除」那個視窗
    # ——2026-08-09 紅線雙票實測到的旁路:凍結的旗標仍然是開的,反 replay 已經
    # 關掉,而守衛重讀環境看不到任何東西於是放行。只看 frozen 則會放過
    # 「環境已經設了、但 card_auth 剛好還沒被 import」的設定意圖。
    frozen_active = card_dev_skip_nonce_binding_frozen()
    env_requests = card_dev_skip_nonce_binding_enabled()
    if not (frozen_active or env_requests):
        return

    dev_card_mode, why_not = _dev_test_ca_explicitly_allowed()
    if dev_card_mode:
        logger.warning(
            "[startup_security] CARD_DEV_SKIP_NONCE_BINDING 已開啟 —— "
            "卡登的 nonce 綁定(反 replay)在這台機器上是關的。"
            "dev-card 模式下允許,但這台機器的刷卡結果不可以當成身分證據。"
        )
        return

    trigger = (
        "這個行程已經凍結成「跳過 nonce 綁定」(旗標在 card_auth import 當下是開的——"
        "之後把環境變數移除或改成別的值**不會**把它關回去)"
        if frozen_active
        else "環境要求開啟它"
    )
    raise RuntimeError(
        f"Refusing to start: CARD_DEV_SKIP_NONCE_BINDING —— {trigger},但這不是 "
        f"dev-card 模式({why_not})。這顆旗標會關掉卡登的 nonce 綁定,"
        "也就是反 replay 保護 —— 任何人攔到一次成功的刷卡簽章就能無限重放,"
        "而簽章與憑證鏈驗證全都會通過,log 上看起來是正常登入。"
        "內網正式部署一律不可設(見 docs/runbooks/intranet-deployment-runbook.md)。"
        "若確實要在本機接舊的固定簽章素材,請一併設 CARD_DEV_TRUST_TEST_CA=1 "
        "並讓 ANILA_AUTH_MODE 不是 card-only —— 這是 dev-card 模式的定義。"
    )


def assert_intranet_lockdown_consistency() -> None:
    """Branch ``SSO``：檢查單一 auth mode 的合法值與 card-only 政策。

    中科院內網 production 政策是「**卡片登入是唯一活路**」 — 本機帳密、
    OIDC、自助註冊全部禁用。單一 ``ANILA_AUTH_MODE`` 值避免兩個旗標
    不一致造成「沒人能登入」的 bricked 狀態。

    本檢查在 ``lifespan`` 啟動時跑;不通過直接拒絕啟動 — secure by default
    at deployment time,比 runtime check 強。
    """
    mode = settings.ANILA_AUTH_MODE
    if mode not in {"password", "mixed", "card-only"}:
        raise RuntimeError(
            "Refusing to start: ANILA_AUTH_MODE 必須是 password、mixed 或 card-only"
        )
    if mode != "card-only":
        return
