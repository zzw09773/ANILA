"""設定收斂的契約測試。

這裡守治理頁仍承諾的十二顆 C 類設定，以及整棵 repo 的 Python env
reader 不得再讀本輪刪掉的 CSP 設定名。部署檔與腳本的同一份孤兒
清單則在交付報告中用逐名全樹掃描留證。
"""

from __future__ import annotations

import ast
import os
import subprocess
from pathlib import Path

import pytest

from app.models.platform_setting import PlatformSetting, get_setting, set_setting
from app.services.settings_registry import (
    EDITABLE_CLASSES,
    REGISTRY,
    SETTINGS,
    SettingClass,
    T_BOOL_NE_0,
)


KEEP_KEYS = {
    "institutional_kb.score_threshold",
    "memory.retrieve_min_cosine",
    "memory.retrieve_top_k",
    "proxy.llm_timeout",
    "proxy.embedding_timeout",
    "auth.access_token_expire_minutes",
    "auth.refresh_token_expire_days",
    "limits.department_max_depth",
    "limits.action_invoke_per_min",
    "limits.attachment_budget_ratio",
    "intl.zh_normalize",
    "intl.query_expansion",
}

REMOVED_ENV_NAMES = {
    "APP_NAME",
    "APP_VERSION",
    "DEBUG",
    "ALGORITHM",
    "JWT_PRIVATE_KEY_PATH",
    "JWT_PUBLIC_KEY_PATH",
    "ALLOW_AUTO_KEYGEN",
    "ADMIN_USERNAME",
    "PROXY_MAX_RETRIES",
    "PROXY_RETRY_BASE_DELAY",
    "HEALTH_CHECK_INTERVAL",
    "ALERT_CHECK_INTERVAL",
    "USAGE_BATCH_SIZE",
    "USAGE_FLUSH_INTERVAL",
    "ENABLE_CARD_LOGIN",
    "REQUIRE_CARD_LOGIN_ONLY",
    "AUTO_REGISTER_LINKS",
    "ATTACHMENT_STORAGE_PATH",
    "TOKEN_REVOCATION_REDIS_TIMEOUT_SECONDS",
    "ANILA_MESSAGE_MAX_SIBLINGS",
    "ANILA_ACTION_MAX_BODY_CHARS",
    "ANILA_DEFAULT_CONTEXT_WINDOW",
    "ANILA_ATTACHMENT_TOKEN_SAFETY",
    "ANILA_ATTACHMENT_MAX_STORED_TOKENS",
    "ANILA_ZIP_FILENAME_ENC",
    "MEMORY_MAX_CHUNK_CHARS",
    "MEMORY_HTTP_TIMEOUT",
    "ANILA_TEMPLATE_DIR",
    "VISION_VERIFY_SSL",
    "PDF_OCR_VISION_PROMPT",
    "PDF_OCR_DPI",
    "PDF_OCR_MAX_PAGES",
    "LEGACY_SQLITE_PATH",
    "INGESTION_UPLOAD_DIR",
    "CSP_SECRET_KEY",
    "COOKIE_SECURE",
    "STATIC_DIR",
}

_SETTINGS_REDUCTION_COMMIT = "18916a56"


def _python_sources() -> list[Path]:
    """Return every Python source file, including non-CSP services.

    A retired setting name is only safe when no process in this checkout can
    read it.  Keeping this scan at repo scope prevents a later service from
    silently reintroducing one of the old names behind CSP's local tests.
    """

    repo = Path(__file__).resolve().parents[3]
    skipped = {".git", "node_modules", "static", "__pycache__"}
    return [
        path
        for path in repo.rglob("*.py")
        if not skipped.intersection(path.parts)
    ]


