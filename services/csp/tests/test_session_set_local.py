"""Runtime SQL must not leave a session-level SET for the next PgBouncer client.

Transaction pooling gives the next request a different server connection,
except when the previous transaction used a plain SET. SET LOCAL and
set_config(..., true) end at COMMIT and are safe.
"""

from pathlib import Path

ROOTS = (
    Path(__file__).resolve().parents[1] / "app",
    Path(__file__).resolve().parents[3] / "services" / "ingestion-worker" / "src",
    Path(__file__).resolve().parents[3] / "packages" / "anila-core" / "src",
)


def _scan(path: Path) -> list[str]:
    hits: list[str] = []
    text = path.read_text(encoding="utf-8", errors="replace")
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        lowered = stripped.lower()
        if "set local" in lowered:
            continue
        if "set_config(" in lowered and ", true" in lowered:
            continue
        if "set_config(" in lowered and ", false" in lowered:
            hits.append(f"{path}:{lineno}: {stripped}")
            continue
        if " execute(" in lowered or lowered.startswith("execute(") or "text(" in lowered:
            if " set " in f" {lowered}" and "set_config" not in lowered and "offset " not in lowered:
                if any(token in lowered for token in ("set anila.", "set session ", " set role", "set search_path")):
                    hits.append(f"{path}:{lineno}: {stripped}")
    return hits


def test_runtime_sql_uses_transaction_scoped_settings():
    hits: list[str] = []
    for root in ROOTS:
        for path in root.rglob("*.py"):
            if "migrations" in path.parts or "tests" in path.parts:
                continue
            hits.extend(_scan(path))
    assert hits == []
