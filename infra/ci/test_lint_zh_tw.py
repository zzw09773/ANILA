"""lint_zh_tw 回歸測試 — 路徑豁免必須跨平台。

Windows 上 ``path.relative_to(root)`` 轉字串是反斜線分隔;豁免比對若未
正規化為 POSIX 分隔,node_modules/dist 等會整批漏豁免,本機 lint 產生
數百筆假命中(Linux CI 不受影響)。Windows 案例以純字串模擬,Linux 上
亦可驗證,不需真的在 Windows 執行。

用法:python -m pytest infra/ci/test_lint_zh_tw.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lint_zh_tw import is_exempt, scan_file  # noqa: E402


@pytest.mark.parametrize("rel", [
    # Windows 反斜線(回歸案例:修正前全數漏豁免)
    "csp-governance-ui\\node_modules\\zrender\\src\\zrender.ts",
    "web\\dist\\assets\\index.js",
    "tools\\.venv\\lib\\site-packages\\pkg\\mod.py",
    "src\\tests\\unit\\foo.spec.ts",
    # POSIX 正斜線(Linux CI 原本就豁免,不得回歸)
    "csp-governance-ui/node_modules/zrender/src/zrender.ts",
    "web/dist/assets/index.js",
    "tools/.venv/lib/site-packages/pkg/mod.py",
    "src/tests/unit/foo.spec.ts",
])
def test_exempt_paths_match_on_both_separators(rel: str) -> None:
    assert is_exempt(rel)


@pytest.mark.parametrize("rel", [
    "csp-governance-ui\\src\\views\\GovernanceView.vue",
    "csp-governance-ui/src/views/GovernanceView.vue",
    "app\\api\\routes\\chat.py",
])
def test_non_exempt_paths_still_scanned(rel: str) -> None:
    assert not is_exempt(rel)


def test_scan_file_exempts_vendored_file_on_native_paths(tmp_path: Path) -> None:
    vendored = tmp_path / "node_modules" / "zrender" / "util.js"
    vendored.parent.mkdir(parents=True)
    vendored.write_text('const label = "視頻";\n', encoding="utf-8")
    assert scan_file(vendored, tmp_path) == []


def test_scan_file_still_flags_mainland_term_in_source(tmp_path: Path) -> None:
    src = tmp_path / "src" / "views" / "Hello.vue"
    src.parent.mkdir(parents=True)
    src.write_text("<template><span>視頻</span></template>\n", encoding="utf-8")
    hits = scan_file(src, tmp_path)
    assert [lineno for lineno, _ in hits] == [1]


def test_scan_file_still_flags_simplified_char_in_source(tmp_path: Path) -> None:
    src = tmp_path / "src" / "Hello.ts"
    src.parent.mkdir(parents=True)
    # 簡體「這」以 code point 組出,原始碼不含簡體字面(repo 政策)
    src.write_text(f'const t = "{chr(0x8FD9)}";\n', encoding="utf-8")
    assert scan_file(src, tmp_path)
