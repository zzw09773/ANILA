"""Sprint 6 X / A5: regression tests for ``app.services.startup_security``.

Covers the three states the gate can land in:

1. Dev mode (``ANILA_ALLOW_DEV_SECRET=1``) → known-default values only
   produce a WARNING; ``assert_no_dev_defaults`` returns cleanly.
2. Production mode (``ANILA_ALLOW_DEV_SECRET`` unset) → known-default
   values raise ``RuntimeError`` so uvicorn never finishes startup.
3. Production mode + every secret overridden → returns cleanly.

These tests poke ``settings`` directly via monkeypatch so they don't
require the docker stack to be running.
"""
from __future__ import annotations

import importlib
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture
def reload_startup_security(monkeypatch):
    """Reimport the module under test fresh for each test.

    Keep the process-global ``app.config.settings`` object intact: reloading
    app.config here used to poison later modules that imported that object.
    Instead bind a fresh Settings instance only inside startup_security for
    this test; monkeypatch restores the original binding afterwards.
    """
    def _factory():
        from app.config import Settings
        import app.services.startup_security as ss_module
        importlib.reload(ss_module)
        monkeypatch.setattr(
            ss_module,
            "settings",
            Settings(_env_file=None),
        )
        return ss_module
    return _factory


def _override_all_to_safe(monkeypatch):
    """Set every guarded var to a non-default safe value."""
    monkeypatch.setenv("SECRET_KEY", "real-prod-secret-" + "a" * 40)
    monkeypatch.setenv("ADMIN_PASSWORD", "real-admin-pw-" + "a" * 20)
    monkeypatch.setenv("CSP_SERVICE_TOKEN", "real-service-token-" + "a" * 20)
    # DATABASE_URL password 部分；URL parser 拿 password
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://csp_app:RealStrongDbPassword123@db:5432/csp",
    )
    monkeypatch.setenv("INTERNAL_PLATFORM_API_KEY", "sk-real-internal-" + "a" * 30)
    monkeypatch.setenv("CODESERVER_PASSWORD", "real-codeserver-" + "a" * 20)


def test_dev_mode_warns_but_allows(monkeypatch, caplog, reload_startup_security):
    """ANILA_ALLOW_DEV_SECRET=1 → defaults logged as WARNING, no raise."""
    monkeypatch.setenv("ANILA_ALLOW_DEV_SECRET", "1")
    monkeypatch.setenv("SECRET_KEY", "dev-secret-key-change-in-prod")
    monkeypatch.setenv("ADMIN_PASSWORD", "changeme")
    monkeypatch.setenv("CSP_SERVICE_TOKEN", "dev-service-token")
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql://csp_app:csp_password@db:5432/csp",
    )

    ss = reload_startup_security()
    with caplog.at_level("WARNING"):
        ss.assert_no_dev_defaults()  # 不該 raise

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert any(
        "dev 預設值" in r.getMessage() for r in warnings
    ), "expected at least one 'dev 預設值' WARNING in dev mode"


@pytest.mark.parametrize(
    ("anila_env", "card_only"),
    [("production", "false"), ("development", "true")],
)
def test_formal_posture_rejects_dev_secret_bypass_even_with_safe_values(
    anila_env,
    card_only,
    monkeypatch,
    reload_startup_security,
):
    _override_all_to_safe(monkeypatch)
    monkeypatch.setenv("ANILA_ALLOW_DEV_SECRET", "1")
    monkeypatch.setenv("ANILA_ENV", anila_env)
    monkeypatch.setenv("REQUIRE_CARD_LOGIN_ONLY", card_only)
    monkeypatch.setenv("ENABLE_CARD_LOGIN", card_only)

    ss = reload_startup_security()
    with pytest.raises(RuntimeError, match="ANILA_ALLOW_DEV_SECRET"):
        ss.assert_no_dev_defaults()


def test_production_raises_on_default_secret(monkeypatch, reload_startup_security):
    """ANILA_ALLOW_DEV_SECRET unset → defaults raise RuntimeError."""
    monkeypatch.delenv("ANILA_ALLOW_DEV_SECRET", raising=False)
    monkeypatch.setenv("SECRET_KEY", "dev-secret-key-change-in-prod")
    # 其他變數先設成安全值，確認 SECRET_KEY 單獨能觸發 raise。
    _override_all_to_safe(monkeypatch)
    monkeypatch.setenv("SECRET_KEY", "dev-secret-key-change-in-prod")

    ss = reload_startup_security()
    with pytest.raises(RuntimeError) as excinfo:
        ss.assert_no_dev_defaults()
    assert "SECRET_KEY" in str(excinfo.value)


