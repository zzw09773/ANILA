"""Tests for compact services — micro compact, auto compact, session memory."""

from __future__ import annotations

import asyncio

import pytest

from agentic_rag.compact.auto_compact import (
    AUTOCOMPACT_BUFFER_TOKENS,
    MAX_OUTPUT_TOKENS_FOR_SUMMARY,
    get_auto_compact_threshold,
    should_compact,
)
from agentic_rag.compact.micro_compact import (
    COMPACTABLE_TOOLS,
    TIME_BASED_MC_CLEARED_MESSAGE,
    micro_compact_messages,
    time_based_micro_compact,
)
from agentic_rag.compact.session_memory import SessionMemoryConfig, SessionMemoryService
from agentic_rag.context.agent_context import AgentContext
from agentic_rag.models.message import AssistantMessage, ToolCall, UserMessage


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_assistant_with_tool_call(tool_name: str, tool_id: str) -> AssistantMessage:
    return AssistantMessage(
        content=[
            {"type": "tool_use", "id": tool_id, "name": tool_name, "input": {"command": "ls"}},
        ],
        tool_calls=[ToolCall(id=tool_id, name=tool_name, input={"command": "ls"})],
    )


def make_tool_result_message(tool_id: str, content: str = "output here") -> UserMessage:
    return UserMessage(
        content=[
            {"type": "tool_result", "tool_use_id": tool_id, "content": content}
        ]
    )


# ---------------------------------------------------------------------------
# MicroCompact
# ---------------------------------------------------------------------------

class TestMicroCompact:
    def test_clears_specified_tool_results(self) -> None:
        asst = make_assistant_with_tool_call("bash", "t1")
        result_msg = make_tool_result_message("t1", "lots of output")
        messages = [asst, result_msg]
        new_messages = micro_compact_messages(messages, ["t1"])

        # Original messages not mutated
        assert result_msg.content[0]["content"] == "lots of output"

        # New messages have cleared content
        user_block = new_messages[-1].content[0]
        assert user_block["content"] == TIME_BASED_MC_CLEARED_MESSAGE

    def test_does_not_clear_non_listed_tools(self) -> None:
        asst = make_assistant_with_tool_call("bash", "t1")
        result_msg = make_tool_result_message("t1", "important output")
        messages = [asst, result_msg]
        new_messages = micro_compact_messages(messages, [])  # empty clear list
        assert new_messages[-1].content[0]["content"] == "important output"

    def test_returns_new_list_not_mutated(self) -> None:
        asst = make_assistant_with_tool_call("bash", "t1")
        result_msg = make_tool_result_message("t1", "content")
        messages = [asst, result_msg]
        new_messages = micro_compact_messages(messages, ["t1"])
        assert new_messages is not messages

    def test_already_cleared_not_double_cleared(self) -> None:
        asst = make_assistant_with_tool_call("bash", "t1")
        result_msg = make_tool_result_message("t1", TIME_BASED_MC_CLEARED_MESSAGE)
        messages = [asst, result_msg]
        new_messages = micro_compact_messages(messages, ["t1"])
        # Should still be the same cleared message
        assert new_messages[-1].content[0]["content"] == TIME_BASED_MC_CLEARED_MESSAGE

    def test_compactable_tools_set(self) -> None:
        assert "bash" in COMPACTABLE_TOOLS
        assert "file_read" in COMPACTABLE_TOOLS
        assert "grep" in COMPACTABLE_TOOLS
        assert "glob" in COMPACTABLE_TOOLS

    def test_time_based_keeps_recent(self) -> None:
        messages = []
        for i in range(5):
            asst = make_assistant_with_tool_call("bash", f"t{i}")
            result = make_tool_result_message(f"t{i}", f"output-{i}")
            messages.extend([asst, result])

        new_msgs, tokens_saved = time_based_micro_compact(messages, keep_recent=2)

        # Last 2 should be preserved
        user_blocks = [
            block
            for msg in new_msgs
            if isinstance(msg, UserMessage) and isinstance(msg.content, list)
            for block in msg.content
            if isinstance(block, dict) and block.get("type") == "tool_result"
        ]
        cleared = [b for b in user_blocks if b["content"] == TIME_BASED_MC_CLEARED_MESSAGE]
        kept = [b for b in user_blocks if b["content"] != TIME_BASED_MC_CLEARED_MESSAGE]
        assert len(kept) == 2
        assert len(cleared) == 3
        assert tokens_saved > 0


# ---------------------------------------------------------------------------
# AutoCompact
# ---------------------------------------------------------------------------

