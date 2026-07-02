"""Unit tests for the approvals primitive.

Covers the pure functions / dataclasses; QueryEngine integration lives in
:mod:`test_engine_interrupt`.
"""

from __future__ import annotations

import pytest

from anila_core.engine.approvals import (
    MultipleInterruptsError,
    RunPaused,
    build_resume_message,
    resume_with,
    to_record,
)
from anila_core.memory import InterruptRecord, MemorySession
from anila_core.models.interrupt import InterruptItem
from anila_core.models.message import ToolCall, ToolResult, UserMessage


# ---------------------------------------------------------------------------
# InterruptItem
# ---------------------------------------------------------------------------


def test_interrupt_item_auto_ids_unique() -> None:
    seen = {InterruptItem(kind="ask_user").id for _ in range(50)}
    assert len(seen) == 50


def test_interrupt_item_is_immutable() -> None:
    item = InterruptItem(kind="ask_user", payload={"q": "?"})
    with pytest.raises(Exception):  # pydantic ValidationError
        item.payload = {"q": "!"}  # type: ignore[misc]


# ---------------------------------------------------------------------------
# to_record
# ---------------------------------------------------------------------------


def test_to_record_captures_tool_call_and_siblings() -> None:
    item = InterruptItem(
        id="int-1",
        kind="ask_user",
        payload={"question": "what color?", "options": [{"label": "blue"}]},
    )
    interrupted_call = ToolCall(id="c1", name="ask_user", input={"q": "?"})
    sibling = ToolResult(tool_call_id="c2", content="search hits 3")

    record = to_record(item, tool_call=interrupted_call, sibling_results=[sibling])

    assert isinstance(record, InterruptRecord)
    assert record.id == "int-1"
    assert record.kind == "ask_user"
    assert record.payload["data"] == {
        "question": "what color?",
        "options": [{"label": "blue"}],
    }
    assert record.payload["tool_call"] == {
        "id": "c1",
        "name": "ask_user",
        "input": {"q": "?"},
    }
    assert record.payload["sibling_results"] == [
        {"tool_call_id": "c2", "content": "search hits 3", "is_error": False}
    ]


# ---------------------------------------------------------------------------
# build_resume_message
# ---------------------------------------------------------------------------


def _record_for_ask_user() -> InterruptRecord:
    item = InterruptItem(id="int-r", kind="ask_user", payload={})
    return to_record(
        item,
        tool_call=ToolCall(id="c-ask", name="ask_user", input={}),
        sibling_results=[
            ToolResult(tool_call_id="c-search", content="found 2 docs"),
            ToolResult(tool_call_id="c-err", content="boom", is_error=True),
        ],
    )


def test_build_resume_message_orders_siblings_then_interrupt() -> None:
    record = _record_for_ask_user()
    msg = build_resume_message(
        record, {"selected": ["blue"], "other_text": "hot pink"}
    )
    assert isinstance(msg, UserMessage)
    blocks = msg.content
    assert isinstance(blocks, list)
    assert [b["tool_use_id"] for b in blocks] == ["c-search", "c-err", "c-ask"]
    assert blocks[1].get("is_error") is True
    assert "blue" in blocks[2]["content"] and "hot pink" in blocks[2]["content"]


def test_build_resume_message_renders_ask_user_selected_only() -> None:
    record = _record_for_ask_user()
    msg = build_resume_message(record, {"selected": ["a", "b"]})
    blocks = msg.content
    assert isinstance(blocks, list)
    assert blocks[-1]["content"] == "user_selected: a, b"


def test_build_resume_message_renders_ask_user_other_only() -> None:
    record = _record_for_ask_user()
    msg = build_resume_message(record, {"other_text": "custom"})
    blocks = msg.content
    assert isinstance(blocks, list)
    assert blocks[-1]["content"] == "user_input: custom"


def test_build_resume_message_renders_plan_approved_with_comment() -> None:
    item = InterruptItem(id="int-p", kind="plan", payload={"plan": "..."})
    record = to_record(
        item,
        tool_call=ToolCall(id="c-plan", name="exit_plan_mode", input={}),
        sibling_results=[],
    )
    msg = build_resume_message(record, {"approved": True, "comment": "lgtm"})
    blocks = msg.content
    assert isinstance(blocks, list)
    assert blocks[0]["content"] == "plan_approved\nuser_comment: lgtm"


def test_build_resume_message_renders_plan_rejected_no_comment() -> None:
    item = InterruptItem(id="int-p", kind="plan", payload={"plan": "..."})
    record = to_record(
        item,
        tool_call=ToolCall(id="c-plan", name="exit_plan_mode", input={}),
        sibling_results=[],
    )
    msg = build_resume_message(record, {"approved": False})
    blocks = msg.content
    assert isinstance(blocks, list)
    assert blocks[0]["content"] == "plan_rejected"


def test_build_resume_message_string_answer_passes_through() -> None:
    record = _record_for_ask_user()
    msg = build_resume_message(record, "raw user text")
    blocks = msg.content
    assert isinstance(blocks, list)
    assert blocks[-1]["content"] == "raw user text"


def test_build_resume_message_handles_empty_ask_user() -> None:
    record = _record_for_ask_user()
    msg = build_resume_message(record, {})
    blocks = msg.content
    assert isinstance(blocks, list)
    assert blocks[-1]["content"] == "(no answer provided)"


# ---------------------------------------------------------------------------
# resume_with (Session integration)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_with_pops_interrupt_and_builds_message() -> None:
    sess = MemorySession("s1")
    await sess.push_interrupt(_record_for_ask_user())

    msg = await resume_with(sess, "int-r", {"selected": ["x"]})

    assert isinstance(msg, UserMessage)
    assert await sess.pending_interrupts() == []


@pytest.mark.asyncio
async def test_resume_with_unknown_id_raises() -> None:
    sess = MemorySession("s1")
    with pytest.raises(ValueError, match="not found"):
        await resume_with(sess, "ghost", {})


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


def test_run_paused_carries_session_and_interrupt_ids() -> None:
    exc = RunPaused(session_id="s1", interrupt_id="int-1", kind="ask_user")
    assert exc.session_id == "s1"
    assert exc.interrupt_id == "int-1"
    assert exc.kind == "ask_user"
    assert "s1" in str(exc)
    assert "int-1" in str(exc)


def test_multiple_interrupts_error_is_runtime_error() -> None:
    assert issubclass(MultipleInterruptsError, RuntimeError)
