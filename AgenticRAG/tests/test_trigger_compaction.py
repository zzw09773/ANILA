"""Tests for B2 — LLM-driven Layer-2 compaction."""

from __future__ import annotations

import pytest

from agentic_rag.compact.model_windows import (
    ModelWindowTable,
)
from agentic_rag.compact.trigger_compaction import (
    COMPACT_SUMMARY_MARKER,
    plan_compaction,
    run_compaction,
)
from agentic_rag.models.message import AssistantMessage, UserMessage


# ── ModelWindowTable ─────────────────────────────────────────────────


def test_window_table_exact_match() -> None:
    t = ModelWindowTable({"google/gemma4": 8_192})
    assert t.get("google/gemma4") == 8_192


def test_window_table_prefix_fallback() -> None:
    t = ModelWindowTable({"meta-llama/Llama-3.1": 128_000})
    assert t.get("meta-llama/Llama-3.1-8B-Instruct") == 128_000


def test_window_table_unknown_returns_fallback() -> None:
    t = ModelWindowTable({}, fallback=4_096)
    assert t.get("nobody/knows") == 4_096


def test_window_table_default_includes_known_models() -> None:
    t = ModelWindowTable()  # defaults
    assert "google/gemma4" in t
    assert "meta-llama/Llama-3.1" in t


def test_window_table_add_validates_positive() -> None:
    t = ModelWindowTable({})
    with pytest.raises(ValueError):
        t.add("bad", 0)


# ── plan_compaction ──────────────────────────────────────────────────


def _msg(text: str, role: str = "user") -> UserMessage | AssistantMessage:
    return UserMessage(content=text) if role == "user" else AssistantMessage(content=text)


def _conversation(turn_count: int, chars_per_msg: int = 100) -> list:
    """Build N user+assistant turns with messages of approx given size."""
    msgs = []
    for i in range(turn_count):
        msgs.append(_msg("u" * chars_per_msg + f" turn{i}", "user"))
        msgs.append(_msg("a" * chars_per_msg + f" turn{i}", "assistant"))
    return msgs


def test_plan_compaction_below_threshold_returns_no_op() -> None:
    msgs = _conversation(turn_count=2, chars_per_msg=50)
    table = ModelWindowTable({"m": 1_000_000})  # huge window
    decision = plan_compaction(msgs, "m", window_table=table)
    assert decision.should_compact is False
    assert decision.compact_indices == []
    assert decision.keep_indices == list(range(len(msgs)))


def test_plan_compaction_above_threshold_picks_old_turns() -> None:
    """Long conversation in a small-window model triggers compaction."""
    msgs = _conversation(turn_count=20, chars_per_msg=2_000)
    table = ModelWindowTable({"m": 8_192})
    decision = plan_compaction(msgs, "m", window_table=table, keep_recent_turns=4)
    assert decision.should_compact is True
    # Recent 4 turns × 2 messages = 8 messages should be kept
    assert len(decision.keep_indices) == 8
    # Older 16 turns × 2 messages = 32 should be marked for compaction
    assert len(decision.compact_indices) == 32


def test_plan_compaction_keeps_summary_marker_on_keep_side() -> None:
    """Existing [歷史摘要] message must not be re-compacted."""
    summary = UserMessage(content=f"{COMPACT_SUMMARY_MARKER} prior summary")
    msgs = [summary, *_conversation(turn_count=20, chars_per_msg=2_000)]
    table = ModelWindowTable({"m": 8_192})
    decision = plan_compaction(msgs, "m", window_table=table, keep_recent_turns=4)
    # Index 0 (the summary marker) must NOT appear in compact_indices.
    assert 0 not in decision.compact_indices
    assert 0 in decision.keep_indices


# ── run_compaction ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_compaction_below_threshold_returns_unchanged() -> None:
    msgs = _conversation(turn_count=2, chars_per_msg=50)
    table = ModelWindowTable({"m": 1_000_000})

    async def summarise(_msgs, _hint):
        raise AssertionError("should not be called")

    result = await run_compaction(msgs, "m", summarise, window_table=table)
    assert result.compacted is False
    assert result.skipped_reason == "below_threshold"
    assert result.new_messages == msgs


