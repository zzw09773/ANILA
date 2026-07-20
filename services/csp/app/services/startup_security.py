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
import math
import os
from pathlib import Path
import re
import stat
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from cryptography import x509

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
    # Password profiles must be able to render without card bootstrap material,
    # so Compose cannot require this value globally.  The formal card posture's
    # CARD_OWNER_CONFIGURED assertion rejects both an empty value and this
    # placeholder before migrations/seeding, avoiding a card-only deployment
    # where every real user is pending and nobody can approve them.
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


def _resolved_posture() -> dict[str, object]:
    """Return only non-secret fields governed by deployment profiles."""

    return {
        "ANILA_ENV": os.environ.get("ANILA_ENV", "").strip().lower(),
        "ANILA_ALLOW_DEV_SECRET": _is_dev_mode(),
        "DEBUG": settings.DEBUG,
        "ENABLE_API_DOCS": settings.ENABLE_API_DOCS,
        "ENABLE_PUBLIC_SHARE": settings.ENABLE_PUBLIC_SHARE,
        "ENABLE_MEMORY": settings.ENABLE_MEMORY,
        "SKIP_STARTUP_MIGRATIONS": settings.SKIP_STARTUP_MIGRATIONS,
        "ALLOW_AUTO_KEYGEN": settings.ALLOW_AUTO_KEYGEN,
        "COOKIE_SECURE": settings.COOKIE_SECURE,
        "ENABLE_CARD_LOGIN": settings.ENABLE_CARD_LOGIN,
        "REQUIRE_CARD_LOGIN_ONLY": settings.REQUIRE_CARD_LOGIN_ONLY,
        "ANILA_ALLOW_HTTP_ENDPOINT": _env_truthy("ANILA_ALLOW_HTTP_ENDPOINT"),
        "ANILA_ALLOW_HTTP_AGENT_ENDPOINT": _env_truthy(
            "ANILA_ALLOW_HTTP_AGENT_ENDPOINT"
        ),
        "ANILA_ALLOW_PRIVATE_ENDPOINT": _env_truthy(
            "ANILA_ALLOW_PRIVATE_ENDPOINT"
        ),
        "CARD_DEV_SKIP_NONCE_BINDING": _env_truthy(
            "CARD_DEV_SKIP_NONCE_BINDING"
        ),
        "CARD_CRL_REQUIRED": settings.CARD_CRL_REQUIRED,
        "CARD_OWNER_CONFIGURED": _card_owner_configured(),
        # Formal profiles must use the CSP-owned readiness/snapshot gate;
        # legacy Agent dispatch is an explicit development-only bridge.
        "ALLOW_LEGACY_AGENT_DISPATCH": settings.ALLOW_LEGACY_AGENT_DISPATCH,
    }


def _card_owner_configured() -> bool:
    """Return whether card bootstrap has a real initial owner configured.

    Card-only deployments must have an out-of-band owner who can approve
    subsequent card users. Keep this check local to the card profiles so a
    password-only deployment does not acquire an unnecessary card dependency.
    """

    raw = str(getattr(settings, "CARD_INITIAL_OWNERS", "") or "").strip()
    if not raw:
        return False
    normalized_raw = raw.lower()
    if (
        normalized_raw in _PROD_PLACEHOLDERS
        or "<" in normalized_raw
        or ">" in normalized_raw
    ):
        return False
    owners = [part.strip().lower() for part in raw.split(",") if part.strip()]
    return bool(owners) and all(owner not in _PROD_PLACEHOLDERS for owner in owners)


