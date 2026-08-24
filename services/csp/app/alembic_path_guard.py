"""Refuse alembic when ``anila_core`` resolved from outside this repo.

Borrowed venvs ship an old-tree ``anila-core`` that imports ``anila_security``.
This tree has no such package. If site-packages wins, ``alembic upgrade``
dies around revision 0027 with ``ModuleNotFoundError: anila_security`` —
a symptom that does not name the cause or the fix.

Call this from ``migrations/env.py`` before any revision runs.
"""
from __future__ import annotations

import os
from pathlib import Path


_FIX = (
    "Borrowed venv ships old-tree anila-core (depends on anila_security); "
    "this tree has no anila_security. Put this tree's packages/anila-core/src "
    "BEFORE site-packages: "
    'export PYTHONPATH="$CSP:$ROOT/packages/anila-core/src${PYTHONPATH:+:$PYTHONPATH}"'
)


def repo_root_from_csp_app(app_file: str | None = None) -> Path:
    """``services/csp/app/…`` → repo root (three parents up from ``app/``)."""
    here = Path(app_file or __file__).resolve()
    # …/services/csp/app/alembic_path_guard.py → repo
    return here.parents[3]


def expected_anila_core_src(repo_root: Path | None = None) -> Path:
    root = repo_root or repo_root_from_csp_app()
    return (root / "packages" / "anila-core" / "src").resolve()


def refuse_foreign_anila_core(
    loaded_file: str | None = None,
    *,
    repo_root: Path | None = None,
) -> None:
    """Raise if the resolved ``anila_core`` is not this tree's copy.

    Uses ``find_spec`` so a foreign package that imports ``anila_security``
    is caught *before* that import runs. ``loaded_file`` is for tests.
    """
    expected = expected_anila_core_src(repo_root)
    if loaded_file is None:
        import importlib.util

        spec = importlib.util.find_spec("anila_core")
        if spec is None:
            return
        loaded_file = spec.origin or (spec.submodule_search_locations or [None])[0]
    if not loaded_file:
        return
    loaded = Path(loaded_file).resolve()
    try:
        loaded.relative_to(expected)
    except ValueError:
        raise RuntimeError(
            "alembic resolved anila_core from outside this repo "
            f"({loaded}). {_FIX}"
        ) from None
