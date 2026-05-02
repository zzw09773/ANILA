"""Tests for :class:`SqliteSession`.

Re-uses the same scenarios as :mod:`test_session_memory` plus
SQLite-specific tests (cross-session isolation in one DB, persistence
across instances, in-memory mode, AssistantMessage round-trip).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

from anila_core.memory import (
    InterruptRecord,
    Session,
    SqliteSession,
    close_all_connections,
    new_interrupt_id,
    new_session_id,
)
from anila_core.memory.sqlite_session import _conn_cache
from anila_core.models.message import AssistantMessage, ToolCall, UserMessage


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db_path(tmp_path: Path):
    """Per-test SQLite file. Cache cleared in teardown to free FDs."""
    db = tmp_path / "sessions.db"
    yield db
    await close_all_connections()


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_sqlite_session_satisfies_protocol(tmp_path: Path) -> None:
    sess = SqliteSession(tmp_path / "x.db", new_session_id())
    assert isinstance(sess, Session)


# ---------------------------------------------------------------------------
# Conversation history (mirrors test_session_memory)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_and_get_items_round_trip(db_path: Path) -> None:
    sess = SqliteSession(db_path, "s1")
    await sess.add_items(
        [
            UserMessage(content="hi"),
            AssistantMessage(content="hello"),
            UserMessage(content="how are you"),
        ]
    )

    fetched = await sess.get_items()
    assert [m.role for m in fetched] == ["user", "assistant", "user"]
    assert fetched[0].get_text() == "hi"
    assert fetched[2].get_text() == "how are you"


@pytest.mark.asyncio
async def test_assistant_message_with_tool_calls_round_trips(
    db_path: Path,
) -> None:
    sess = SqliteSession(db_path, "s1")
    msg = AssistantMessage(
        content=[{"type": "text", "text": "calling tool"}],
        tool_calls=[ToolCall(name="echo", input={"text": "hi"})],
    )
    await sess.add_items([msg])

    [restored] = await sess.get_items()
    assert isinstance(restored, AssistantMessage)
    assert restored.tool_calls[0].name == "echo"
    assert restored.tool_calls[0].input == {"text": "hi"}


@pytest.mark.asyncio
async def test_get_items_with_limit_returns_chronological_tail(
    db_path: Path,
) -> None:
    sess = SqliteSession(db_path, "s1")
    await sess.add_items([UserMessage(content=f"msg-{i}") for i in range(5)])

    last2 = await sess.get_items(limit=2)
    assert [m.get_text() for m in last2] == ["msg-3", "msg-4"]


@pytest.mark.asyncio
async def test_get_items_limit_zero_returns_empty(db_path: Path) -> None:
    sess = SqliteSession(db_path, "s1")
    await sess.add_items([UserMessage(content="x")])
    assert await sess.get_items(limit=0) == []


@pytest.mark.asyncio
async def test_pop_item_returns_most_recent_then_removes(db_path: Path) -> None:
    sess = SqliteSession(db_path, "s1")
    await sess.add_items(
        [UserMessage(content="a"), AssistantMessage(content="b")]
    )

    popped = await sess.pop_item()
    assert popped is not None
    assert popped.get_text() == "b"

    remaining = await sess.get_items()
    assert len(remaining) == 1 and remaining[0].get_text() == "a"


@pytest.mark.asyncio
async def test_pop_item_on_empty_returns_none(db_path: Path) -> None:
    sess = SqliteSession(db_path, "s1")
    assert await sess.pop_item() is None


@pytest.mark.asyncio
async def test_clear_session_wipes_items_and_interrupts(db_path: Path) -> None:
    sess = SqliteSession(db_path, "s1")
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
async def test_push_and_list_interrupts_in_creation_order(
    db_path: Path,
) -> None:
    sess = SqliteSession(db_path, "s1")
    a = InterruptRecord(
        id="int-a", kind="ask_user", payload={"q": "what color?"}
    )
    b = InterruptRecord(id="int-b", kind="plan", payload={"plan": "do x"})

    await sess.push_interrupt(a)
    await sess.push_interrupt(b)

    pending = await sess.pending_interrupts()
    assert [p.id for p in pending] == ["int-a", "int-b"]
    assert pending[0].payload == {"q": "what color?"}
    assert pending[1].kind == "plan"


@pytest.mark.asyncio
async def test_pop_interrupt_returns_record_and_removes(db_path: Path) -> None:
    sess = SqliteSession(db_path, "s1")
    await sess.push_interrupt(
        InterruptRecord(id="int-a", kind="ask_user", payload={})
    )
    await sess.push_interrupt(
        InterruptRecord(id="int-b", kind="plan", payload={})
    )

    popped = await sess.pop_interrupt("int-a")
    assert popped is not None and popped.id == "int-a"

    pending = await sess.pending_interrupts()
    assert [p.id for p in pending] == ["int-b"]


@pytest.mark.asyncio
async def test_pop_interrupt_unknown_returns_none(db_path: Path) -> None:
    sess = SqliteSession(db_path, "s1")
    assert await sess.pop_interrupt("ghost") is None


@pytest.mark.asyncio
async def test_interrupt_payload_handles_unicode(db_path: Path) -> None:
    """Sanity check ensure_ascii=False on the JSON dump."""
    sess = SqliteSession(db_path, "s1")
    await sess.push_interrupt(
        InterruptRecord(
            id="int-zh", kind="ask_user", payload={"q": "你想往哪個方向？"}
        )
    )
    [restored] = await sess.pending_interrupts()
    assert restored.payload == {"q": "你想往哪個方向？"}


# ---------------------------------------------------------------------------
# SQLite-specific scenarios
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_distinct_sessions_share_one_db_but_isolate_data(
    db_path: Path,
) -> None:
    alice = SqliteSession(db_path, "alice")
    bob = SqliteSession(db_path, "bob")
    await alice.add_items([UserMessage(content="alice-msg")])
    await bob.add_items([UserMessage(content="bob-msg")])

    a_items = await alice.get_items()
    b_items = await bob.get_items()
    assert len(a_items) == 1 and a_items[0].get_text() == "alice-msg"
    assert len(b_items) == 1 and b_items[0].get_text() == "bob-msg"


@pytest.mark.asyncio
async def test_state_persists_across_session_instances(db_path: Path) -> None:
    """Two SqliteSession instances pointing at the same (path, session_id)
    see each other's writes — the conversation isn't tied to the Python
    object, only to the row data."""
    s1 = SqliteSession(db_path, "shared")
    await s1.add_items([UserMessage(content="from instance 1")])

    s2 = SqliteSession(db_path, "shared")
    items = await s2.get_items()
    assert len(items) == 1 and items[0].get_text() == "from instance 1"


@pytest.mark.asyncio
async def test_in_memory_db_isolated_per_instance() -> None:
    """``:memory:`` DBs are per-instance — two instances must NOT share."""
    a = SqliteSession(":memory:", "s1")
    b = SqliteSession(":memory:", "s1")
    await a.add_items([UserMessage(content="a-only")])

    assert len(await a.get_items()) == 1
    assert await b.get_items() == []


@pytest.mark.asyncio
async def test_close_all_connections_clears_cache(db_path: Path) -> None:
    sess = SqliteSession(db_path, "s1")
    await sess.add_items([UserMessage(content="x")])
    assert _conn_cache, "expected a cached connection after first use"

    await close_all_connections()
    assert _conn_cache == {}


@pytest.mark.asyncio
async def test_db_file_auto_created_in_nested_dir(tmp_path: Path) -> None:
    """Parent directory should be created on demand (no manual mkdir)."""
    nested = tmp_path / "deep" / "nested" / "sessions.db"
    sess = SqliteSession(nested, "s1")
    await sess.add_items([UserMessage(content="x")])
    assert nested.exists()
    await close_all_connections()