_PROD_INTRANET_CARD_POSTURE: dict[str, object] = {
    "ANILA_ENV": "production",
    "ANILA_ALLOW_DEV_SECRET": False,
    "DEBUG": False,
    "ENABLE_API_DOCS": False,
    "ENABLE_PUBLIC_SHARE": False,
    "ENABLE_MEMORY": False,
    "SKIP_STARTUP_MIGRATIONS": False,
    "ALLOW_AUTO_KEYGEN": False,
    "COOKIE_SECURE": True,
    "ENABLE_CARD_LOGIN": True,
    "REQUIRE_CARD_LOGIN_ONLY": True,
    "ANILA_ALLOW_HTTP_ENDPOINT": False,
    # The reviewed intranet topology has an MLSteam agent on a plain-http
    # NodePort.  This agent-only exception must stay explicit; model/generic
    # endpoints remain HTTPS-only and private hosts still require allow-listing.
    "ANILA_ALLOW_HTTP_AGENT_ENDPOINT": True,
    "ANILA_ALLOW_PRIVATE_ENDPOINT": False,
    "CARD_DEV_SKIP_NONCE_BINDING": False,
    "CARD_CRL_REQUIRED": True,
    "CARD_OWNER_CONFIGURED": True,
    "ALLOW_LEGACY_AGENT_DISPATCH": False,
}

_PASSWORD_PRODUCTION_POSTURE: dict[str, object] = {
    "ANILA_ENV": "production",
    "ANILA_ALLOW_DEV_SECRET": False,
    "DEBUG": False,
    "ENABLE_API_DOCS": False,
    "ENABLE_PUBLIC_SHARE": False,
    "ENABLE_MEMORY": False,
    "SKIP_STARTUP_MIGRATIONS": False,
    "ALLOW_AUTO_KEYGEN": False,
    "COOKIE_SECURE": True,
    "ENABLE_CARD_LOGIN": False,
    "REQUIRE_CARD_LOGIN_ONLY": False,
    "ANILA_ALLOW_HTTP_ENDPOINT": False,
    "ANILA_ALLOW_HTTP_AGENT_ENDPOINT": False,
    "ANILA_ALLOW_PRIVATE_ENDPOINT": False,
    "CARD_DEV_SKIP_NONCE_BINDING": False,
    "CARD_CRL_REQUIRED": False,
    "ALLOW_LEGACY_AGENT_DISPATCH": False,
}

_FORMAL_PROFILE_POSTURES: dict[str, dict[str, object]] = {
    "prod-intranet-card": _PROD_INTRANET_CARD_POSTURE,
    # Availability recovery is a distinct, time-bounded posture rather than a
    # silent mutation of the normal card-only profile.  Card-only remains true:
    # only the owner password AMR is conditionally admitted at the JWT boundary.
    "prod-intranet-card-breakglass": dict(_PROD_INTRANET_CARD_POSTURE),
    # Keep each password profile as an independent first-class contract even
    # though their reviewed posture is currently identical.
    "prod-public-passwd": dict(_PASSWORD_PRODUCTION_POSTURE),
    "prod-military-passwd": dict(_PASSWORD_PRODUCTION_POSTURE),
    "trial-military": dict(_PASSWORD_PRODUCTION_POSTURE),
}

_AUDIT_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{2,127}$")
_FORMAL_SOURCE_SNAPSHOT_PATH = Path("/var/lib/anila/source-snapshots")
_FORMAL_ARTIFACT_BLOB_PATH = Path("/var/lib/anila/artifact-blobs")


def _parse_break_glass_expiry(raw: str) -> datetime:
    expires_at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if expires_at.tzinfo is None:
        raise ValueError("timezone required")
    return expires_at.astimezone(timezone.utc)


def is_break_glass_active(*, now: datetime | None = None) -> bool:
    """Return whether the password-owner exception is active *right now*.

    This is intentionally evaluated at every login/access/refresh boundary,
    not only at process startup, so an already-running container cannot keep a
    password session alive past the approved incident window.
    """

    if settings.ANILA_DEPLOYMENT_PROFILE.strip().lower() != (
        "prod-intranet-card-breakglass"
    ):
        return False
    owner = os.environ.get("ANILA_BREAK_GLASS_OWNER", "").strip()
    ticket = os.environ.get("ANILA_BREAK_GLASS_TICKET", "").strip()
    if not (
        _AUDIT_IDENTIFIER_RE.fullmatch(owner)
        and _AUDIT_IDENTIFIER_RE.fullmatch(ticket)
    ):
        return False
    try:
        expires_at = _parse_break_glass_expiry(
            os.environ.get("ANILA_BREAK_GLASS_EXPIRES_AT", "").strip()
        )
    except (ValueError, TypeError):
        return False
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return current < expires_at


