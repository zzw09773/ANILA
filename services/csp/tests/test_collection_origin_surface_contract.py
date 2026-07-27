# -*- coding: utf-8 -*-
"""Contract: every collection-id resolver call decides ``origin=`` explicitly.

Replaces the old prefix-scanning A3 test. That scan only saw routes under
``/api/ingestion`` and ``/api/personal``, so by construction it could not
catch a hole mounted outside those prefixes (``/api/conversations``,
``/api/tasks``, …).

This test walks ``app/**/*.py`` AST for calls to
``lookup_collection_for_surface`` / ``_require_collection_access`` and
fails when ``origin=`` is omitted. A new caller cannot compile a silent
cross-product leak past CI.

⚠ ``origin`` filtering is inventory partitioning, not authz — see
``app.api.ingestion.surface``.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parents[1] / "app"

_RESOLVER_NAMES = frozenset(
    {
        "lookup_collection_for_surface",
        "_require_collection_access",
        "_require_collection_clearance",
    }
)


def _py_files() -> list[Path]:
    return sorted(p for p in APP_ROOT.rglob("*.py") if p.is_file())


def _call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _calls_missing_origin(path: Path) -> list[str]:
    """Return human-readable locations of resolver calls without origin=."""
    src = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src, filename=str(path))
    except SyntaxError:
        return [f"{path}: unparseable"]

    failures: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node.func)
        if name not in _RESOLVER_NAMES:
            continue
        has_origin = any(
            isinstance(kw, ast.keyword) and kw.arg == "origin"
            for kw in node.keywords
        )
        if not has_origin:
            failures.append(
                f"{path.relative_to(APP_ROOT.parent)}:{node.lineno} "
                f"{name}(...) missing required origin="
            )
    return failures


def test_every_collection_resolver_call_passes_origin():
    failures: list[str] = []
    for path in _py_files():
        failures.extend(_calls_missing_origin(path))
    assert not failures, (
        "collection resolvers require an explicit origin= decision "
        "(SURFACE_CSP / SURFACE_ANILALM / ANY_SURFACE). Omitting it is a "
        "TypeError at runtime and a silent cross-product leak in the old "
        "ambient design:\n  " + "\n  ".join(failures)
    )


def test_lookup_helper_has_no_ambient_fallback():
    """Resolver must not read ContextVar / None as 'all products'."""
    from app.api.ingestion import collections as coll_mod

    src = Path(coll_mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(
        n
        for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "lookup_collection_for_surface"
    )
    body = ast.get_source_segment(src, fn) or ""
    assert "ANY_SURFACE" in body
    assert "get_surface_origin" not in body
    # origin parameter must have no default (required keyword).
    origin_arg = next(a for a in fn.args.kwonlyargs if a.arg == "origin")
    defaults = fn.args.kw_defaults
    # kw_defaults aligns with kwonlyargs; None means no default.
    idx = fn.args.kwonlyargs.index(origin_arg)
    assert defaults[idx] is None, "origin must be a required keyword"


def test_g4_selftest_missing_origin_is_detected(tmp_path, monkeypatch):
    """Red-before proof: a call without origin= fails this contract."""
    probe = APP_ROOT / "_origin_decision_probe_do_not_commit.py"
    assert not probe.exists(), "probe file leaked from a prior run"
    probe.write_text(
        "def _bad(db, user, collection_id):\n"
        "    from app.api.ingestion.collections import _require_collection_access\n"
        "    return _require_collection_access(db, user, collection_id)\n",
        encoding="utf-8",
    )
    try:
        failures = _calls_missing_origin(probe)
        assert failures, "expected the probe call without origin= to be flagged"
        assert "missing required origin=" in failures[0]
    finally:
        probe.unlink(missing_ok=True)
