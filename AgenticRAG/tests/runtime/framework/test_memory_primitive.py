"""Sprint 4 tests — MessageHistory + SemanticMemory Protocols + impls + bridge."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agentic_rag.runtime.framework.items import Message
from agentic_rag.runtime.framework.memory import (
    InMemoryMessageHistory,
    InMemorySemanticMemory,
    MemoryEntry,
    MemoryKind,
    MessageHistory,
    SemanticMemory,
)


# ── Protocol shape ──────────────────────────────────────────────────


def test_in_memory_history_satisfies_protocol() -> None:
    h = InMemoryMessageHistory()
    assert isinstance(h, MessageHistory)


def test_in_memory_semantic_satisfies_protocol() -> None:
    s = InMemorySemanticMemory()
    assert isinstance(s, SemanticMemory)


# ── MemoryEntry ─────────────────────────────────────────────────────


def test_memory_entry_default_id_is_unique() -> None:
    a = MemoryEntry(content="x")
    b = MemoryEntry(content="y")
    assert a.id != b.id
    assert a.id.startswith("mem_")


def test_memory_entry_no_ttl_never_expires() -> None:
    e = MemoryEntry(content="x", ttl_seconds=None)
    assert e.expires_at is None
    assert e.is_expired() is False


def test_memory_entry_expires_at_computed_from_updated_at_and_ttl() -> None:
    now = datetime.now(timezone.utc)
    e = MemoryEntry(content="x", ttl_seconds=60.0, updated_at=now)
    assert e.expires_at is not None
    assert abs((e.expires_at - (now + timedelta(seconds=60))).total_seconds()) < 1


def test_memory_entry_is_expired_by_explicit_now() -> None:
    past = datetime.now(timezone.utc) - timedelta(seconds=120)
    e = MemoryEntry(content="x", ttl_seconds=60.0, updated_at=past)
    assert e.is_expired() is True


# ── InMemoryMessageHistory ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_message_history_append_and_get_round_trip() -> None:
    h = InMemoryMessageHistory()
    await h.append(Message.user("hi"))
    await h.append(Message.assistant("hello"))
    msgs = await h.get()
    assert len(msgs) == 2
    assert msgs[0].content == "hi"


@pytest.mark.asyncio
async def test_message_history_get_with_limit_returns_tail() -> None:
    h = InMemoryMessageHistory()
    for i in range(5):
        await h.append(Message.user(f"m{i}"))
    msgs = await h.get(limit=2)
    assert len(msgs) == 2
    assert msgs[0].content == "m3"
    assert msgs[1].content == "m4"


@pytest.mark.asyncio
async def test_message_history_truncate_drops_first_n() -> None:
    h = InMemoryMessageHistory()
    for i in range(5):
        await h.append(Message.user(f"m{i}"))
    await h.truncate(2)
    msgs = await h.get()
    assert len(msgs) == 3
    assert msgs[0].content == "m2"


@pytest.mark.asyncio
async def test_message_history_clear_empties() -> None:
    h = InMemoryMessageHistory()
    await h.append(Message.user("x"))
    await h.clear()
    assert len(h) == 0


# ── InMemorySemanticMemory ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_semantic_memory_remember_then_list() -> None:
    s = InMemorySemanticMemory()
    e = MemoryEntry(content="user prefers concise answers", kind=MemoryKind.USER)
    await s.remember(e)
    entries = await s.list_all()
    assert len(entries) == 1
    assert entries[0].id == e.id


@pytest.mark.asyncio
async def test_semantic_memory_recall_substring_ranking() -> None:
    s = InMemorySemanticMemory()
    await s.remember(MemoryEntry(content="user prefers concise answers", kind=MemoryKind.USER))
    await s.remember(MemoryEntry(content="project uses pytest", kind=MemoryKind.PROJECT))
    await s.remember(MemoryEntry(content="reranker is enabled by default", kind=MemoryKind.REFERENCE))

    hits = await s.recall("concise answer style")
    assert len(hits) >= 1
    assert "concise" in hits[0].content


@pytest.mark.asyncio
async def test_semantic_memory_recall_filters_by_kind() -> None:
    s = InMemorySemanticMemory()
    await s.remember(MemoryEntry(content="user thing", kind=MemoryKind.USER))
    await s.remember(MemoryEntry(content="project thing", kind=MemoryKind.PROJECT))

    user_hits = await s.recall("thing", kind=MemoryKind.USER)
    project_hits = await s.recall("thing", kind=MemoryKind.PROJECT)
    assert len(user_hits) == 1 and user_hits[0].kind is MemoryKind.USER
    assert len(project_hits) == 1 and project_hits[0].kind is MemoryKind.PROJECT


@pytest.mark.asyncio
async def test_semantic_memory_recall_ignores_expired_entries() -> None:
    s = InMemorySemanticMemory()
    past = datetime.now(timezone.utc) - timedelta(hours=2)
    await s.remember(
        MemoryEntry(
            content="should be expired",
            kind=MemoryKind.WORKING,
            ttl_seconds=60.0,
            updated_at=past,
        )
    )
    await s.remember(MemoryEntry(content="fresh entry"))
    hits = await s.recall("expired fresh")
    contents = [h.content for h in hits]
    assert "should be expired" not in contents
    assert "fresh entry" in contents


@pytest.mark.asyncio
async def test_semantic_memory_recall_empty_query_returns_recent() -> None:
    s = InMemorySemanticMemory()
    older = MemoryEntry(content="older", updated_at=datetime.now(timezone.utc) - timedelta(days=2))
    newer = MemoryEntry(content="newer")
    await s.remember(older)
    await s.remember(newer)
    hits = await s.recall("", limit=2)
    assert hits[0].content == "newer"


@pytest.mark.asyncio
async def test_semantic_memory_forget_removes_by_id() -> None:
    s = InMemorySemanticMemory()
    e = MemoryEntry(content="x")
    await s.remember(e)
    await s.forget(e.id)
    assert len(s) == 0


@pytest.mark.asyncio
async def test_semantic_memory_remember_upserts_on_duplicate_id() -> None:
    s = InMemorySemanticMemory()
    e1 = MemoryEntry(id="fixed", content="v1")
    e2 = MemoryEntry(id="fixed", content="v2")
    await s.remember(e1)
    await s.remember(e2)
    assert len(s) == 1
    entries = await s.list_all()
    assert entries[0].content == "v2"


# ── Bridge: MemdirSemanticMemory ────────────────────────────────────


@pytest.mark.asyncio
async def test_memdir_bridge_round_trips_entry_through_disk(tmp_path) -> None:
    from agentic_rag.runtime.bridge.semantic_memory_bridge import MemdirSemanticMemory

    memory = MemdirSemanticMemory(memory_dir=tmp_path)
    e = MemoryEntry(
        id="user_role",
        kind=MemoryKind.USER,
        content="Senior backend engineer working on RAG.",
        metadata={"title": "User role", "description": "Background", "tags": ["bg"]},
    )
    await memory.remember(e)
    entries = await memory.list_all()
    assert len(entries) == 1
    assert entries[0].id == "user_role"
    assert entries[0].kind is MemoryKind.USER
    assert "Senior backend engineer" in entries[0].content


@pytest.mark.asyncio
async def test_memdir_bridge_recall_falls_back_to_substring_without_selector(
    tmp_path,
) -> None:
    from agentic_rag.runtime.bridge.semantic_memory_bridge import MemdirSemanticMemory

    memory = MemdirSemanticMemory(memory_dir=tmp_path)
    await memory.remember(
        MemoryEntry(
            id="pref",
            kind=MemoryKind.USER,
            content="prefers concise answers",
            metadata={"title": "Style preference", "description": "user likes concise"},
        )
    )
    await memory.remember(
        MemoryEntry(
            id="proj",
            kind=MemoryKind.PROJECT,
            content="uses pytest",
            metadata={"title": "Test framework", "description": "project standard"},
        )
    )

    hits = await memory.recall("concise style")
    assert len(hits) >= 1
    assert hits[0].id == "pref"


@pytest.mark.asyncio
async def test_memdir_bridge_filters_by_kind(tmp_path) -> None:
    from agentic_rag.runtime.bridge.semantic_memory_bridge import MemdirSemanticMemory

    memory = MemdirSemanticMemory(memory_dir=tmp_path)
    await memory.remember(
        MemoryEntry(id="u", kind=MemoryKind.USER, content="user fact",
                    metadata={"title": "u", "description": "user d"})
    )
    await memory.remember(
        MemoryEntry(id="p", kind=MemoryKind.PROJECT, content="project fact",
                    metadata={"title": "p", "description": "project d"})
    )

    only_user = await memory.list_all(kind=MemoryKind.USER)
    assert len(only_user) == 1
    assert only_user[0].id == "u"


@pytest.mark.asyncio
async def test_memdir_bridge_forget_removes_file(tmp_path) -> None:
    from agentic_rag.runtime.bridge.semantic_memory_bridge import MemdirSemanticMemory

    memory = MemdirSemanticMemory(memory_dir=tmp_path)
    e = MemoryEntry(id="to-delete", kind=MemoryKind.USER, content="x",
                    metadata={"title": "x", "description": "x"})
    await memory.remember(e)
    assert (tmp_path / "to-delete.md").exists()
    await memory.forget("to-delete")
    assert not (tmp_path / "to-delete.md").exists()


@pytest.mark.asyncio
async def test_memdir_bridge_forget_missing_id_is_no_op(tmp_path) -> None:
    from agentic_rag.runtime.bridge.semantic_memory_bridge import MemdirSemanticMemory

    memory = MemdirSemanticMemory(memory_dir=tmp_path)
    await memory.forget("never-existed")  # must not raise
