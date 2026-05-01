"""Phase 0 boundary regression test — AgenticRAG must not hard-import anila-core.

Why: AgenticRAG is a fork-template for devs starting new agents. Hard
imports from anila-core would force every fork to install the
platform-internal package — breaking the fork promise. Phase 0
(2026-05-02) reclaimed local copies of pg_pool / pgvector_store / etc.
This test catches regressions where someone re-introduces an
``import anila_core`` outside the documented soft-fallback whitelist.

Whitelist:
  - ``api/middleware/loader.py`` — uses ``importlib.import_module`` to
    prefer anila-core's middleware when the platform happens to install
    it; falls back to the local ``csp_auth.py`` otherwise. Soft import
    only, no top-level ``from anila_core ...`` statement.
  - ``api/middleware/csp_auth.py`` — the local fallback itself, contains
    only docstring references.
  - Any file with anila_core mentions confined to comments / docstrings
    (provenance notes from the Phase 0 refactor).

Failure mode: any new ``from anila_core ...`` or ``import anila_core``
at module top level outside the whitelist fails this test.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest


_REPO_SRC = Path(__file__).resolve().parent.parent / "src" / "agentic_rag"
_REPO_ROOT_API = Path(__file__).resolve().parent.parent / "api.py"

# These regexes only match real import statements, not comments or
# string literals. Anchored to start-of-line (with optional indent).
_HARD_IMPORT_RE = re.compile(
    r"^\s*(?:from\s+anila_core\b|import\s+anila_core\b)",
    re.MULTILINE,
)


def _collect_offending_files() -> list[tuple[Path, list[str]]]:
    """Walk the AgenticRAG source tree, return (file, matching_lines)."""
    offenders: list[tuple[Path, list[str]]] = []
    files = list(_REPO_SRC.rglob("*.py"))
    if _REPO_ROOT_API.is_file():
        files.append(_REPO_ROOT_API)

    for path in files:
        text = path.read_text(encoding="utf-8")
        # Strip out comment-only lines and triple-quoted docstrings to
        # avoid false positives. We don't run the full Python parser
        # here because import-detection only needs line-level analysis.
        lines = text.splitlines()
        hits: list[str] = []
        in_triple = False
        triple_char: str | None = None
        for ln in lines:
            stripped = ln.strip()
            # Toggle triple-quoted string state.
            for q in ('"""', "'''"):
                if stripped.startswith(q) or stripped.endswith(q) or q in stripped:
                    if not in_triple and stripped.count(q) % 2 == 1:
                        in_triple = True
                        triple_char = q
                        break
                    if in_triple and triple_char == q:
                        in_triple = False
                        triple_char = None
                        break
            if in_triple:
                continue
            if stripped.startswith("#"):
                continue
            if _HARD_IMPORT_RE.match(ln):
                hits.append(ln)
        if hits:
            offenders.append((path, hits))
    return offenders


def test_no_hard_anila_core_imports() -> None:
    """No ``from anila_core ...`` or ``import anila_core`` anywhere.

    The middleware loader uses ``importlib.import_module`` (dynamic
    soft import) — that path won't match these regexes, which only
    flag literal ``from`` / ``import`` statements.
    """
    offenders = _collect_offending_files()
    if offenders:
        msg_parts = ["AgenticRAG must not hard-import anila-core. Found:"]
        for path, hits in offenders:
            msg_parts.append(f"\n  {path.relative_to(_REPO_SRC.parent.parent)}:")
            for h in hits:
                msg_parts.append(f"    {h}")
        msg_parts.append(
            "\n\nIf you genuinely need anila-core functionality, use the "
            "vector_store_override hook in app_factory.build_app(...) "
            "or follow the soft-import pattern in "
            "api/middleware/loader.py."
        )
        pytest.fail("\n".join(msg_parts))


def test_app_factory_imports_without_anila_core(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force ``import anila_core`` to fail; agentic_rag must still load.

    Simulates a dev who has ``pip install agentic-rag[rag]`` but does
    NOT have anila-core on their PYTHONPATH.
    """
    # Block any future ``import anila_core ...`` by setting it to None
    # in sys.modules (Python's import system treats ``None`` as
    # "previously failed; re-raise ImportError").
    monkeypatch.setitem(sys.modules, "anila_core", None)

    # Drop any agentic_rag.* modules that may have been loaded by other
    # tests so we re-import freshly. Important: we want to prove that
    # *importing the package from cold* succeeds without anila-core.
    for mod_name in list(sys.modules):
        if mod_name.startswith("agentic_rag"):
            monkeypatch.delitem(sys.modules, mod_name, raising=False)

    # The minimal smoke: importing app_factory must not raise. We don't
    # try to actually run the FastAPI app here — that would require a
    # real database. Just prove the module graph loads.
    import agentic_rag.app_factory  # noqa: F401

    # Spot-check: the local stores / models are reachable.
    from agentic_rag.models.ingestion import IngestionChunk, SearchHit  # noqa: F401
    from agentic_rag.storage.adapters import (  # noqa: F401
        CollectionScopedPgVectorStore,
        PgPool,
    )
    from agentic_rag.storage.protocols import VectorStore  # noqa: F401
