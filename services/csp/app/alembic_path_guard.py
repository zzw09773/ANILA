"""Refuse alembic when ``anila_core`` resolved from outside this *source* tree.

Borrowed venvs ship an old-tree ``anila-core`` that imports ``anila_security``.
This tree has no such package. If site-packages wins, ``alembic upgrade``
dies around revision 0027 with ``ModuleNotFoundError: anila_security`` —
a symptom that does not name the cause or the fix.

The shipping image has no repo: ``anila-core`` is installed into
site-packages (``csp.Dockerfile`` ``pip install /tmp/anila-core``) and
this file lives at ``/app/app/…``. Walking a fixed number of parents
to find a repo root is a layout assumption — it IndexError'd in the
image and alembic never ran.

Rule: look for ``packages/anila-core/src`` by walking *up*. Found →
this is a source checkout, enforce that imported ``anila_core`` lives
there. Not found → image / installed layout; skip the check and say
why (INFO, not silent).
"""
from __future__ import annotations

import logging
from pathlib import Path


logger = logging.getLogger(__name__)

_FIX = (
    "Borrowed venv ships old-tree anila-core (depends on anila_security); "
    "this tree has no anila_security. Put this tree's packages/anila-core/src "
    "BEFORE site-packages: "
    'export PYTHONPATH="$CSP:$ROOT/packages/anila-core/src${PYTHONPATH:+:$PYTHONPATH}"'
)

# Grep-able. Distinct from "the guard never ran".
_SKIP_INFO = (
    "skipping source-tree anila_core check: no packages/anila-core/src "
    "above this file (image or installed layout; anila-core comes from "
    "site-packages)"
)


def source_tree_anila_core_src(start: Path | None = None) -> Path | None:
    """Walk up from ``start`` (default: this file) looking for the checkout.

    Returns the ``packages/anila-core/src`` directory, or ``None`` when
    this is not a source tree. Criterion is *what is found*, not how
    many parents the path has.
    """
    here = (start or Path(__file__)).resolve()
    for parent in (here, *here.parents):
        candidate = parent / "packages" / "anila-core" / "src"
        if (candidate / "anila_core").is_dir():
            return candidate.resolve()
    return None


def repo_root_from_csp_app(app_file: str | None = None) -> Path | None:
    """Source-tree repo root, or ``None`` in an image / installed layout."""
    src = source_tree_anila_core_src(Path(app_file) if app_file else None)
    if src is None:
        return None
    # packages/anila-core/src → repo
    return src.parent.parent.parent


def expected_anila_core_src(repo_root: Path | None = None) -> Path | None:
    if repo_root is not None:
        candidate = (repo_root / "packages" / "anila-core" / "src").resolve()
        if (candidate / "anila_core").is_dir():
            return candidate
        return None
    return source_tree_anila_core_src()


def refuse_foreign_anila_core(
    loaded_file: str | None = None,
    *,
    repo_root: Path | None = None,
    start: Path | None = None,
) -> None:
    """Raise if a *source tree* resolved ``anila_core`` from outside itself.

    Uses ``find_spec`` so a foreign package that imports ``anila_security``
    is caught *before* that import runs. ``loaded_file`` / ``start`` are
    for tests. Image layout: no ``packages/anila-core/src`` above us →
    log why and return. Never IndexError on a short path.
    """
    expected = (
        expected_anila_core_src(repo_root)
        if repo_root is not None
        else source_tree_anila_core_src(start)
    )
    if expected is None:
        logger.info(_SKIP_INFO)
        return
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
