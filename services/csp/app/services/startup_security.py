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


def _is_production_posture() -> bool:
    """Treat an explicit production profile as formal even if dev opt-in leaks.

    When ANILA_ENV is absent, the existing secure convention applies: only an
    explicit ANILA_ALLOW_DEV_SECRET=1 opts into development behavior.
    """
    env_name = os.environ.get("ANILA_ENV", "").strip().lower()
    return (
        env_name in {"prod", "production"}
        or settings.REQUIRE_CARD_LOGIN_ONLY
        or not _is_dev_mode()
    )


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


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
    if dev_mode and _is_production_posture():
        raise RuntimeError(
            "Refusing to start: ANILA_ALLOW_DEV_SECRET=1 僅限明示的 dev/test "
            "profile；ANILA_ENV=production 或 card-only 正式姿態禁止此旁路。"
        )
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


def assert_card_only_data_feature_policy() -> None:
    """Keep unfinished data features out of the formal card-only posture.

    A stale developer ``.env`` must not silently re-enable public sharing or
    long-term memory when the deployment is switched to card-only mode.  These
    capabilities remain available to explicit non-card development profiles;
    they are blocked here only until their later security gates are complete.
    """
    if not settings.REQUIRE_CARD_LOGIN_ONLY:
        return

    enabled = [
        name
        for name, value in (
            ("ENABLE_PUBLIC_SHARE", settings.ENABLE_PUBLIC_SHARE),
            ("ENABLE_MEMORY", settings.ENABLE_MEMORY),
        )
        if value
    ]
    if enabled:
        raise RuntimeError(
            "Refusing to start: REQUIRE_CARD_LOGIN_ONLY=True 的正式姿態禁止啟用 "
            + ", ".join(enabled)
            + "；請先關閉這些未完成端到端分級控管的功能。"
        )


def assert_card_nonce_binding_policy() -> None:
    """Reject the replay-prone card mock bypass in every formal posture."""
    if not _env_truthy("CARD_DEV_SKIP_NONCE_BINDING"):
        return
    if _is_production_posture():
        raise RuntimeError(
            "Refusing to start: CARD_DEV_SKIP_NONCE_BINDING=true 是 dev-only "
            "憑證卡 mock 旁路，production/formal profile 禁止啟用。"
        )
    logger.warning(
        "[startup_security] CARD_DEV_SKIP_NONCE_BINDING=true dev-only 旁路已啟用；"
        "只可搭配固定測試簽章，不得用於正式環境。"
    )


def assert_secure_cookie_policy() -> None:
    """Formal profiles must emit standards-compliant ``__Host-`` cookies.

    ``COOKIE_SECURE=false`` selects a distinct ``anila_dev_*`` namespace so
    HTTP TestClient/local loops work without teaching production consumers to
    accept a legacy cookie name. It is therefore permitted only under the
    explicit development posture.
    """
    if settings.COOKIE_SECURE:
        return
    if _is_production_posture():
        raise RuntimeError(
            "Refusing to start: COOKIE_SECURE=false 僅限明示的 dev/test HTTP "
            "profile；production/formal profile 必須使用 Secure + __Host- cookies。"
        )
    logger.warning(
        "[startup_security] COOKIE_SECURE=false；使用 anila_dev_* cookies，"
        "僅適用 TestClient 或本機 HTTP 開發。"
    )


def assert_startup_migration_policy() -> None:
    """Production may never bypass the fail-stop Alembic startup gate."""
    if not settings.SKIP_STARTUP_MIGRATIONS:
        return
    if _is_production_posture():
        raise RuntimeError(
            "Refusing to start: SKIP_STARTUP_MIGRATIONS=true 僅限明示的 "
            "dev/test SQLite fixture，production/formal profile 禁止跳過 migration。"
        )
    logger.warning(
        "[startup_security] SKIP_STARTUP_MIGRATIONS=true；"
        "僅適用 dev/test 自行建立 schema 的 fixture。"
    )
