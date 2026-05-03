"""Tests for the ``ask_user`` tool."""

from __future__ import annotations

import pytest

from anila_core.models.interrupt import InterruptItem
from anila_core.models.tool import ToolSafety
from anila_core.tools.ask_user import (
    INPUT_SCHEMA,
    TOOL_NAME,
    _ask_user_impl,
    ask_user_tool,
)


def test_tool_definition_basic_fields() -> None:
    tool = ask_user_tool()
    assert tool.name == TOOL_NAME == "ask_user"
    assert tool.safety == ToolSafety.READ_ONLY
    assert tool.implementation is _ask_user_impl
    assert "options" in INPUT_SCHEMA["properties"]
    assert INPUT_SCHEMA["required"] == ["question", "options"]


@pytest.mark.asyncio
async def test_returns_interrupt_with_normalised_payload() -> None:
    result = await _ask_user_impl(
        {
            "question": "what color?",
            "options": [
                {"label": "blue"},
                {"label": "red", "value": "r", "description": "warm"},
            ],
        }
    )
    assert isinstance(result, InterruptItem)
    assert result.kind == "ask_user"
    payload = result.payload
    assert payload["question"] == "what color?"
    assert payload["multi_select"] is False
    assert payload["allow_other"] is True
    assert payload["options"] == [
        {"label": "blue", "value": "blue", "description": ""},
        {"label": "red", "value": "r", "description": "warm"},
    ]


@pytest.mark.asyncio
async def test_multi_select_propagates() -> None:
    result = await _ask_user_impl(
        {
            "question": "?",
            "options": [{"label": "a"}, {"label": "b"}],
            "multi_select": True,
        }
    )
    assert result.payload["multi_select"] is True


@pytest.mark.asyncio
async def test_skips_invalid_options() -> None:
    result = await _ask_user_impl(
        {
            "question": "?",
            "options": [
                {"label": "ok"},
                "string-instead-of-dict",  # invalid
                {},  # missing label
                {"label": "  "},  # blank label
            ],
        }
    )
    assert [o["label"] for o in result.payload["options"]] == ["ok"]
