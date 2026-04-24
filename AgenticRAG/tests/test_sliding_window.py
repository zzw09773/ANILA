"""Tests for sliding window compaction and JSON schema normalization."""

from __future__ import annotations


from agentic_rag.compact.sliding_window import (
    SLIDING_WINDOW_SUMMARY,
    sliding_window_compact,
    _split_into_turns,
    _rough_token_count,
)
from agentic_rag.models.message import AssistantMessage, UserMessage
from agentic_rag.models.tool import ToolDefinition, _normalize_schema_types


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _user(text: str) -> UserMessage:
    return UserMessage(content=text)


def _assistant(text: str) -> AssistantMessage:
    return AssistantMessage(content=text)


def _tool_result_user(tool_id: str, content: str) -> UserMessage:
    return UserMessage(content=[
        {"type": "tool_result", "tool_use_id": tool_id, "content": content}
    ])


# ---------------------------------------------------------------------------
# SlidingWindowCompact
# ---------------------------------------------------------------------------

class TestSlidingWindowCompact:
    def test_no_truncation_when_under_budget(self) -> None:
        msgs = [_user("hello"), _assistant("hi")]
        result, dropped = sliding_window_compact(msgs, max_tokens=10000)
        assert len(result) == 2
        assert dropped == 0

    def test_truncates_old_turns(self) -> None:
        # Create 10 turns (20 messages), each ~100 chars
        msgs = []
        for i in range(10):
            msgs.append(_user(f"question {i} " + "x" * 100))
            msgs.append(_assistant(f"answer {i} " + "y" * 100))

        # Set a tight budget that only fits ~4 turns
        result, dropped = sliding_window_compact(
            msgs, max_tokens=300, keep_recent_turns=2,
        )
        # Should have summary + recent turns
        assert dropped > 0
        assert len(result) < len(msgs)
        # Summary marker should be present
        assert any(
            isinstance(m, UserMessage) and
            isinstance(m.content, str) and
            SLIDING_WINDOW_SUMMARY in m.content
            for m in result
        )

    def test_keeps_minimum_recent_turns(self) -> None:
        msgs = []
        for i in range(5):
            msgs.append(_user(f"q{i}"))
            msgs.append(_assistant(f"a{i}"))

        result, _ = sliding_window_compact(
            msgs, max_tokens=1, keep_recent_turns=2,
        )
        # Even with tiny budget, at least 2 turns (4 msgs) + summary
        non_summary = [m for m in result
                       if not (isinstance(m, UserMessage) and
                               isinstance(m.content, str) and
                               SLIDING_WINDOW_SUMMARY in m.content)]
        assert len(non_summary) >= 4  # 2 turns = 4 messages

    def test_empty_messages(self) -> None:
        result, dropped = sliding_window_compact([], max_tokens=1000)
        assert result == []
        assert dropped == 0

    def test_tool_result_stays_with_turn(self) -> None:
        msgs = [
            _user("q1"),
            _assistant("calling tool"),
            _tool_result_user("t1", "tool output"),
            _assistant("final answer"),
            _user("q2"),
            _assistant("a2"),
        ]
        turns = _split_into_turns(msgs)
        # First turn should include q1 + assistant + tool_result + assistant
        assert len(turns) == 2
        assert len(turns[0]) == 4
        assert len(turns[1]) == 2


# ---------------------------------------------------------------------------
# Rough Token Count
# ---------------------------------------------------------------------------

class TestRoughTokenCount:
    def test_string_content(self) -> None:
        msgs = [_user("hello world")]  # 11 chars → ~11/4*4/3 ≈ 3
        count = _rough_token_count(msgs)
        assert count > 0

    def test_block_content(self) -> None:
        msgs = [UserMessage(content=[{"type": "text", "text": "hello world"}])]
        count = _rough_token_count(msgs)
        assert count > 0


# ---------------------------------------------------------------------------
# JSON Schema Normalization (S3)
# ---------------------------------------------------------------------------

class TestNormalizeSchemaTypes:
    def test_replaces_integer_with_number(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "count": {"type": "integer", "description": "number of items"},
                "name": {"type": "string"},
            },
        }
        result = _normalize_schema_types(schema)
        assert result["properties"]["count"]["type"] == "number"
        assert result["properties"]["name"]["type"] == "string"
        assert result["type"] == "object"  # object should not be changed

    def test_nested_schema(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "config": {
                    "type": "object",
                    "properties": {
                        "max_items": {"type": "integer"},
                    },
                },
            },
        }
        result = _normalize_schema_types(schema)
        assert result["properties"]["config"]["properties"]["max_items"]["type"] == "number"

    def test_no_mutation_without_integer(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "score": {"type": "number"},
            },
        }
        result = _normalize_schema_types(schema)
        assert result == schema

    def test_tool_definition_to_openai_schema_normalizes(self) -> None:
        tool = ToolDefinition(
            name="test_tool",
            description="A test tool",
            input_schema={
                "type": "object",
                "properties": {
                    "top_k": {"type": "integer", "description": "result count"},
                },
                "required": ["top_k"],
            },
        )
        schema = tool.to_openai_schema()
        params = schema["function"]["parameters"]
        assert params["properties"]["top_k"]["type"] == "number"

    def test_array_items_normalized(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                },
            },
        }
        result = _normalize_schema_types(schema)
        assert result["properties"]["ids"]["items"]["type"] == "number"