def test_production_raises_on_default_admin(monkeypatch, reload_startup_security):
    monkeypatch.delenv("ANILA_ALLOW_DEV_SECRET", raising=False)
    _override_all_to_safe(monkeypatch)
    monkeypatch.setenv("ADMIN_PASSWORD", "changeme")

    ss = reload_startup_security()
    with pytest.raises(RuntimeError) as excinfo:
        ss.assert_no_dev_defaults()
    assert "ADMIN_PASSWORD" in str(excinfo.value)


def test_production_raises_on_default_db_password(monkeypatch, reload_startup_security):
    monkeypatch.delenv("ANILA_ALLOW_DEV_SECRET", raising=False)
    _override_all_to_safe(monkeypatch)
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql://csp_app:csp_password@db:5432/csp",
    )

    ss = reload_startup_security()
    with pytest.raises(RuntimeError) as excinfo:
        ss.assert_no_dev_defaults()
    assert "DB_PASSWORD" in str(excinfo.value)


def test_production_raises_on_default_codeserver_password(
    monkeypatch, reload_startup_security
):
    monkeypatch.delenv("ANILA_ALLOW_DEV_SECRET", raising=False)
    _override_all_to_safe(monkeypatch)
    monkeypatch.setenv("CODESERVER_PASSWORD", "changeme-codeserver")

    ss = reload_startup_security()
    with pytest.raises(RuntimeError) as excinfo:
        ss.assert_no_dev_defaults()
    assert "CODESERVER_PASSWORD" in str(excinfo.value)


def test_production_passes_with_all_overrides(monkeypatch, reload_startup_security):
    """All vars overridden → returns cleanly even with dev mode disabled."""
    monkeypatch.delenv("ANILA_ALLOW_DEV_SECRET", raising=False)
    _override_all_to_safe(monkeypatch)

    ss = reload_startup_security()
    ss.assert_no_dev_defaults()  # 不該 raise


def test_empty_secret_key_raises_even_in_dev(monkeypatch, reload_startup_security):
    """空 SECRET_KEY 永遠是 fatal — 即使 dev mode 也應 raise。

    （實作上空字串視為「未覆寫」最危險的形態；assert_no_dev_defaults
    把它列入 offenders 直接 raise，無視 dev_mode flag。）
    """
    monkeypatch.setenv("ANILA_ALLOW_DEV_SECRET", "1")
    _override_all_to_safe(monkeypatch)
    monkeypatch.setenv("SECRET_KEY", "")

    ss = reload_startup_security()
    with pytest.raises(RuntimeError) as excinfo:
        ss.assert_no_dev_defaults()
    assert "SECRET_KEY" in str(excinfo.value)


@pytest.mark.parametrize("value", ["1", "true", "yes", "ON"])
def test_production_rejects_card_nonce_skip(
    value, monkeypatch, reload_startup_security
):
    monkeypatch.delenv("ANILA_ALLOW_DEV_SECRET", raising=False)
    monkeypatch.setenv("ANILA_ENV", "production")
    monkeypatch.setenv("CARD_DEV_SKIP_NONCE_BINDING", value)
    _override_all_to_safe(monkeypatch)

    ss = reload_startup_security()
    with pytest.raises(RuntimeError, match="CARD_DEV_SKIP_NONCE_BINDING"):
        ss.assert_card_nonce_binding_policy()


def test_production_env_rejects_card_nonce_skip_even_with_dev_secret_opt_in(
    monkeypatch, reload_startup_security
):
    monkeypatch.setenv("ANILA_ALLOW_DEV_SECRET", "1")
    monkeypatch.setenv("ANILA_ENV", "production")
    monkeypatch.setenv("CARD_DEV_SKIP_NONCE_BINDING", "true")

    ss = reload_startup_security()
    with pytest.raises(RuntimeError, match="CARD_DEV_SKIP_NONCE_BINDING"):
        ss.assert_card_nonce_binding_policy()


def test_formal_card_only_profile_rejects_nonce_skip_even_with_dev_opt_in(
    monkeypatch, reload_startup_security
):
    monkeypatch.setenv("ANILA_ALLOW_DEV_SECRET", "1")
    monkeypatch.setenv("ANILA_ENV", "development")
    monkeypatch.setenv("REQUIRE_CARD_LOGIN_ONLY", "true")
    monkeypatch.setenv("ENABLE_CARD_LOGIN", "true")
    monkeypatch.setenv("CARD_DEV_SKIP_NONCE_BINDING", "true")

    ss = reload_startup_security()
    with pytest.raises(RuntimeError, match="CARD_DEV_SKIP_NONCE_BINDING"):
        ss.assert_card_nonce_binding_policy()


