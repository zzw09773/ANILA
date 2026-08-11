"""Boot-boundary regression tests after settings reduction."""

from pathlib import Path


def test_boot_override_surface_is_gone():
    config = Path(__file__).parents[1] / "app/config.py"
    platform_api = Path(__file__).parents[1] / "app/api/platform_settings.py"
    main = Path(__file__).parents[1] / "app/main.py"
    config_text = config.read_text(encoding="utf-8")
    api_text = platform_api.read_text(encoding="utf-8")
    assert "BootOverrideSnapshot" not in config_text
    assert "apply_boot_overrides" not in config_text
    assert "boot_override" not in api_text
    assert "restart_required" not in api_text
    assert "pending" not in api_text


def test_startup_security_guards_keep_their_order_before_migrations():
    main = Path(__file__).parents[1] / "app/main.py"
    source = main.read_text(encoding="utf-8")
    guards = [
        "assert_no_dev_defaults()",
        "assert_intranet_lockdown_consistency()",
        "assert_card_dev_bypass_not_in_a_real_boot()",
    ]
    positions = [source.index(guard) for guard in guards]
    assert positions == sorted(positions)
    migration = source.index("run_startup_migrations()")
    assert all(position < migration for position in positions)


def test_only_twelve_c_settings_are_declared():
    from app.services.settings_registry import EDITABLE_CLASSES, SETTINGS, SettingClass

    assert len(SETTINGS) == 12
    assert EDITABLE_CLASSES == frozenset({SettingClass.C})
