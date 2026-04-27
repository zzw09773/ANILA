"""Sprint 1 Gate G3 — single retrieval entry point.

Per docs/ingestion-platform-design.md §9 G3:

    grep -rnE "(FROM|INSERT INTO|UPDATE|DELETE FROM|...) document_chunks"
    --include="*.py" anila-core AgenticRAG ingestion-worker
    | grep -v "_archive|tests"
    → exactly 1 file (the canonical SDK).

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
import subprocess
from pathlib import Path

import pytest


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
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCAN_DIRS = [
    _REPO_ROOT / "anila-core" / "src",
    _REPO_ROOT / "AgenticRAG" / "src",
    _REPO_ROOT / "AgenticRAG" / "api.py",
    _REPO_ROOT / "ingestion-worker" / "src",
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


def test_g3_single_sql_entry_point() -> None:
    """The only file with actual SQL on ``document_chunks`` is the
    central ``AgentScopedPgVectorStore``.

    The test is robust against ordering / new files: it asserts
    ``offenders`` is exactly ``{anila-core/.../pgvector_store.py}``.
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

    canonical = _REPO_ROOT / "anila-core" / "src" / "anila_core" / "storage" / "adapters" / "pgvector_store.py"
    canonical_resolved = canonical.resolve()

    extras = sorted(
        p.relative_to(_REPO_ROOT) for p in offenders if p.resolve() != canonical_resolved
    )
    assert canonical_resolved in {p.resolve() for p in offenders}, (
        f"G3 anomaly: the canonical SDK file {canonical_resolved} has no "
        f"document_chunks SQL. The single-entry-point invariant only holds "
        f"if that file actually IS the entry point."
    )
    assert not extras, (
        f"G3 BREACH: {len(extras)} file(s) outside the central SDK now "
        f"contain SQL touching document_chunks:\n"
        + "\n".join(f"  - {p}" for p in extras)
        + "\nAll retrieval / index / delete operations on the chunks table "
          "must flow through anila_core.storage.adapters.AgentScopedPgVectorStore."
    )


def test_g3_design_doc_grep_form() -> None:
    """Run the design-doc grep verbatim and parse the output.

    Slightly different from the SQL-pattern test above: this matches
    *any* mention of ``document_chunks``, including docstrings and
    type-hint strings. The expected count is small but >1 — comments
    in module docstrings, ``IngestionChunk`` model docstring, etc.
    The test asserts the design-doc literal grep doesn't *grow* — a
    new mention triggers manual review.
    """
    cmd = [
        "grep",
        "-rn",
        "document_chunks",
        "--include=*.py",
        "anila-core",
        "AgenticRAG",
        "ingestion-worker",
    ]
    result = subprocess.run(
        cmd,
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode not in (0, 1):
        # 0 = matches found, 1 = no matches; anything else is a grep error.
        pytest.fail(f"grep failed: {result.stderr}")

    files = set()
    for line in result.stdout.splitlines():
        path = line.split(":", 1)[0]
        if any(frag.strip("/") in path for frag in _SKIP_FRAGMENTS):
            continue
        files.add(path)

    # Loose ceiling: 12 files. As of Chunk F we're at 7 (mostly docstring
    # / settings string mentions). Bumping past 12 means someone added
    # a substantial new file referring to the table — review and either
    # update the ceiling or refactor.
    assert len(files) <= 12, (
        f"G3 advisory: {len(files)} files mention document_chunks "
        f"({sorted(files)}). Review whether this is justified or a "
        f"new SQL caller leaking through."
    )