def test_explicit_dev_profile_allows_card_nonce_skip(
    monkeypatch, caplog, reload_startup_security
):
    monkeypatch.setenv("ANILA_ALLOW_DEV_SECRET", "1")
    monkeypatch.setenv("ANILA_ENV", "development")
    monkeypatch.setenv("CARD_DEV_SKIP_NONCE_BINDING", "true")

    ss = reload_startup_security()
    with caplog.at_level("WARNING"):
        ss.assert_card_nonce_binding_policy()
    assert "dev-only" in caplog.text


def test_production_rejects_startup_migration_skip(
    monkeypatch, reload_startup_security
):
    monkeypatch.delenv("ANILA_ALLOW_DEV_SECRET", raising=False)
    monkeypatch.setenv("ANILA_ENV", "production")
    monkeypatch.setenv("SKIP_STARTUP_MIGRATIONS", "true")

    ss = reload_startup_security()
    with pytest.raises(RuntimeError, match="SKIP_STARTUP_MIGRATIONS"):
        ss.assert_startup_migration_policy()


def test_explicit_dev_profile_allows_startup_migration_skip(
    monkeypatch, reload_startup_security
):
    monkeypatch.setenv("ANILA_ALLOW_DEV_SECRET", "1")
    monkeypatch.setenv("ANILA_ENV", "test")
    monkeypatch.setenv("SKIP_STARTUP_MIGRATIONS", "true")

    ss = reload_startup_security()
    ss.assert_startup_migration_policy()


def test_production_rejects_insecure_cookie_profile(
    monkeypatch, reload_startup_security
):
    monkeypatch.setenv("ANILA_ALLOW_DEV_SECRET", "1")
    monkeypatch.setenv("ANILA_ENV", "production")
    monkeypatch.setenv("COOKIE_SECURE", "false")

    ss = reload_startup_security()
    with pytest.raises(RuntimeError, match="COOKIE_SECURE"):
        ss.assert_secure_cookie_policy()


def test_card_only_rejects_insecure_cookie_profile(
    monkeypatch, reload_startup_security
):
    monkeypatch.setenv("ANILA_ALLOW_DEV_SECRET", "1")
    monkeypatch.setenv("ANILA_ENV", "development")
    monkeypatch.setenv("REQUIRE_CARD_LOGIN_ONLY", "true")
    monkeypatch.setenv("ENABLE_CARD_LOGIN", "true")
    monkeypatch.setenv("COOKIE_SECURE", "false")

    ss = reload_startup_security()
    with pytest.raises(RuntimeError, match="COOKIE_SECURE"):
        ss.assert_secure_cookie_policy()


def test_explicit_dev_profile_allows_distinct_insecure_cookie_names(
    monkeypatch, caplog, reload_startup_security
):
    monkeypatch.setenv("ANILA_ALLOW_DEV_SECRET", "1")
    monkeypatch.setenv("ANILA_ENV", "development")
    monkeypatch.setenv("REQUIRE_CARD_LOGIN_ONLY", "false")
    monkeypatch.setenv("COOKIE_SECURE", "false")

    ss = reload_startup_security()
    with caplog.at_level("WARNING"):
        ss.assert_secure_cookie_policy()
    assert "anila_dev_*" in caplog.text


def test_formal_secure_cookie_profile_is_allowed(
    monkeypatch, reload_startup_security
):
    monkeypatch.delenv("ANILA_ALLOW_DEV_SECRET", raising=False)
    monkeypatch.setenv("ANILA_ENV", "production")
    monkeypatch.setenv("COOKIE_SECURE", "true")

    ss = reload_startup_security()
    ss.assert_secure_cookie_policy()


@pytest.mark.parametrize("enabled_feature", ["ENABLE_PUBLIC_SHARE", "ENABLE_MEMORY"])
def test_card_only_rejects_data_features_from_stale_dev_environment(
    enabled_feature, monkeypatch, reload_startup_security
):
    """A leaked dev opt-in must not weaken the formal card-only posture."""
    monkeypatch.setenv("ANILA_ALLOW_DEV_SECRET", "1")
    monkeypatch.setenv("ANILA_ENV", "development")
    monkeypatch.setenv("REQUIRE_CARD_LOGIN_ONLY", "true")
    monkeypatch.setenv("ENABLE_CARD_LOGIN", "true")
    monkeypatch.setenv("ENABLE_PUBLIC_SHARE", "false")
    monkeypatch.setenv("ENABLE_MEMORY", "false")
    monkeypatch.setenv(enabled_feature, "true")

    ss = reload_startup_security()
    with pytest.raises(RuntimeError, match=enabled_feature):
        ss.assert_card_only_data_feature_policy()