def _env_reads(path: Path) -> list[tuple[str, int]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    reads: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr in {"get", "getenv"}
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
                and (
                    isinstance(func.value, ast.Name)
                    and func.value.id in {"environ", "os"}
                    or isinstance(func.value, ast.Attribute)
                    and func.value.attr == "environ"
                )
            ):
                reads.append((node.args[0].value, node.lineno))
        if isinstance(node, ast.Subscript):
            value = node.value
            if (
                isinstance(value, ast.Name)
                and value.id == "environ"
                or isinstance(value, ast.Attribute)
                and value.attr == "environ"
            ):
                literal = node.slice
                if isinstance(literal, ast.Constant) and isinstance(literal.value, str):
                    reads.append((literal.value, node.lineno))
    return reads


def _uppercase_class_assignments(source: str, class_name: str) -> set[str]:
    """Generate env-shaped class fields from a historical Settings source."""
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            names: set[str] = set()
            for statement in node.body:
                targets: list[ast.expr] = []
                if isinstance(statement, ast.AnnAssign):
                    targets.append(statement.target)
                elif isinstance(statement, ast.Assign):
                    targets.extend(statement.targets)
                for target in targets:
                    if isinstance(target, ast.Name) and target.id.isupper():
                        names.add(target.id)
            return names
    raise AssertionError(f"{class_name} class not found")


def _removed_config_env_names() -> set[str]:
    """Generate the removed config-field inventory from the reduction diff.

    The two token-expiry names remain live registry fallbacks even after their
    boot-snapshot fields are removed, so they are excluded from the retired
    env inventory rather than being hand-maintained as a special case.
    """
    repo = Path(__file__).resolve().parents[3]
    old_source = subprocess.check_output(
        [
            "git",
            "show",
            f"{_SETTINGS_REDUCTION_COMMIT}^:services/csp/app/config.py",
        ],
        cwd=repo,
        text=True,
    )
    from app.config import Settings

    old_fields = _uppercase_class_assignments(old_source, "Settings")
    current_fields = set(Settings.model_fields)
    active_fallbacks = {
        spec.env_name for spec in SETTINGS if spec.env_name is not None
    }
    return old_fields - current_fields - active_fallbacks


def test_registry_is_exactly_the_twelve_immediate_settings():
    assert {spec.key for spec in SETTINGS} == KEEP_KEYS
    assert set(REGISTRY) == KEEP_KEYS
    assert all(spec.setting_class is SettingClass.C for spec in SETTINGS)
    assert EDITABLE_CLASSES == frozenset({SettingClass.C})
    assert sum(spec.value_type is T_BOOL_NE_0 for spec in SETTINGS) == 2


def test_removed_settings_have_no_python_environment_reader():
    violations: list[str] = []
    for path in _python_sources():
        for name, lineno in _env_reads(path):
            if name in REMOVED_ENV_NAMES:
                violations.append(f"{path}:{lineno}: {name}")
    assert violations == []


def test_removed_env_inventory_covers_generated_config_removals():
    """A newly omitted Settings field cannot evade the retired-name guard."""
    assert _removed_config_env_names() <= REMOVED_ENV_NAMES


def test_settings_reduction_downgrade_refuses_to_fabricate_rows():
    from migrations.versions import r1_0035_settings_reduction as migration

    with pytest.raises(NotImplementedError, match="cannot restore"):
        migration.downgrade()


def test_db_overrides_env_and_boolean_parser_is_one_contract(db, monkeypatch):
    monkeypatch.setenv("LLM_TIMEOUT", "77")
    assert get_setting(db, "proxy.llm_timeout") == 77
    db.add(PlatformSetting(key="proxy.llm_timeout", value="88"))
    db.commit()
    assert get_setting(db, "proxy.llm_timeout") == 88

    monkeypatch.setenv("ANILA_ZH_NORMALIZE", "false")
    assert get_setting(db, "intl.zh_normalize") is True
    set_setting(db, "intl.zh_normalize", "false")
    db.commit()
    assert get_setting(db, "intl.zh_normalize") is False


def test_no_removed_legacy_env_names_are_present_in_settings_config():
    from app.config import Settings

    fields = set(Settings.model_fields)
    assert fields.isdisjoint(REMOVED_ENV_NAMES)
    assert "ANILA_AUTH_MODE" in fields
    assert "SECRET_KEY" in fields
