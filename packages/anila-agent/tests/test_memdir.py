"""memdir store：CRUD、frontmatter、路徑安全、索引上限。"""

from __future__ import annotations

import pytest

from anila_agent.memory.memdir import (
    Memory,
    MemoryStore,
    validate_memory_dir,
    validate_name,
)

pytestmark = pytest.mark.unit


def test_write_read_roundtrip(tmp_path):
    store = MemoryStore(root=tmp_path)
    store.write(Memory(name="user-role", description="使用者是後端工程師", type="user", body="喜歡繁中"))
    got = store.read("user-role")
    assert got is not None
    assert got.name == "user-role" and got.type == "user"
    assert "喜歡繁中" in got.body


def test_to_markdown_has_frontmatter():
    md = Memory(name="x-y", description="d", type="project", body="b").to_markdown()
    assert md.startswith("---\n") and "type: project" in md and "name: x-y" in md


def test_validate_name_rejects_traversal():
    for bad in ["../etc", "a/b", "a b", "UPPER", "", "x.md"]:
        with pytest.raises(ValueError):
            validate_name(bad)
    assert validate_name("good-name_2") == "good-name_2"


def test_validate_memory_dir_rejects_suspicious():
    for bad in ["", "~", "/", "\\\\unc\\share", "a\x00b"]:
        with pytest.raises(ValueError):
            validate_memory_dir(bad)


def test_manifest_and_list(tmp_path):
    store = MemoryStore(root=tmp_path)
    store.write(Memory(name="a-one", description="第一", type="project"))
    store.write(Memory(name="b-two", description="第二", type="feedback"))
    manifest = store.manifest()
    assert manifest == {"a-one": "第一", "b-two": "第二"}
    assert len(store.list()) == 2


def test_index_rebuilt_on_write(tmp_path):
    store = MemoryStore(root=tmp_path)
    store.write(Memory(name="a-one", description="第一則", type="project"))
    idx = store.index_text()
    assert "a-one" in idx and "第一則" in idx
    assert (tmp_path / "MEMORY.md").is_file()


def test_invalid_type_rejected_on_markdown():
    with pytest.raises(ValueError):
        Memory(name="x", description="d", type="bogus").to_markdown()


def test_delete(tmp_path):
    store = MemoryStore(root=tmp_path)
    store.write(Memory(name="gone", description="d", type="reference"))
    assert store.delete("gone") is True
    assert store.read("gone") is None
    assert store.delete("missing") is False
