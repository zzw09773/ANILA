"""Spreadsheet formula neutralization — helper, twin lock, producer scan.

Acceptance locked here:
- six trigger chars each assert the output's first character is ``'``
- dropping neutralization (identity helper / new un-neutralized writer)
  makes the relevant test red
- producer coverage is a scan of ``csv.writer(`` / ``media_type="text/csv"``
  / ``Workbook()``, not a hand-written file list
- constructor granularity is the enclosing function/module (AST Call),
  not a whole-file substring of the helper name
"""
from __future__ import annotations

import ast
import os
import re
from pathlib import Path

import pytest

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

from app.utils.csv_formula import FORMULA_TRIGGER_PREFIXES, csv_formula_safe


_SERVICE_ROOT = Path(__file__).resolve().parents[1]
_APP_ROOT = _SERVICE_ROOT / "app"
_TWIN = _SERVICE_ROOT.parent / "anila-studio" / "app" / "services" / "csv_formula.py"

_PRODUCER_PATTERNS = (
    re.compile(r"csv\.writer\s*\("),
    re.compile(r"""media_type\s*=\s*["']text/csv"""),
    re.compile(r"Workbook\s*\("),
)
_NEUTRALIZER_NAMES = ("csv_formula_safe", "_csv_safe")
_CLOSED_DOMAIN_MARKER = "CSV_FORMULA_INJECTION_CLOSED_DOMAIN"


# ── helper behaviour ───────────────────────────────────────────────────────


@pytest.mark.parametrize("prefix", FORMULA_TRIGGER_PREFIXES)
def test_each_trigger_char_is_prefixed_with_apostrophe(prefix: str):
    payload = f"{prefix}1+1"
    out = csv_formula_safe(payload)
    assert out[:1] == "'", f"first char of {out!r} must be apostrophe"
    assert out[1:] == payload


def test_plain_text_and_none_are_unchanged():
    assert csv_formula_safe("hello") == "hello"
    assert csv_formula_safe("1+1") == "1+1"
    assert csv_formula_safe(None) == ""
    assert csv_formula_safe(12) == "12"


def test_already_prefixed_value_is_not_double_wrapped():
    assert csv_formula_safe("'=1+1") == "'=1+1"


def test_trigger_set_is_exactly_the_owasp_closed_six():
    assert FORMULA_TRIGGER_PREFIXES == ("=", "+", "-", "@", "\t", "\r")


# ── twin lock ──────────────────────────────────────────────────────────────


def _function_logic_dump(path: Path, name: str) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            body = list(node.body)
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
            ):
                body = body[1:]
            return ast.dump(ast.Module(body=body, type_ignores=[]))
    raise AssertionError(f"{path} has no function {name}")


def _assign_dump(path: Path, name: str) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return ast.dump(node.value)
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == name
            and node.value is not None
        ):
            return ast.dump(node.value)
    raise AssertionError(f"{path} has no assignment {name}")


def test_twin_implementations_have_identical_logic():
    """Two copies by ruling; the bodies must not silently drift."""
    local = _APP_ROOT / "utils" / "csv_formula.py"
    assert _TWIN.is_file(), f"missing twin {_TWIN}"
    assert _function_logic_dump(local, "csv_formula_safe") == _function_logic_dump(
        _TWIN, "csv_formula_safe"
    )
    assert _assign_dump(local, "FORMULA_TRIGGER_PREFIXES") == _assign_dump(
        _TWIN, "FORMULA_TRIGGER_PREFIXES"
    )


def test_twin_docstrings_name_the_other_copy():
    local = (_APP_ROOT / "utils" / "csv_formula.py").read_text(encoding="utf-8")
    twin = _TWIN.read_text(encoding="utf-8")
    assert "services/anila-studio/app/services/csv_formula.py:26" in local
    assert "services/csp/app/utils/csv_formula.py:26" in twin
    assert "這兩份會漂開，改一邊要改另一邊" in local
    assert "這兩份會漂開，改一邊要改另一邊" in twin


# ── producer coverage scan (not a file list) ───────────────────────────────


def iter_spreadsheet_producers(root: Path) -> list[Path]:
    hits: list[Path] = []
    for path in sorted(root.rglob("*.py")):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        if any(pat.search(text) for pat in _PRODUCER_PATTERNS):
            hits.append(path)
    return hits


def _imported_app_paths(path: Path, service_root: Path) -> list[Path]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[Path] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        if not node.module.startswith("app"):
            continue
        rel = Path(*node.module.split("."))
        py = service_root / rel.with_suffix(".py")
        init = service_root / rel / "__init__.py"
        if py.is_file():
            found.append(py)
        elif init.is_file():
            found.append(init)
    return found


def _callee_parts(call: ast.Call) -> tuple[str, ...]:
    parts: list[str] = []
    node = call.func
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return tuple(reversed(parts))


def _is_spreadsheet_ctor(call: ast.Call) -> bool:
    parts = _callee_parts(call)
    if parts[-2:] == ("csv", "writer"):
        return True
    return bool(parts) and parts[-1] == "Workbook"


def _is_neutralizer_call(call: ast.Call) -> bool:
    parts = _callee_parts(call)
    return bool(parts) and parts[-1] in _NEUTRALIZER_NAMES


def _direct_calls(scope: ast.AST) -> list[ast.Call]:
    """Calls in ``scope`` itself, not in nested functions or classes."""
    calls: list[ast.Call] = []
    for child in ast.iter_child_nodes(scope):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for node in ast.walk(child):
            if isinstance(node, ast.Call):
                calls.append(node)
    return calls


