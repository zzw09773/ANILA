"""Tests for ``enter_plan_mode`` / ``exit_plan_mode`` tools and the
plan-mode gate in :class:`ToolRegistry`."""

from __future__ import annotations

import pytest

from anila_core.context.agent_context import (
    AgentContext,
    set_current_context,
)
from anila_core.models.interrupt import InterruptItem
from anila_core.models.message import ToolCall
from anila_core.models.tool import ToolDefinition, ToolSafety
from anila_core.router.tool_router import ToolRegistry
from anila_core.tools.plan_mode import (
    _enter_plan_mode_impl,
    _exit_plan_mode_impl,
    enter_plan_mode_tool,
    exit_plan_mode_tool,
    is_plan_mode_active,
)


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------


def test_enter_tool_definition() -> None:
    tool = enter_plan_mode_tool()
    assert tool.name == "enter_plan_mode"
    assert tool.safety == ToolSafety.READ_ONLY
    assert tool.input_schema["properties"] == {}


def test_exit_tool_definition() -> None:
    tool = exit_plan_mode_tool()
    assert tool.name == "exit_plan_mode"
    assert tool.safety == ToolSafety.READ_ONLY
    assert "plan" in tool.input_schema["required"]


# ---------------------------------------------------------------------------
# enter_plan_mode behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enter_plan_mode_sets_context_flag() -> None:
    ctx = AgentContext()
    set_current_context(ctx)
    assert ctx.plan_mode is False

    msg = await _enter_plan_mode_impl({})
    assert msg == "plan_mode_entered"
    assert ctx.plan_mode is True
    assert is_plan_mode_active() is True


@pytest.mark.asyncio
async def test_enter_plan_mode_handles_no_context_gracefully() -> None:
    # No context bound → returns descriptive note, doesn't crash.
    import contextvars

    new_ctx = contextvars.copy_context()
    msg = await new_ctx.run(_enter_plan_mode_impl_runner)
    assert msg.startswith("plan_mode_entered")
    assert "no AgentContext active" in msg


async def _enter_plan_mode_impl_runner() -> str:
    # Helper that runs in an isolated context with no current_context set.
    from anila_core.context.agent_context import _current_context

    _current_context.set(None)  # type: ignore[arg-type]
    return await _enter_plan_mode_impl({})


# ---------------------------------------------------------------------------
# exit_plan_mode behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exit_plan_mode_returns_interrupt() -> None:
    result = await _exit_plan_mode_impl(
        {"plan": "1. fix bug\n2. add tests"}
    )
    assert isinstance(result, InterruptItem)
    assert result.kind == "plan"
    assert result.payload == {"plan": "1. fix bug\n2. add tests"}


# ---------------------------------------------------------------------------
# Plan-mode gate in ToolRegistry
# ---------------------------------------------------------------------------


def _make_destructive_tool() -> ToolDefinition:
    async def impl(input: dict, **_):
        return "wrote file"

    return ToolDefinition(
        name="file_write",
        description="Write to disk.",
        input_schema={"type": "object"},
        safety=ToolSafety.DESTRUCTIVE,
        implementation=impl,
    )


def _make_read_only_tool() -> ToolDefinition:
    async def impl(input: dict, **_):
        return "ok"

    return ToolDefinition(
        name="file_read",
        description="Read from disk.",
        input_schema={"type": "object"},
        safety=ToolSafety.READ_ONLY,
        implementation=impl,
    )


@pytest.mark.asyncio
async def test_destructive_tool_blocked_in_plan_mode() -> None:
    ctx = AgentContext()
    ctx.plan_mode = True
    set_current_context(ctx)

    registry = ToolRegistry()
    registry.register(_make_destructive_tool())
    result = await registry.execute(
        ToolCall(name="file_write", input={})
    )
    assert result.is_error is True
    assert "plan mode" in result.content
    assert "exit_plan_mode" in result.content


@pytest.mark.asyncio
async def test_read_only_tool_allowed_in_plan_mode() -> None:
    ctx = AgentContext()
    ctx.plan_mode = True
    set_current_context(ctx)

    registry = ToolRegistry()
    registry.register(_make_read_only_tool())
    result = await registry.execute(
        ToolCall(name="file_read", input={})
    )
    assert result.is_error is False
    assert result.content == "ok"


@pytest.mark.asyncio
async def test_destructive_tool_allowed_when_not_in_plan_mode() -> None:
    ctx = AgentContext()
    ctx.plan_mode = False
    set_current_context(ctx)

    registry = ToolRegistry()
    registry.register(_make_destructive_tool())
    result = await registry.execute(
        ToolCall(name="file_write", input={})
    )
    assert result.is_error is False
    assert result.content == "wrote file"


@pytest.mark.asyncio
async def test_destructive_tool_allowed_when_no_context() -> None:
    """No AgentContext bound → gate doesn't fire (defensive default)."""
    import contextvars

    isolated = contextvars.copy_context()

    async def runner():
        from anila_core.context.agent_context import _current_context

        _current_context.set(None)  # type: ignore[arg-type]
        registry = ToolRegistry()
        registry.register(_make_destructive_tool())
        return await registry.execute(ToolCall(name="file_write", input={}))

    result = await isolated.run(runner)
    assert result.is_error is False
    assert result.content == "wrote file"