def test_non_card_dev_profile_may_opt_in_to_data_features(
    monkeypatch, reload_startup_security
):
    monkeypatch.setenv("ANILA_ALLOW_DEV_SECRET", "1")
    monkeypatch.setenv("ANILA_ENV", "development")
    monkeypatch.setenv("REQUIRE_CARD_LOGIN_ONLY", "false")
    monkeypatch.setenv("ENABLE_PUBLIC_SHARE", "true")
    monkeypatch.setenv("ENABLE_MEMORY", "true")

    ss = reload_startup_security()
    ss.assert_card_only_data_feature_policy()


def _set_formal_card_profile(monkeypatch) -> None:
    values = {
        "ANILA_DEPLOYMENT_PROFILE": "prod-intranet-card",
        "ANILA_ENV": "production",
        "ANILA_ALLOW_DEV_SECRET": "0",
        "DEBUG": "false",
        "ENABLE_API_DOCS": "false",
        "ENABLE_PUBLIC_SHARE": "false",
        "ENABLE_MEMORY": "false",
        "SKIP_STARTUP_MIGRATIONS": "false",
        "ALLOW_AUTO_KEYGEN": "false",
        "COOKIE_SECURE": "true",
        "ENABLE_CARD_LOGIN": "true",
        "REQUIRE_CARD_LOGIN_ONLY": "true",
        "ANILA_ALLOW_HTTP_ENDPOINT": "0",
        "ANILA_ALLOW_HTTP_AGENT_ENDPOINT": "1",
        "ANILA_ALLOW_PRIVATE_ENDPOINT": "0",
        "CARD_DEV_SKIP_NONCE_BINDING": "false",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def test_declared_formal_card_profile_accepts_exact_posture(
    monkeypatch, reload_startup_security
):
    _set_formal_card_profile(monkeypatch)
    reload_startup_security().assert_deployment_profile_posture()


def test_normal_card_profile_rejects_stale_break_glass_metadata(
    monkeypatch, reload_startup_security
):
    _set_formal_card_profile(monkeypatch)
    monkeypatch.setenv("ANILA_BREAK_GLASS_TICKET", "INC-STALE-001")
    with pytest.raises(RuntimeError, match="must not retain break-glass metadata"):
        reload_startup_security().assert_deployment_profile_posture()


@pytest.mark.parametrize(
    ("name", "bad_value"),
    [
        ("ANILA_ENV", "development"),
        ("ANILA_ALLOW_DEV_SECRET", "1"),
        ("DEBUG", "true"),
        ("ENABLE_API_DOCS", "true"),
        ("ENABLE_PUBLIC_SHARE", "true"),
        ("ENABLE_MEMORY", "true"),
        ("SKIP_STARTUP_MIGRATIONS", "true"),
        ("ALLOW_AUTO_KEYGEN", "true"),
        ("COOKIE_SECURE", "false"),
        ("ENABLE_CARD_LOGIN", "false"),
        ("REQUIRE_CARD_LOGIN_ONLY", "false"),
        ("ANILA_ALLOW_HTTP_ENDPOINT", "1"),
        ("ANILA_ALLOW_HTTP_AGENT_ENDPOINT", "0"),
        ("ANILA_ALLOW_PRIVATE_ENDPOINT", "1"),
        ("CARD_DEV_SKIP_NONCE_BINDING", "true"),
    ],
)
def test_declared_formal_card_profile_rejects_each_mismatch(
    name, bad_value, monkeypatch, reload_startup_security
):
    _set_formal_card_profile(monkeypatch)
    monkeypatch.setenv(name, bad_value)
    with pytest.raises(RuntimeError, match=name):
        reload_startup_security().assert_deployment_profile_posture()


def test_unknown_formal_profile_is_fail_closed(
    monkeypatch, reload_startup_security
):
    _set_formal_card_profile(monkeypatch)
    monkeypatch.setenv("ANILA_DEPLOYMENT_PROFILE", "prod-unreviewed")
    with pytest.raises(RuntimeError, match="unknown ANILA_DEPLOYMENT_PROFILE"):
        reload_startup_security().assert_deployment_profile_posture()


def test_development_profile_cannot_mask_production_posture(
    monkeypatch, reload_startup_security
):
    monkeypatch.setenv("ANILA_DEPLOYMENT_PROFILE", "development")
    monkeypatch.setenv("ANILA_ENV", "production")
    monkeypatch.setenv("ANILA_ALLOW_DEV_SECRET", "1")
    with pytest.raises(RuntimeError, match="cannot be used"):
        reload_startup_security().assert_deployment_profile_posture()


def test_test_profile_is_allowed_only_in_explicit_dev_posture(
    monkeypatch, reload_startup_security
):
    monkeypatch.setenv("ANILA_DEPLOYMENT_PROFILE", "test")
    monkeypatch.setenv("ANILA_ENV", "test")
    monkeypatch.setenv("ANILA_ALLOW_DEV_SECRET", "1")
    monkeypatch.setenv("REQUIRE_CARD_LOGIN_ONLY", "false")
    reload_startup_security().assert_deployment_profile_posture()


def test_break_glass_is_named_time_bounded_and_auditable(
    monkeypatch, caplog, reload_startup_security
):
    _set_formal_card_profile(monkeypatch)
    monkeypatch.setenv(
        "ANILA_DEPLOYMENT_PROFILE", "prod-intranet-card-breakglass"
    )
    monkeypatch.setenv("ANILA_BREAK_GLASS_OWNER", "system-owner")
    monkeypatch.setenv("ANILA_BREAK_GLASS_TICKET", "INC-2026-001")
    monkeypatch.setenv(
        "ANILA_BREAK_GLASS_EXPIRES_AT",
        (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    )
    with caplog.at_level("CRITICAL"):
        reload_startup_security().assert_deployment_profile_posture()
    assert "BREAK-GLASS posture active" in caplog.text


@pytest.mark.parametrize(
    ("missing", "bad_expiry"),
    [
        ("ANILA_BREAK_GLASS_OWNER", None),
        ("ANILA_BREAK_GLASS_TICKET", None),
        ("ANILA_BREAK_GLASS_EXPIRES_AT", None),
        (None, "not-a-timestamp"),
        (None, "2999-01-01T00:00:00Z"),
    ],
)
def test_break_glass_rejects_missing_or_invalid_metadata(
    missing, bad_expiry, monkeypatch, reload_startup_security
):
    _set_formal_card_profile(monkeypatch)
    monkeypatch.setenv(
        "ANILA_DEPLOYMENT_PROFILE", "prod-intranet-card-breakglass"
    )
    monkeypatch.setenv("ANILA_BREAK_GLASS_OWNER", "system-owner")
    monkeypatch.setenv("ANILA_BREAK_GLASS_TICKET", "INC-2026-001")
    monkeypatch.setenv(
        "ANILA_BREAK_GLASS_EXPIRES_AT",
        bad_expiry
        or (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    )
    if missing:
        monkeypatch.delenv(missing, raising=False)
    with pytest.raises(RuntimeError, match="break-glass|BREAK_GLASS"):
        reload_startup_security().assert_deployment_profile_posture()


def test_break_glass_runtime_gate_closes_after_expiry(
    monkeypatch, reload_startup_security
):
    _set_formal_card_profile(monkeypatch)
    monkeypatch.setenv(
        "ANILA_DEPLOYMENT_PROFILE", "prod-intranet-card-breakglass"
    )
    monkeypatch.setenv("ANILA_BREAK_GLASS_OWNER", "system-owner")
    monkeypatch.setenv("ANILA_BREAK_GLASS_TICKET", "INC-2026-001")
    monkeypatch.setenv(
        "ANILA_BREAK_GLASS_EXPIRES_AT",
        (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
    )
    ss = reload_startup_security()
    assert ss.is_break_glass_active()
    assert ss.break_glass_audit_metadata()["ticket"] == "INC-2026-001"
    assert not ss.is_break_glass_active(
        now=datetime.now(timezone.utc) + timedelta(minutes=6)
    )
    monkeypatch.delenv("ANILA_BREAK_GLASS_TICKET")
    assert not ss.is_break_glass_active()


@pytest.mark.asyncio
async def test_lifespan_rejects_profile_drift_before_migrations(
    monkeypatch, reload_startup_security
):
    _set_formal_card_profile(monkeypatch)
    monkeypatch.setenv("ENABLE_PUBLIC_SHARE", "true")
    reload_startup_security()

    import app.main as main_module

    migration_called = False

    def migration_probe(_app):
        nonlocal migration_called
        migration_called = True

    monkeypatch.setattr(main_module, "_apply_schema_migrations", migration_probe)
    with pytest.raises(RuntimeError, match="ENABLE_PUBLIC_SHARE"):
        async with main_module.lifespan(main_module.app):
            pass
    assert migration_called is False
