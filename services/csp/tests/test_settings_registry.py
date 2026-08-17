"""設定收斂的契約測試。

這裡守治理頁仍承諾的十二顆 C 類設定，以及整棵 repo 的 Python env
reader 不得再讀本輪刪掉的 CSP 設定名。

⚠ 2026-08-17：**部署檔與腳本這一側現在也由本檔掃描**（`*.yml`／`*.yaml`／
`*.sh`／`.env.example`），不再只靠交付報告的人工全樹掃描留證。
舊寫法只看 `*.py`，守的是**消費端**；而 FAKE-CONTROLS #59 那三顆
asr-gateway 死旋鈕長在**生產端**（compose），所以整整活到凍結前才被
一次文件逐句稽核偶然撞到。守衛的視野要蓋住它自己宣稱要守的範圍。
"""

from __future__ import annotations

import ast
import os
import re
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


_SKIPPED_DIRS = {".git", "node_modules", "static", "__pycache__"}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _python_sources() -> list[Path]:
    """Return every Python source file, including non-CSP services.

    A retired setting name is only safe when no process in this checkout can
    read it.  Keeping this scan at repo scope prevents a later service from
    silently reintroducing one of the old names behind CSP's local tests.
    """

    return [
        path
        for path in _repo_root().rglob("*.py")
        if not _SKIPPED_DIRS.intersection(path.parts)
    ]


# ── 部署端(生產端)掃描 ────────────────────────────────────────────────
# Python 那側用 AST,分得開「提到名字」與「真的讀取」。yml／sh 只能做文字
# 比對,而**最常提到已退役名字的地方,正是解釋它為什麼被退役的註解**
# (`services/asr-gateway/app/config.py:91-106` 現在就是這樣)。
#
# 所以這裡一律比對**賦值形狀**,不比對「出現過」——註解行因此自然被排除,
# 不需要維護任何 allowlist。誤報一次,這道守衛就再也沒有人看了。
_DEPLOYMENT_GLOBS = ("*.yml", "*.yaml", "*.sh", ".env.example")


def _deployment_sources() -> list[Path]:
    """Deployment-side files that ship inside the delivery bundle.

    ⚠ 只收 `.env.example`(**受追蹤**、隨出貨包走),刻意**不收**裸 `.env`
    與 `.env.bak*`:那些是 gitignored 的機器本機檔,內容因機器而異,收進來
    會讓這道守衛在某些開發機上恆紅、在另一些機器上恆綠——不可重現的紅燈
    等於噪音,而噪音就是這道守衛失效的方式。
    """

    repo = _repo_root()
    return [
        path
        for pattern in _DEPLOYMENT_GLOBS
        for path in repo.rglob(pattern)
        if not _SKIPPED_DIRS.intersection(path.parts)
    ]


def _assignment_pattern(path: Path, name: str) -> str:
    """賦值形狀:yml `KEY:`、sh `[export ]KEY=`、env 檔 `KEY=`(不容前導空白)。"""
    if path.name == ".env.example":
        return rf"^{re.escape(name)}="
    if path.suffix == ".sh":
        return rf"^[ \t]*(?:export[ \t]+)?{re.escape(name)}="
    return rf"^[ \t]*{re.escape(name)}:"


def _env_assignments(path: Path) -> list[tuple[str, int]]:
    text = path.read_text(encoding="utf-8", errors="replace")
    found: list[tuple[str, int]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for name in REMOVED_ENV_NAMES:
            if re.match(_assignment_pattern(path, name), line):
                found.append((name, lineno))
    return found


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


def test_removed_settings_have_no_deployment_declaration():
    """部署檔不得再宣告已退役的設定名(FAKE-CONTROLS #59 的形狀)。

    「沒人讀」那半本來就有 `test_removed_settings_have_no_python_environment_reader`
    在看;這一條看的是**沒人宣告**——維運者編輯的是 compose,不會去讀服務的
    設定模組,所以只寫在消費端的「刻意不收」約定,對生產端等於不存在。
    """
    sources = _deployment_sources()

    # 正向錨點:掃到 0 個檔案時,下面那條負向斷言會**恆真**(repo root 算錯、
    # glob 打錯都會這樣)。先證明我們真的看過這道守衛存在的理由那兩個檔。
    scanned = {path.name for path in sources}
    assert {"platform.yml", "dev.yml"} <= scanned, f"deployment scan missed compose: {sorted(scanned)[:20]}"
    assert len(sources) > 40, f"deployment scan surface implausibly small: {len(sources)}"

    violations: list[str] = []
    for path in sources:
        for name, lineno in _env_assignments(path):
            violations.append(f"{path.relative_to(_repo_root())}:{lineno}: {name}")
    assert violations == []


def test_deployment_scan_ignores_comments_but_catches_assignments(tmp_path):
    """守衛必須分得開「註解提到」與「真的宣告」,否則它只會製造噪音。

    `services/asr-gateway/app/config.py:91-106` 正是「註解裡出現退役名字」的
    真實案例;若這裡改成比對「出現過」,那份註解會讓守衛永遠紅。
    """
    name = sorted(REMOVED_ENV_NAMES)[0]

    yml = tmp_path / "probe.yml"
    yml.write_text(f"services:\n  x:\n    environment:\n      # {name}: retired\n", encoding="utf-8")
    assert _env_assignments(yml) == []
    yml.write_text(f"services:\n  x:\n    environment:\n      {name}: \"true\"\n", encoding="utf-8")
    assert _env_assignments(yml) == [(name, 4)]

    sh = tmp_path / "probe.sh"
    sh.write_text(f"#!/bin/sh\n# export {name}=1\n", encoding="utf-8")
    assert _env_assignments(sh) == []
    sh.write_text(f"#!/bin/sh\nexport {name}=1\n", encoding="utf-8")
    assert _env_assignments(sh) == [(name, 2)]


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