def _iter_scopes(tree: ast.AST):
    yield tree
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _any_scope_has_unneutralized_ctor(tree: ast.AST) -> bool:
    for scope in _iter_scopes(tree):
        calls = _direct_calls(scope)
        if any(_is_spreadsheet_ctor(c) for c in calls) and not any(
            _is_neutralizer_call(c) for c in calls
        ):
            return True
    return False


def _tree_has_spreadsheet_ctor(tree: ast.AST) -> bool:
    return any(
        _is_spreadsheet_ctor(c) for scope in _iter_scopes(tree) for c in _direct_calls(scope)
    )


def producer_is_neutralized(path: Path, service_root: Path) -> bool:
    """Per constructor-scope, not whole-file substring.

    A ``csv.writer`` / ``Workbook`` Call is green only if its enclosing
    function (or the module body) also *calls* ``csv_formula_safe`` or
    ``_csv_safe``. Importing the helper, mentioning it in a TODO, or
    neutralizing a sibling function does not cover a second constructor.

    Remaining blind spot (not value-tracing): one helper call greens every
    ``writerow`` / cell assignment in that same scope. Closed-domain
    marker is still a whole-file exemption. ``media_type="text/csv"``
    files with no constructor still pass by importing a neutralized
    writer module.
    """
    text = path.read_text(encoding="utf-8")
    if _CLOSED_DOMAIN_MARKER in text:
        return True
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError:
        return False
    if _tree_has_spreadsheet_ctor(tree):
        return not _any_scope_has_unneutralized_ctor(tree)
    for imported in _imported_app_paths(path, service_root):
        imported_text = imported.read_text(encoding="utf-8")
        if not (
            re.search(r"csv\.writer\s*\(", imported_text)
            or re.search(r"Workbook\s*\(", imported_text)
            or _CLOSED_DOMAIN_MARKER in imported_text
        ):
            continue
        if producer_is_neutralized(imported, service_root):
            return True
    return False


def test_every_csp_spreadsheet_producer_is_neutralized_or_closed_domain():
    offenders = [
        p
        for p in iter_spreadsheet_producers(_APP_ROOT)
        if not producer_is_neutralized(p, _SERVICE_ROOT)
    ]
    assert offenders == [], (
        "spreadsheet producer without neutralization or closed-domain "
        f"marker: {offenders}"
    )


def test_scan_finds_known_shapes_without_a_file_list():
    """If the needles stop matching, the coverage guard is blind."""
    hits = {p.name for p in iter_spreadsheet_producers(_APP_ROOT)}
    assert "feedback.py" in hits
    assert "usage_service.py" in hits
    assert "audit_logs.py" in hits
    assert "classification_inventory.py" in hits
    assert "usage.py" in hits


@pytest.mark.parametrize(
    "source",
    [
        "import csv, io\nw = csv.writer(io.StringIO())\nw.writerow(['=1+1'])\n",
        "from fastapi.responses import StreamingResponse\n"
        "def dump():\n"
        "    return StreamingResponse(iter(['=1+1']), media_type='text/csv')\n",
        "from openpyxl import Workbook\nwb = Workbook()\nwb.active['A1'] = '=1+1'\n",
    ],
)
def test_guard_goes_red_for_a_new_unneutralized_producer(tmp_path: Path, source: str):
    evil = tmp_path / "new_unneutralized_producer.py"
    evil.write_text(source, encoding="utf-8")
    producers = iter_spreadsheet_producers(tmp_path)
    assert evil in producers
    assert producer_is_neutralized(evil, tmp_path) is False


def test_guard_accepts_a_producer_that_calls_the_helper(tmp_path: Path):
    ok = tmp_path / "ok_export.py"
    ok.write_text(
        "import csv, io\n"
        "from app.utils.csv_formula import csv_formula_safe\n"
        "w = csv.writer(io.StringIO())\n"
        "w.writerow([csv_formula_safe('=1+1')])\n",
        encoding="utf-8",
    )
    assert producer_is_neutralized(ok, tmp_path) is True


def test_guard_goes_red_for_a_second_bare_writer_in_a_file_that_imports_the_helper(
    tmp_path: Path,
):
    """Reviewer scenario: feedback.py already imports the helper; a second
    function with a bare csv.writer must still fail the guard."""
    evil = tmp_path / "feedback_like.py"
    evil.write_text(
        "import csv, io\n"
        "from app.utils.csv_formula import csv_formula_safe\n"
        "\n"
        "def _items_to_csv(items):\n"
        "    w = csv.writer(io.StringIO())\n"
        "    w.writerow([csv_formula_safe(x) for x in items])\n"
        "\n"
        "def export_feedback_raw_v2():\n"
        "    w = csv.writer(io.StringIO())\n"
        "    w.writerow(['=1+1'])\n",
        encoding="utf-8",
    )
    assert evil in iter_spreadsheet_producers(tmp_path)
    assert producer_is_neutralized(evil, tmp_path) is False


def test_guard_does_not_treat_helper_name_in_a_comment_as_neutralization(
    tmp_path: Path,
):
    evil = tmp_path / "todo_only.py"
    evil.write_text(
        "# TODO: remember csv_formula_safe\n"
        "import csv, io\n"
        "w = csv.writer(io.StringIO())\n"
        "w.writerow(['=1+1'])\n",
        encoding="utf-8",
    )
    assert producer_is_neutralized(evil, tmp_path) is False
