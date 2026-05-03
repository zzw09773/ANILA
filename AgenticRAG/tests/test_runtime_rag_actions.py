"""Tests for runtime.rag_actions — wrapping AgenticRAG ToolDefinition as framework Action."""

from __future__ import annotations

from typing import Any

import pytest

from agentic_rag.runtime.framework.action import ActionContext, ActionKind, ActionResult, SideEffectClass
from agentic_rag.runtime.framework.exceptions import UserError

from agentic_rag.models.tool import ToolDefinition as RagToolDefinition, ToolSafety
from agentic_rag.runtime.bridge.rag_actions import wrap_tool_definition


def _ctx(params: dict | None = None) -> ActionContext:
    return ActionContext(
        run_id="r1",
        agent_name="test-agent",
        params=params or {},
        history=(),
    )


def _build_def(impl: Any, *, safety: ToolSafety = ToolSafety.READ_ONLY) -> RagToolDefinition:
    return RagToolDefinition(
        name="t",
        description="d",
        input_schema={"type": "object", "properties": {}},
        safety=safety,
        implementation=impl,
    )


# ── Wrap behaviour ──────────────────────────────────────────────────────


def test_wrap_rejects_definition_without_implementation() -> None:
    td = RagToolDefinition(
        name="t", description="d", input_schema={"type": "object"}, implementation=None
    )
    with pytest.raises(UserError, match="no implementation"):
        wrap_tool_definition(td)


def test_wrap_carries_name_description_schema_through() -> None:
    async def impl(params: dict) -> dict:
        return {"ok": True}

    td = RagToolDefinition(
        name="my_tool",
        description="does things",
        input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
        implementation=impl,
    )
    action = wrap_tool_definition(td)
    assert action.name == "my_tool"
    assert action.description == "does things"
    assert action.kind is ActionKind.SYNC_TOOL
    assert action.input_schema["properties"]["q"]["type"] == "string"


def test_wrap_maps_safety_to_side_effect_class() -> None:
    async def impl(params: dict) -> dict:
        return {}

    assert (
        wrap_tool_definition(_build_def(impl, safety=ToolSafety.READ_ONLY)).side_effect_class
        is SideEffectClass.PURE
    )
    assert (
        wrap_tool_definition(
            _build_def(impl, safety=ToolSafety.CONCURRENCY_SAFE)
        ).side_effect_class
        is SideEffectClass.LOCAL
    )
    assert (
        wrap_tool_definition(
            _build_def(impl, safety=ToolSafety.DESTRUCTIVE)
        ).side_effect_class
        is SideEffectClass.IRREVERSIBLE
    )


# ── Handler behaviour ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_wrapped_handler_returns_dict_as_action_output() -> None:
    async def impl(params: dict) -> dict:
        return {"results": [1, 2, 3]}

    action = wrap_tool_definition(_build_def(impl))
    result = await action.handler(_ctx({"q": "x"}))
    assert isinstance(result, ActionResult)
    assert result.is_error is False
    assert result.output == {"results": [1, 2, 3]}


@pytest.mark.asyncio
async def test_wrapped_handler_translates_error_dict_to_action_error() -> None:
    """AgenticRAG tool convention: returning {"error": "..."} signals failure."""

    async def impl(params: dict) -> dict:
        return {"error": "bad input"}

    action = wrap_tool_definition(_build_def(impl))
    result = await action.handler(_ctx())
    assert result.is_error is True
    assert result.error == "bad input"


@pytest.mark.asyncio
async def test_wrapped_handler_catches_implementation_exception() -> None:
    """Handler exceptions become ActionResult errors so the runner doesn't crash."""

    async def impl(params: dict) -> dict:
        raise RuntimeError("disk on fire")

    action = wrap_tool_definition(_build_def(impl))
    result = await action.handler(_ctx())
    assert result.is_error is True
    assert "disk on fire" in result.error
    assert "RuntimeError" in result.error


@pytest.mark.asyncio
async def test_wrapped_handler_rejects_non_dict_return() -> None:
    """AgenticRAG tools always return dict; if one returns something else,
    the wrapper surfaces it as a clean error rather than letting it propagate
    through the runner as a confusing serialisation crash."""

    async def impl(params: dict) -> Any:
        return "just a string"

    action = wrap_tool_definition(_build_def(impl))
    result = await action.handler(_ctx())
    assert result.is_error is True
    assert "expected dict" in result.error


@pytest.mark.asyncio
async def test_wrapped_handler_passes_params_through() -> None:
    """Handler receives ActionContext.params verbatim — the wrapper does
    NOT pre-validate / coerce."""

    received = {}

    async def impl(params: dict) -> dict:
        received.update(params)
        return {"echoed": params}

    action = wrap_tool_definition(_build_def(impl))
    await action.handler(_ctx({"a": 1, "b": "two"}))
    assert received == {"a": 1, "b": "two"}
