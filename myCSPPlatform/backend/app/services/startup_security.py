"""Startup-time guard for known dev defaults.

Sprint 5 X security review (M1): the platform shipped with several env
vars whose defaults are publicly committed in ``.env.example`` and
``docker-compose.yml`` (``SECRET_KEY``, ``ADMIN_PASSWORD``,
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
_KNOWN_DEFAULTS: dict[str, frozenset[str]] = {
    "SECRET_KEY": frozenset({
        "your-secret-key-change-this-in-production",
        "dev-secret-key-change-in-prod",
        "change-me",
        "change_me",
        "secret",
    }),
    "ADMIN_PASSWORD": frozenset({"changeme", "password", "admin"}),
    "CSP_SERVICE_TOKEN": frozenset({"dev-service-token", "changeme"}),
    "DB_PASSWORD": frozenset({"csp_password", "csp", "postgres", "password"}),
    "INTERNAL_PLATFORM_API_KEY": frozenset({
        "sk-internal-worker-changeme",
        "sk-changeme",
    }),
    "CODESERVER_PASSWORD": frozenset({"changeme-codeserver", "changeme"}),
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


def assert_intranet_lockdown_consistency() -> None:
    """Branch ``SSO``：``REQUIRE_CARD_LOGIN_ONLY`` 與其他 auth flag 的相容性。

    中科院內網 production 政策是「**卡片登入是唯一活路**」 — 本機帳密、
    OIDC、自助註冊全部禁用。要 enforce 這個政策，必須 ``ENABLE_CARD_LOGIN``
    同時啟用，否則整個系統會處於「沒人能登入」的 bricked 狀態。

    本檢查在 ``lifespan`` 啟動時跑；不通過直接拒絕啟動 — secure by default
    at deployment time，比 runtime check 強。
    """
    if not settings.REQUIRE_CARD_LOGIN_ONLY:
        return

    if not settings.ENABLE_CARD_LOGIN:
        raise RuntimeError(
            "Refusing to start: REQUIRE_CARD_LOGIN_ONLY=True 但 "
            "ENABLE_CARD_LOGIN=False — 將無人能登入。請同時啟用 "
            "ENABLE_CARD_LOGIN=true，或關閉 REQUIRE_CARD_LOGIN_ONLY。"
        )

    if (settings.CARD_VERIFY_MODE or "").lower() == "loose":
        # LOOSE mode 信任 cht/ mock — 不該在強制卡片登入的 production 出現。
        # 不直接 raise（避免擋掉「先 loose 上線、補 strict」的演進）但給強警告。
        logger.warning(
            "[startup_security] REQUIRE_CARD_LOGIN_ONLY=True 但 "
            "CARD_VERIFY_MODE='loose' — production 內網應改用 'strict'。"
        )
