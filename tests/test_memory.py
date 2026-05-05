"""Memory store + recall tests. No LLM required — uses the keyword-overlap fallback."""

from __future__ import annotations

from pathlib import Path

import pytest

from anila_agent.memory.long_term import LongTermMemory
from anila_agent.memory.store import ENTRYPOINT_NAME, MemdirStore
from anila_agent.models.schemas import MemoryFrontmatter


@pytest.mark.unit
def test_write_and_read_roundtrip(memory_dir: Path) -> None:
    store = MemdirStore(memory_dir)
    fm = MemoryFrontmatter(name="user role", description="user is a data scientist", type="user")
    store.write("user_role.md", fm, "User is investigating logging.")

    parsed_fm, body = store.read("user_role.md")
    assert parsed_fm == fm
    assert "investigating logging" in body


@pytest.mark.unit
def test_index_truncation_line_cap(memory_dir: Path) -> None:
    store = MemdirStore(memory_dir, max_index_lines=3)
    raw = "line1\nline2\nline3\nline4\nline5"
    truncated = store.truncate_index(raw)
    assert truncated.line_truncated is True
    assert truncated.content.split("\n", 3)[:3] == ["line1", "line2", "line3"]


@pytest.mark.unit
def test_scan_returns_newest_first(memory_dir: Path) -> None:
    import time

    store = MemdirStore(memory_dir)
    store.write(
        "older.md",
        MemoryFrontmatter(name="older", description="old fact", type="project"),
        "old",
    )
    time.sleep(0.01)
    store.write(
        "newer.md",
        MemoryFrontmatter(name="newer", description="new fact", type="project"),
        "new",
    )
    headers = store.scan()
    assert [h.filename for h in headers[:2]] == ["newer.md", "older.md"]


@pytest.mark.unit
def test_index_skipped_in_scan(memory_dir: Path) -> None:
    store = MemdirStore(memory_dir)
    (memory_dir / ENTRYPOINT_NAME).write_text("- some entry", encoding="utf-8")
    store.write(
        "topic.md",
        MemoryFrontmatter(name="topic", description="topic fact", type="reference"),
        "body",
    )
    files = [h.filename for h in store.scan()]
    assert ENTRYPOINT_NAME not in files
    assert "topic.md" in files


@pytest.mark.unit
def test_append_index_dedup(memory_dir: Path) -> None:
    store = MemdirStore(memory_dir)
    store.append_index_entry("- [User role](user_role.md) — user is a data scientist")
    store.append_index_entry("- [User role](user_role.md) — user is a data scientist")
    content = store.read_index().strip().splitlines()
    assert len(content) == 1


@pytest.mark.unit
async def test_recall_fallback_uses_keyword_overlap(memory_dir: Path) -> None:
    store = MemdirStore(memory_dir)
    store.write(
        "user_role.md",
        MemoryFrontmatter(
            name="user role",
            description="user is a data scientist focused on logging",
            type="user",
        ),
        "body",
    )
    store.write(
        "project_release.md",
        MemoryFrontmatter(
            name="release",
            description="freeze begins next thursday",
            type="project",
        ),
        "body",
    )
    memory = LongTermMemory(store, model=None)
    recalled = await memory.recall("what is the user's role?", k=2)
    assert any(r.filename == "user_role.md" for r in recalled)


@pytest.mark.unit
def test_write_rejects_path_traversal(memory_dir: Path) -> None:
    store = MemdirStore(memory_dir)
    fm = MemoryFrontmatter(name="x", description="x", type="user")
    with pytest.raises(ValueError):
        store.write("../escape.md", fm, "no")