class TestAutoCompact:
    def test_should_compact_above_threshold(self) -> None:
        context_window = 200_000
        threshold = get_auto_compact_threshold(context_window)
        assert should_compact(context_window, threshold + 1)

    def test_should_not_compact_below_threshold(self) -> None:
        context_window = 200_000
        threshold = get_auto_compact_threshold(context_window)
        assert not should_compact(context_window, threshold - 1000)

    def test_threshold_formula(self) -> None:
        context_window = 200_000
        threshold = get_auto_compact_threshold(context_window)
        expected = context_window - MAX_OUTPUT_TOKENS_FOR_SUMMARY - AUTOCOMPACT_BUFFER_TOKENS
        assert threshold == expected

    def test_exactly_at_threshold_triggers(self) -> None:
        context_window = 100_000
        threshold = get_auto_compact_threshold(context_window)
        assert should_compact(context_window, threshold)

    def test_custom_max_output_tokens(self) -> None:
        context_window = 100_000
        result = should_compact(context_window, 85_000, max_output_tokens=5_000)
        # threshold = 100000 - 5000 - 13000 = 82000; 85000 >= 82000 -> True
        assert result


# ---------------------------------------------------------------------------
# SessionMemory golden test
# ---------------------------------------------------------------------------

class TestSessionMemory:
    def _make_messages(self, n_tool_calls: int, final_text: str = "Done") -> list:
        messages = []
        for i in range(n_tool_calls):
            asst = AssistantMessage(
                content=[
                    {"type": "tool_use", "id": f"t{i}", "name": "bash", "input": {}},
                ],
                tool_calls=[ToolCall(id=f"t{i}", name="bash", input={})],
            )
            result = make_tool_result_message(f"t{i}", f"output-{i}" * 100)
            messages.extend([asst, result])
        messages.append(AssistantMessage(content=final_text, tool_calls=[]))
        return messages

    def test_should_extract_after_init_threshold(self) -> None:
        config = SessionMemoryConfig(
            minimum_tokens_to_init=100,
            minimum_tokens_between_updates=50,
            tool_calls_between_updates=2,
        )
        svc = SessionMemoryService(config)
        messages = self._make_messages(n_tool_calls=5)
        assert svc.should_extract(messages)

    def test_should_not_extract_below_init_threshold(self) -> None:
        config = SessionMemoryConfig(
            minimum_tokens_to_init=1_000_000,
            minimum_tokens_between_updates=0,
            tool_calls_between_updates=0,
        )
        svc = SessionMemoryService(config)
        messages = [UserMessage(content="hello"), AssistantMessage(content="hi", tool_calls=[])]
        assert not svc.should_extract(messages)

    def test_should_not_extract_when_last_turn_has_tool_calls(self) -> None:
        config = SessionMemoryConfig(
            minimum_tokens_to_init=10,
            minimum_tokens_between_updates=10,
            tool_calls_between_updates=100,  # very high: tool count threshold NOT met
        )
        svc = SessionMemoryService(config)
        messages = self._make_messages(n_tool_calls=3)
        # Remove the final text message so that last assistant turn has tool calls
        # and the token-only path checks _has_tool_calls_in_last_assistant_turn
        messages = messages[:-1]
        # Last assistant message has tool_calls, so should NOT extract via token-only path
        assert not svc.should_extract(messages)

    @pytest.mark.asyncio
    async def test_extract_calls_run_fn(self) -> None:
        config = SessionMemoryConfig(
            minimum_tokens_to_init=50,
            minimum_tokens_between_updates=50,
            tool_calls_between_updates=1,
        )
        svc = SessionMemoryService(config)
        messages = self._make_messages(n_tool_calls=3)

        extract_called = False

        async def mock_run(msgs, ctx):
            nonlocal extract_called
            extract_called = True
            return "extracted notes"

        ctx = AgentContext(session_id="test", model="test")
        await svc.extract(messages, ctx, mock_run)
        assert extract_called

    @pytest.mark.asyncio
    async def test_no_overlapping_extractions(self) -> None:
        """Test that concurrent calls do not overlap — second call returns None."""
        config = SessionMemoryConfig(
            minimum_tokens_to_init=10,
            minimum_tokens_between_updates=10,
            tool_calls_between_updates=1,
        )
        svc = SessionMemoryService(config)
        messages = self._make_messages(n_tool_calls=3)

        call_count = 0
        # Use an event that first task will wait on so second task sees in_progress=True
        first_started = asyncio.Event()
        allow_finish = asyncio.Event()

        async def slow_run(msgs, ctx):
            nonlocal call_count
            call_count += 1
            first_started.set()
            await allow_finish.wait()
            return "done"

        ctx = AgentContext(session_id="test", model="test")

        # Start task1 and wait for it to enter the slow function
        task1 = asyncio.create_task(svc.extract(messages, ctx, slow_run))
        await first_started.wait()

        # Now task1 is running (in_progress=True). task2 should see this and skip.
        task2 = asyncio.create_task(svc.extract(messages, ctx, slow_run))
        result2 = await task2
        assert result2 is None  # blocked by in_progress

        # Let task1 finish
        allow_finish.set()
        await task1
        assert call_count == 1