@pytest.mark.asyncio
async def test_run_compaction_summarises_and_replaces_old_turns() -> None:
    msgs = _conversation(turn_count=20, chars_per_msg=2_000)
    table = ModelWindowTable({"m": 8_192})

    async def summarise(to_summarise, hint):
        assert "Summarise" in hint
        return f"Compressed {len(to_summarise)} messages."

    result = await run_compaction(
        msgs, "m", summarise, window_table=table, keep_recent_turns=4
    )
    assert result.compacted is True
    assert result.summary_text.startswith("Compressed")
    # First message in new history is the synthetic summary marker.
    assert isinstance(result.new_messages[0], UserMessage)
    assert result.new_messages[0].content.startswith(COMPACT_SUMMARY_MARKER)
    # The 4 most recent turns (8 messages) follow.
    assert len(result.new_messages) == 1 + 8


@pytest.mark.asyncio
async def test_run_compaction_handles_summarise_exception() -> None:
    msgs = _conversation(turn_count=20, chars_per_msg=2_000)
    table = ModelWindowTable({"m": 8_192})

    async def boom(_msgs, _hint):
        raise RuntimeError("LLM unavailable")

    result = await run_compaction(msgs, "m", boom, window_table=table)
    assert result.compacted is False
    assert "summarise_failed" in result.skipped_reason
    assert "LLM unavailable" in result.skipped_reason
    # Original messages preserved.
    assert result.new_messages == msgs


@pytest.mark.asyncio
async def test_run_compaction_handles_empty_summary() -> None:
    msgs = _conversation(turn_count=20, chars_per_msg=2_000)
    table = ModelWindowTable({"m": 8_192})

    async def empty(_msgs, _hint):
        return "   "  # whitespace-only

    result = await run_compaction(msgs, "m", empty, window_table=table)
    assert result.compacted is False
    assert result.skipped_reason == "summarise_returned_empty"


@pytest.mark.asyncio
async def test_run_compaction_reduces_token_count() -> None:
    msgs = _conversation(turn_count=20, chars_per_msg=2_000)
    table = ModelWindowTable({"m": 8_192})

    async def summarise(_msgs, _hint):
        return "Brief."

    result = await run_compaction(msgs, "m", summarise, window_table=table)
    assert result.tokens_after < result.tokens_before


@pytest.mark.asyncio
async def test_run_compaction_does_not_split_turn_with_tool_results() -> None:
    """Tool-result messages must stay attached to their assistant turn."""
    # User → Assistant w/ tool_use → User w/ tool_result → Assistant final
    user1 = UserMessage(content="big query " * 300)
    asst1 = AssistantMessage(
        content=[
            {"type": "text", "text": "calling search..."},
            {
                "type": "tool_use",
                "id": "c1",
                "name": "search",
                "input": {"q": "x"},
            },
        ]
    )
    tool_result = UserMessage(
        content=[
            {"type": "tool_result", "tool_use_id": "c1", "content": "results " * 200}
        ]
    )
    asst2 = AssistantMessage(content="here you go")
    msgs = [user1, asst1, tool_result, asst2] * 8  # 32 messages

    table = ModelWindowTable({"m": 8_192})

    async def summarise(to_summarise, _hint):
        return "summary"

    result = await run_compaction(
        msgs, "m", summarise, window_table=table, keep_recent_turns=2
    )
    assert result.compacted
    # No orphan tool_result without its assistant — verify by confirming
    # each tool_result in the kept tail has a preceding tool_use.
    kept = result.new_messages[1:]  # skip summary marker
    for i, m in enumerate(kept):
        if isinstance(m, UserMessage) and isinstance(m.content, list):
            if any(
                isinstance(b, dict) and b.get("type") == "tool_result"
                for b in m.content
            ):
                # Must have an assistant with tool_use directly before it
                prev = kept[i - 1]
                assert isinstance(prev, AssistantMessage)