def break_glass_audit_metadata() -> dict[str, object] | None:
    """Return safe incident identifiers for an active password exception."""

    if not is_break_glass_active():
        return None
    expires_at = _parse_break_glass_expiry(
        os.environ["ANILA_BREAK_GLASS_EXPIRES_AT"].strip()
    )
    return {
        "break_glass": True,
        "owner": os.environ["ANILA_BREAK_GLASS_OWNER"].strip(),
        "ticket": os.environ["ANILA_BREAK_GLASS_TICKET"].strip(),
        "expires_at": expires_at.isoformat(),
    }


def _assert_break_glass_metadata() -> None:
    owner = os.environ.get("ANILA_BREAK_GLASS_OWNER", "").strip()
    ticket = os.environ.get("ANILA_BREAK_GLASS_TICKET", "").strip()
    expires_raw = os.environ.get("ANILA_BREAK_GLASS_EXPIRES_AT", "").strip()
    missing = [
        name
        for name, value in (
            ("ANILA_BREAK_GLASS_OWNER", owner),
            ("ANILA_BREAK_GLASS_TICKET", ticket),
            ("ANILA_BREAK_GLASS_EXPIRES_AT", expires_raw),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(
            "Refusing to start: break-glass profile requires " + ", ".join(missing)
        )
    for name, value in (
        ("ANILA_BREAK_GLASS_OWNER", owner),
        ("ANILA_BREAK_GLASS_TICKET", ticket),
    ):
        if not _AUDIT_IDENTIFIER_RE.fullmatch(value):
            raise RuntimeError(
                f"Refusing to start: {name} must be a bounded audit identifier"
            )
    try:
        expires_at = _parse_break_glass_expiry(expires_raw)
    except ValueError as exc:
        raise RuntimeError(
            "Refusing to start: ANILA_BREAK_GLASS_EXPIRES_AT must be an "
            "RFC3339 timestamp with timezone"
        ) from exc
    now = datetime.now(timezone.utc)
    if expires_at <= now or expires_at > now + timedelta(hours=24):
        raise RuntimeError(
            "Refusing to start: break-glass expiry must be in the next 24 hours"
        )
    logger.critical(
        "[startup_security] BREAK-GLASS posture active; owner=%s ticket=%s "
        "expires_at=%s",
        owner,
        ticket,
        expires_at.isoformat(),
    )


def assert_deployment_profile_posture() -> None:
    """Fail startup when resolved flags contradict the declared profile.

    A branch name is not a security boundary: the same container can be
    started with arbitrary environment variables.  This assertion turns the
    formal deployment profile into an executable contract and runs before
    migrations, seeds, or workers begin.  Development/test remain explicit
    profiles, but they can never be paired with a production posture.
    """

    profile = settings.ANILA_DEPLOYMENT_PROFILE.strip().lower()
    if not profile:
        raise RuntimeError(
            "Refusing to start: ANILA_DEPLOYMENT_PROFILE must be declared"
        )

    if profile in {"development", "test"}:
        if _is_production_posture():
            raise RuntimeError(
                "Refusing to start: development/test deployment profile "
                "cannot be used with a production/formal posture"
            )
        return

    expected = _FORMAL_PROFILE_POSTURES.get(profile)
    if expected is None:
        raise RuntimeError(
            "Refusing to start: unknown ANILA_DEPLOYMENT_PROFILE "
            f"{profile!r}; add a reviewed posture contract before deployment"
        )

    actual = _resolved_posture()
    mismatches = [
        f"{name} expected {wanted!r}, got {actual[name]!r}"
        for name, wanted in expected.items()
        if actual[name] != wanted
    ]
    if mismatches:
        raise RuntimeError(
            f"Refusing to start: deployment profile {profile!r} posture "
            "mismatch: " + "; ".join(mismatches)
        )
    if profile == "prod-intranet-card-breakglass":
        _assert_break_glass_metadata()
    else:
        stale_metadata = [
            name
            for name in (
                "ANILA_BREAK_GLASS_OWNER",
                "ANILA_BREAK_GLASS_TICKET",
                "ANILA_BREAK_GLASS_EXPIRES_AT",
            )
            if os.environ.get(name, "").strip()
        ]
        if stale_metadata:
            raise RuntimeError(
                "Refusing to start: non-break-glass formal profile must not retain "
                "break-glass metadata: " + ", ".join(stale_metadata)
            )


def assert_source_snapshot_storage_policy() -> None:
    """Formal retrieval evidence must use the prepared private state mount."""

    profile = settings.ANILA_DEPLOYMENT_PROFILE.strip().lower()
    if profile not in _FORMAL_PROFILE_POSTURES:
        return
    configured = Path(settings.SOURCE_SNAPSHOT_STORAGE_PATH)
    if configured != _FORMAL_SOURCE_SNAPSHOT_PATH:
        raise RuntimeError(
            "Refusing to start: formal SOURCE_SNAPSHOT_STORAGE_PATH must be "
            f"{_FORMAL_SOURCE_SNAPSHOT_PATH}"
        )
    try:
        metadata = configured.lstat()
    except OSError as exc:
        raise RuntimeError(
            "Refusing to start: SourceSnapshot state mount is missing"
        ) from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeError(
            "Refusing to start: SourceSnapshot state mount must be a real directory"
        )
    runtime_uid = os.geteuid() if hasattr(os, "geteuid") else metadata.st_uid
    if metadata.st_uid != runtime_uid or metadata.st_mode & 0o077:
        raise RuntimeError(
            "Refusing to start: SourceSnapshot state mount must be owned by "
            "the CSP runtime user with mode 0700"
        )
    if not os.access(configured, os.W_OK | os.X_OK):
        raise RuntimeError(
            "Refusing to start: SourceSnapshot state mount is not writable"
        )


def assert_ingestion_queue_integrity_policy() -> None:
    """Formal CSP must authenticate every job published to shared Redis."""
    profile = settings.ANILA_DEPLOYMENT_PROFILE.strip().lower()
    if profile not in _FORMAL_PROFILE_POSTURES:
        return
    key = settings.INGESTION_QUEUE_HMAC_KEY.strip()
    if len(key) < 32 or key.startswith("dev-"):
        raise RuntimeError(
            "Refusing to start: formal ingestion queue HMAC key is missing or development-only"
        )


def assert_artifact_blob_storage_policy() -> None:
    """Formal immutable artifacts require a private CSP-owned state mount."""
    profile = settings.ANILA_DEPLOYMENT_PROFILE.strip().lower()
    if profile not in _FORMAL_PROFILE_POSTURES:
        return
    configured = Path(settings.ARTIFACT_BLOB_STORAGE_PATH)
    if configured != _FORMAL_ARTIFACT_BLOB_PATH:
        raise RuntimeError(
            "Refusing to start: formal ARTIFACT_BLOB_STORAGE_PATH must be "
            f"{_FORMAL_ARTIFACT_BLOB_PATH}"
        )
    try:
        metadata = configured.lstat()
    except OSError as exc:
        raise RuntimeError("Refusing to start: Artifact blob state mount is missing") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeError("Refusing to start: Artifact blob mount must be a real directory")
    runtime_uid = os.geteuid() if hasattr(os, "geteuid") else metadata.st_uid
    if metadata.st_uid != runtime_uid or metadata.st_mode & 0o077:
        raise RuntimeError(
            "Refusing to start: Artifact blob mount must be owned by the CSP "
            "runtime user with mode 0700"
        )
    if not os.access(configured, os.W_OK | os.X_OK):
        raise RuntimeError("Refusing to start: Artifact blob mount is not writable")


def assert_retention_policy() -> None:
    """Formal profiles must run one bounded, leased, fail-closed reaper."""
    profile = settings.ANILA_DEPLOYMENT_PROFILE.strip().lower()
    if profile not in _FORMAL_PROFILE_POSTURES:
        return
    if not settings.RETENTION_ENABLED:
        raise RuntimeError("Refusing to start: formal retention reaper is disabled")
    if settings.RETENTION_REAPER_INTERVAL_SECONDS >= settings.RETENTION_REAPER_LEASE_SECONDS:
        raise RuntimeError(
            "Refusing to start: retention lease must exceed the reaper interval"
        )
    if Path(settings.INGESTION_UPLOAD_DIR) != Path("/var/anila/ingestion-uploads"):
        raise RuntimeError(
            "Refusing to start: formal INGESTION_UPLOAD_DIR must be "
            "/var/anila/ingestion-uploads"
        )


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


def assert_card_crl_policy() -> None:
    """Formal card-only posture must provision offline revocation inputs."""
    if not settings.REQUIRE_CARD_LOGIN_ONLY:
        return
    if not settings.CARD_CRL_REQUIRED:
        raise RuntimeError(
            "Refusing to start: card-only profile requires CARD_CRL_REQUIRED=true"
        )
    path = Path(settings.CARD_CRL_BUNDLE_PATH)
    if not settings.CARD_CRL_BUNDLE_PATH or not path.is_file():
        raise RuntimeError(
            "Refusing to start: CARD_CRL_BUNDLE_PATH must be a mounted CRL file"
        )
    crl_source = settings.CARD_CRL_SOURCE.strip()
    if (
        not crl_source
        or (crl_source.startswith("<") and crl_source.endswith(">"))
        or crl_source.lower() in {"manual", "unknown", "todo", "placeholder"}
    ):
        raise RuntimeError(
            "Refusing to start: CARD_CRL_SOURCE must name the offline sync source/owner"
        )
    policy_values = [
        item.strip()
        for item in settings.CARD_REQUIRED_CERT_POLICY_OIDS.split(",")
        if item.strip()
    ]
    if not policy_values:
        raise RuntimeError(
            "Refusing to start: CARD_REQUIRED_CERT_POLICY_OIDS is required"
        )
    try:
        for value in policy_values:
            x509.ObjectIdentifier(value)
        x509.ObjectIdentifier(settings.CARD_REQUIRED_EKU_OID)
    except ValueError as exc:
        raise RuntimeError(
            "Refusing to start: card EKU/certificate policy OID is malformed"
        ) from exc
    from app.services.card_auth import CardAuthError, validate_card_crl_bundle

    try:
        validate_card_crl_bundle()
    except CardAuthError as exc:
        raise RuntimeError(
            f"Refusing to start: card CRL bundle validation failed: {exc}"
        ) from exc


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


def assert_runtime_deadline_policy() -> None:
    """Keep durable stream closure ahead of crash reconciliation.

    Streaming releases admission locks before bytes flow. If the stale-run
    reconciler can close that run while the configured stream is still valid,
    the real final usage closure is rejected by the terminal-state guard and
    usage is lost. Preserve one minute for cancellation propagation and the
    final durable closure transaction.
    """

    stream_seconds = float(settings.PROXY_STREAM_MAX_SECONDS)
    stale_seconds = float(settings.TASK_RUN_STALE_SECONDS)
    if not math.isfinite(stream_seconds) or stream_seconds <= 0:
        raise RuntimeError(
            "Refusing to start: PROXY_STREAM_MAX_SECONDS must be positive and finite"
        )
    if not math.isfinite(stale_seconds) or stale_seconds < 60:
        raise RuntimeError(
            "Refusing to start: TASK_RUN_STALE_SECONDS must be finite and at least 60"
        )
    if stream_seconds + 60 > stale_seconds:
        raise RuntimeError(
            "Refusing to start: PROXY_STREAM_MAX_SECONDS must leave at least "
            "60 seconds before TASK_RUN_STALE_SECONDS so durable usage closure "
            "cannot race crash reconciliation"
        )


_verified_pilot_callsites: frozenset[str] = frozenset()
_verified_pilot_admission = None


def assert_gate2_pilot_profile() -> None:
    """Pilot mode requires the four-owner signed machine profile.

    The disabled repository template intentionally fails this boundary. Trust
    keys and the actual approval profile must be provisioned out-of-band in
    the read-only secrets mount.
    """
    global _verified_pilot_admission, _verified_pilot_callsites
    if not settings.ANILA_PILOT_MODE:
        _verified_pilot_admission = None
        _verified_pilot_callsites = frozenset()
        return
    if settings.GATE2_PILOT_COMPOSE_POSTURE != "gate2-pilot-v1":
        raise RuntimeError(
            "Refusing to start Gate 2 pilot without the reviewed Compose "
            "posture marker; ANILA_PILOT_MODE alone does not apply the pilot overlay"
        )
    from anila_security import PilotProfileError, verify_signed_pilot_profile

    try:
        admission = verify_signed_pilot_profile(
            profile_path=settings.GATE2_PILOT_PROFILE_PATH,
            inventory_path=settings.GATE2_INFERENCE_INVENTORY_PATH,
            trust_store_path=settings.GATE2_PILOT_TRUST_STORE_PATH,
            expected_csp_image_id=settings.GATE2_CSP_IMAGE_ID,
        )
    except PilotProfileError as exc:
        raise RuntimeError(
            f"Refusing to start unsigned/invalid Gate 2 pilot: {exc}"
        ) from exc
    if "csp.agent_dispatch" in admission.enabled_callsites and not {
        item.strip()
        for item in settings.PILOT_FIRST_PARTY_AGENT_ALLOWLIST.split(",")
        if item.strip()
    }:
        raise RuntimeError(
            "Refusing to start Gate 2 pilot: enabled agent dispatch requires "
            "PILOT_FIRST_PARTY_AGENT_ALLOWLIST"
        )
    _verified_pilot_admission = admission
    _verified_pilot_callsites = admission.enabled_callsites


def _active_pilot_admission():
    if not settings.ANILA_PILOT_MODE:
        return None
    admission = _verified_pilot_admission
    if admission is None:
        raise RuntimeError("Gate 2 signed pilot admission is unavailable")
    now = datetime.now(timezone.utc)
    if not admission.valid_from <= now < admission.valid_until:
        raise RuntimeError("Gate 2 signed pilot profile is no longer effective")
    return admission


def require_pilot_callsite(callsite: str) -> None:
    """Reject runtime inference not present in the verified signed profile."""
    admission = _active_pilot_admission()
    if admission is not None and callsite not in admission.enabled_callsites:
        raise RuntimeError(f"Gate 2 signed pilot does not enable {callsite}")


def require_pilot_target(
    *, callsite: str, name: str, model_type: str, endpoint_url: str,
    classification_ceiling: str,
) -> None:
    """Require an exact signer-approved registry target at the network sink."""
    admission = _active_pilot_admission()
    if admission is None:
        return
    if not admission.target_allowed(
        callsite=callsite,
        name=name,
        model_type=model_type,
        endpoint_url=endpoint_url,
        classification_ceiling=classification_ceiling,
    ):
        raise RuntimeError(
            f"Gate 2 signed pilot does not authorize target {name!r} for {callsite}"
        )


def require_pilot_collection(collection_id: int) -> None:
    """Reject data access outside the exact signed pilot collection scope."""
    admission = _active_pilot_admission()
    if admission is not None and collection_id not in admission.collection_ids:
        raise RuntimeError(
            f"Gate 2 signed pilot does not authorize collection {collection_id}"
        )


def require_pilot_classification(level: str) -> None:
    """Enforce the signed data ceiling on every request and outbound sink."""
    admission = _active_pilot_admission()
    if admission is None:
        return
    from anila_contracts import Classification

    try:
        current = Classification.from_storage(level)
        ceiling = Classification.from_storage(admission.data_classification_ceiling)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Gate 2 pilot classification authority is invalid") from exc
    if current > ceiling:
        raise RuntimeError(
            "Gate 2 signed pilot data classification ceiling would be exceeded"
        )
