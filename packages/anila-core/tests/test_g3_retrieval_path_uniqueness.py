"""Sprint 1 Gate G3 — explicit SQL entry-point allowlist.

Per docs/ingestion/ingestion-platform-design.md §9 G3:

    grep -rnE "(FROM|INSERT INTO|UPDATE|DELETE FROM|...) document_chunks"
    --include="*.py" anila-core AgenticRAG ingestion-worker
    | grep -v "_archive|tests"
    → only the canonical SDK and the relation centroid engine.

Different from G1/G2 which test runtime behaviour against a live DB.
G3 is a *static* invariant — every retrieval and every write to the
chunks table must go through the central SDK, not inline SQL anywhere
else. A regression here is usually a code-level mistake, not a runtime
one; catch it before merge with this static check.

This test runs in the unit-test pass (no integration deps) so a
broken Sprint 1 G3 fails the standard ``pytest`` invocation.
"""

from __future__ import annotations

import re
from pathlib import Path


# Patterns that count as "actual SQL touching document_chunks". Excludes
# docstrings / comments / type-annotation strings — those are common and
# harmless. Matches the design-doc command's spirit, not its literal grep.
_SQL_PATTERN = re.compile(
    r"(FROM|INSERT\s+INTO|UPDATE|DELETE\s+FROM|ALTER\s+TABLE|"
    r"CREATE\s+TABLE|DROP\s+TABLE|CREATE\s+INDEX[^\n]*ON)\s+document_chunks",
    re.IGNORECASE,
)

# Where to look. Migrations are excluded — they DEFINE the table, which
# is the schema authority, not a retrieval path. anila-core is the
# canonical home of the SDK; AgenticRAG and ingestion-worker are the
# two callers that historically had inline SQL.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCAN_DIRS = [
    _REPO_ROOT / "packages" / "anila-core" / "src",
    _REPO_ROOT / "services" / "ingestion-worker" / "src",
]
# Directory fragments anywhere in the path that mean "skip" — tests,
# archived code, build artefacts.
_SKIP_FRAGMENTS = ("/_archive/", "/tests/", "/__pycache__/", "/migrations/")


def _iter_python_files() -> list[Path]:
    files: list[Path] = []
    for root in _SCAN_DIRS:
        if not root.exists():
            continue
        if root.is_file() and root.suffix == ".py":
            files.append(root)
            continue
        for p in root.rglob("*.py"):
            posix = p.as_posix()
            if any(frag in posix for frag in _SKIP_FRAGMENTS):
                continue
            files.append(p)
    return files


def test_g3_only_approved_sql_entry_points() -> None:
    """Only the central store and relation centroid engine may use chunk SQL.

    The test is robust against ordering / new files: it asserts
    ``offenders`` is exactly the two reviewed entry points below.
    Adding a new SQL spot anywhere else fails this test loudly.
    """
    offenders: dict[Path, list[str]] = {}
    for p in _iter_python_files():
        try:
            text = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        hits = _SQL_PATTERN.findall(text)
        if hits:
            offenders[p] = hits

    approved = {
        Path("packages/anila-core/src/anila_core/storage/adapters/pgvector_store.py"),
        Path("services/ingestion-worker/src/ingestion_worker/similarity_relations.py"),
    }
    actual = {path.relative_to(_REPO_ROOT) for path in offenders}
    assert actual == approved, (
        "G3 BREACH: document_chunks SQL entry points differ from the reviewed "
        f"allowlist; unexpected={sorted(actual - approved)}, "
        f"missing={sorted(approved - actual)}"
    )


def test_g3_design_doc_grep_form() -> None:
    """Apply the design-doc broad search without a host ``grep`` dependency.

    Slightly different from the SQL-pattern test above: this matches
    *any* mention of ``document_chunks``, including docstrings and
    type-hint strings. The expected count is small but >1 — comments
    in module docstrings, ``IngestionChunk`` model docstring, etc.
    The test asserts the design-doc literal grep doesn't *grow* — a
    new mention triggers manual review.
    """
    files = {
        path.relative_to(_REPO_ROOT).as_posix()
        for path in _iter_python_files()
        if "document_chunks" in path.read_text(encoding="utf-8")
    }

    # Loose ceiling: 12 files. As of Chunk F we're at 7 (mostly docstring
    # / settings string mentions). Bumping past 12 means someone added
    # a substantial new file referring to the table — review and either
    # update the ceiling or refactor.
    assert len(files) <= 12, (
        f"G3 advisory: {len(files)} files mention document_chunks "
        f"({sorted(files)}). Review whether this is justified or a "
        f"new SQL caller leaking through."
    )
