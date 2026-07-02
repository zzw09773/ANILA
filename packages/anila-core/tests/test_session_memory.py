"""Tests for the Protocol surface via :class:`MemorySession`.

These tests double as the spec for any future Session adapter — every
adapter must pass the same battery (see :mod:`test_session_sqlite` for
the SQLite variant that imports ``_PROTOCOL_TESTS`` from here).
"""

from __future__ import annotations

import pytest

from anila_core.memory import (
    InterruptRecord,
    MemorySession,
    Session,
    new_interrupt_id,
    new_session_id,
)
from anila_core.models.message import AssistantMessage, UserMessage


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_memory_session_satisfies_protocol() -> None:
    sess = MemorySession(new_session_id())
    assert isinstance(sess, Session)


# ---------------------------------------------------------------------------
# Conversation history
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_and_get_items_round_trip() -> None:
    sess = MemorySession("s1")
    items = [
        UserMessage(content="hi"),
        AssistantMessage(content="hello"),
        UserMessage(content="how are you"),
    ]
    await sess.add_items(items)

    fetched = await sess.get_items()
    assert len(fetched) == 3
    assert [m.role for m in fetched] == ["user", "assistant", "user"]
    assert fetched[0].get_text() == "hi"
    assert fetched[2].get_text() == "how are you"


@pytest.mark.asyncio
async def test_get_items_with_limit_returns_chronological_tail() -> None:
    sess = MemorySession("s1")
    await sess.add_items(
        [UserMessage(content=f"msg-{i}") for i in range(5)]
    )

    last2 = await sess.get_items(limit=2)
    assert [m.get_text() for m in last2] == ["msg-3", "msg-4"]


@pytest.mark.asyncio
async def test_get_items_limit_zero_returns_empty() -> None:
    sess = MemorySession("s1")
    await sess.add_items([UserMessage(content="x")])
    assert await sess.get_items(limit=0) == []


@pytest.mark.asyncio
async def test_pop_item_returns_most_recent_then_removes() -> None:
    sess = MemorySession("s1")
    await sess.add_items(
        [UserMessage(content="a"), AssistantMessage(content="b")]
    )

    popped = await sess.pop_item()
    assert popped is not None
    assert popped.get_text() == "b"

    remaining = await sess.get_items()
    assert len(remaining) == 1
    assert remaining[0].get_text() == "a"


@pytest.mark.asyncio
async def test_pop_item_on_empty_returns_none() -> None:
    sess = MemorySession("s1")
    assert await sess.pop_item() is None


@pytest.mark.asyncio
async def test_clear_session_wipes_items_and_interrupts() -> None:
    sess = MemorySession("s1")
    await sess.add_items([UserMessage(content="x")])
    await sess.push_interrupt(
        InterruptRecord(id=new_interrupt_id(), kind="ask_user", payload={})
    )

    await sess.clear_session()

    assert await sess.get_items() == []
    assert await sess.pending_interrupts() == []


# ---------------------------------------------------------------------------
# Pending interrupts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_push_and_list_interrupts_in_creation_order() -> None:
    sess = MemorySession("s1")
    a = InterruptRecord(id="int-a", kind="ask_user", payload={"q": "1"})
    b = InterruptRecord(id="int-b", kind="plan", payload={"plan": "do x"})

    await sess.push_interrupt(a)
    await sess.push_interrupt(b)

    pending = await sess.pending_interrupts()
    assert [p.id for p in pending] == ["int-a", "int-b"]
    assert pending[0].kind == "ask_user"
    assert pending[1].payload == {"plan": "do x"}


@pytest.mark.asyncio
async def test_pop_interrupt_returns_record_and_removes() -> None:
    sess = MemorySession("s1")
    a = InterruptRecord(id="int-a", kind="ask_user", payload={})
    b = InterruptRecord(id="int-b", kind="plan", payload={})
    await sess.push_interrupt(a)
    await sess.push_interrupt(b)

    popped = await sess.pop_interrupt("int-a")
    assert popped is not None
    assert popped.id == "int-a"

    pending = await sess.pending_interrupts()
    assert [p.id for p in pending] == ["int-b"]


@pytest.mark.asyncio
async def test_pop_interrupt_unknown_returns_none() -> None:
    sess = MemorySession("s1")
    assert await sess.pop_interrupt("does-not-exist") is None


# ---------------------------------------------------------------------------
# Session isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_distinct_sessions_are_isolated() -> None:
    a = MemorySession("alice")
    b = MemorySession("bob")
    await a.add_items([UserMessage(content="alice-msg")])
    await b.add_items([UserMessage(content="bob-msg")])

    a_items = await a.get_items()
    b_items = await b.get_items()
    assert len(a_items) == 1 and a_items[0].get_text() == "alice-msg"
    assert len(b_items) == 1 and b_items[0].get_text() == "bob-msg"


# ---------------------------------------------------------------------------
# new_session_id / new_interrupt_id
# ---------------------------------------------------------------------------


def test_new_session_id_returns_unique_strings() -> None:
    seen = {new_session_id() for _ in range(100)}
    assert len(seen) == 100
    assert all(isinstance(s, str) and len(s) >= 8 for s in seen)


def test_new_interrupt_id_has_int_prefix() -> None:
    iid = new_interrupt_id()
    assert iid.startswith("int-")
    assert len(iid) > len("int-")
