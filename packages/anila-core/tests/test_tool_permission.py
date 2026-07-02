"""Tests for per-tool permission policy (Sprint 11 PR 3)."""

from __future__ import annotations

import pytest

from anila_core.engine.approvals import RunPaused, resume_tool_approval
from anila_core.engine.query_engine import QueryConfig, QueryEngine
from anila_core.memory import MemorySession
from anila_core.models.message import ToolCall, UserMessage
from anila_core.models.tool import ToolDefinition, ToolPermission, ToolSafety
from anila_core.providers.mock import (
    MockProvider,
    ScriptedResponse,
    ScriptedToolCall,
)
from anila_core.router.tool_router import ToolRegistry


# ---------------------------------------------------------------------------
# ToolPermission enum + ToolDefinition default
# ---------------------------------------------------------------------------


def test_default_permission_is_allow() -> None:
    tool = ToolDefinition(
        name="x", description="d", input_schema={"type": "object"},
    )
    assert tool.permission == ToolPermission.ALLOW


def test_can_construct_with_each_permission() -> None:
    for perm in (ToolPermission.ALLOW, ToolPermission.DENY, ToolPermission.ASK):
        tool = ToolDefinition(
            name="x", description="d", input_schema={"type": "object"},
            permission=perm,
        )
        assert tool.permission == perm


# ---------------------------------------------------------------------------
# ToolRegistry permission gates
# ---------------------------------------------------------------------------


def _make_tool(name: str, perm: ToolPermission) -> ToolDefinition:
    async def impl(input, **_):
        return f"ran:{input.get('text', '')}"
    return ToolDefinition(
        name=name, description="t",
        input_schema={"type": "object"},
        safety=ToolSafety.READ_ONLY,
        permission=perm,
        implementation=impl,
    )


@pytest.mark.asyncio
async def test_allow_runs_normally() -> None:
    registry = ToolRegistry()
    registry.register(_make_tool("a", ToolPermission.ALLOW))
    result = await registry.execute(ToolCall(name="a", input={"text": "x"}))
    assert result.is_error is False
    assert result.content == "ran:x"


@pytest.mark.asyncio
async def test_deny_returns_error_result() -> None:
    registry = ToolRegistry()
    registry.register(_make_tool("d", ToolPermission.DENY))
    result = await registry.execute(ToolCall(name="d", input={"text": "x"}))
    assert result.is_error is True
    assert "DENIED" in result.content


@pytest.mark.asyncio
async def test_ask_returns_interrupt_in_tool_result() -> None:
    registry = ToolRegistry()
    registry.register(_make_tool("k", ToolPermission.ASK))
    result = await registry.execute(
        ToolCall(name="k", input={"text": "secret"}, id="c-k")
    )
    assert result.interrupt is not None
    assert result.interrupt.kind == "tool_approval"
    payload = result.interrupt.payload
    assert payload["tool_name"] == "k"
    assert payload["tool_call_id"] == "c-k"
    assert payload["tool_input"] == {"text": "secret"}


@pytest.mark.asyncio
async def test_bypass_gates_skips_deny() -> None:
    registry = ToolRegistry()
    registry.register(_make_tool("d", ToolPermission.DENY))
    result = await registry.execute(
        ToolCall(name="d", input={"text": "x"}),
        bypass_gates=True,
    )
    assert result.is_error is False
    assert result.content == "ran:x"


@pytest.mark.asyncio
async def test_bypass_gates_skips_ask_and_runs_directly() -> None:
    registry = ToolRegistry()
    registry.register(_make_tool("k", ToolPermission.ASK))
    result = await registry.execute(
        ToolCall(name="k", input={"text": "x"}),
        bypass_gates=True,
    )
    assert result.is_error is False
    assert result.interrupt is None
    assert result.content == "ran:x"


# ---------------------------------------------------------------------------
# Resume — approve / deny round trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_engine_pauses_on_ask_tool() -> None:
    sess = MemorySession("s1")
    registry = ToolRegistry()
    registry.register(_make_tool("k", ToolPermission.ASK))
    provider = MockProvider(
        [
            ScriptedResponse(
                tool_calls=[
                    ScriptedToolCall(
                        name="k", input={"text": "z"}, tool_id="c-k"
                    )
                ],
                finish_reason="tool_use",
            )
        ]
    )
    engine = QueryEngine(
        provider, registry, QueryConfig(model="m"), session=sess
    )
    with pytest.raises(RunPaused) as excinfo:
        await engine.run([UserMessage(content="hi")])
    assert excinfo.value.kind == "tool_approval"


@pytest.mark.asyncio
async def test_resume_approve_executes_tool_then_continues() -> None:
    sess = MemorySession("s1")
    registry = ToolRegistry()
    registry.register(_make_tool("k", ToolPermission.ASK))
    provider = MockProvider(
        [
            ScriptedResponse(
                tool_calls=[
                    ScriptedToolCall(
                        name="k", input={"text": "z"}, tool_id="c-k"
                    )
                ],
                finish_reason="tool_use",
            ),
            # After resume, model sees tool_result and produces final answer.
            ScriptedResponse(text="all clear", finish_reason="end_turn"),
        ]
    )
    engine = QueryEngine(
        provider, registry, QueryConfig(model="m"), session=sess
    )
    with pytest.raises(RunPaused) as excinfo:
        await engine.run([UserMessage(content="go")])
    interrupt_id = excinfo.value.interrupt_id

    result = await engine.resume_from_interrupt(
        interrupt_id, {"approved": True}
    )
    assert "all clear" in result.messages[-1].get_text()


@pytest.mark.asyncio
async def test_resume_deny_returns_error_tool_result_to_model() -> None:
    sess = MemorySession("s1")
    registry = ToolRegistry()
    registry.register(_make_tool("k", ToolPermission.ASK))
    provider = MockProvider(
        [
            ScriptedResponse(
                tool_calls=[
                    ScriptedToolCall(
                        name="k", input={"text": "z"}, tool_id="c-k"
                    )
                ],
                finish_reason="tool_use",
            ),
            ScriptedResponse(
                text="ok, abandoning that path", finish_reason="end_turn"
            ),
        ]
    )
    engine = QueryEngine(
        provider, registry, QueryConfig(model="m"), session=sess
    )
    with pytest.raises(RunPaused) as excinfo:
        await engine.run([UserMessage(content="go")])

    await engine.resume_from_interrupt(
        excinfo.value.interrupt_id,
        {"approved": False, "comment": "too risky"},
    )
    # The provider was called twice: original + resume turn. The 2nd
    # turn's input must include a tool_result with is_error=True.
    second_call = provider.requests[1]
    last_user = second_call.messages[-1]
    blocks = last_user.content
    assert isinstance(blocks, list)
    deny_block = next(
        b for b in blocks if b.get("tool_use_id") == "c-k"
    )
    assert deny_block.get("is_error") is True
    assert "denied" in deny_block["content"]
    assert "too risky" in deny_block["content"]


@pytest.mark.asyncio
async def test_resume_string_yes_counts_as_approve() -> None:
    sess = MemorySession("s1")
    registry = ToolRegistry()
    registry.register(_make_tool("k", ToolPermission.ASK))
    provider = MockProvider(
        [
            ScriptedResponse(
                tool_calls=[
                    ScriptedToolCall(
                        name="k", input={"text": "z"}, tool_id="c-k"
                    )
                ],
                finish_reason="tool_use",
            ),
            ScriptedResponse(text="done", finish_reason="end_turn"),
        ]
    )
    engine = QueryEngine(
        provider, registry, QueryConfig(model="m"), session=sess
    )
    with pytest.raises(RunPaused) as excinfo:
        await engine.run([UserMessage(content="go")])
    await engine.resume_from_interrupt(excinfo.value.interrupt_id, "yes")
    second = provider.requests[1]
    last_user = second.messages[-1]
    blocks = last_user.content
    assert isinstance(blocks, list)
    block = next(b for b in blocks if b.get("tool_use_id") == "c-k")
    # On approve the tool actually ran → content is the impl's output.
    assert block["content"] == "ran:z"
    assert "is_error" not in block


@pytest.mark.asyncio
async def test_resume_tool_approval_helper_rejects_wrong_kind() -> None:
    sess = MemorySession("s1")
    registry = ToolRegistry()
    from anila_core.memory.session import InterruptRecord

    await sess.push_interrupt(
        InterruptRecord(id="int-x", kind="ask_user", payload={
            "tool_call": {"id": "c", "name": "n", "input": {}},
        })
    )
    with pytest.raises(ValueError, match="expected kind=tool_approval"):
        await resume_tool_approval(
            sess, registry, "int-x", approved=True
        )


@pytest.mark.asyncio
async def test_resume_tool_approval_unknown_id_raises() -> None:
    sess = MemorySession("s1")
    registry = ToolRegistry()
    with pytest.raises(ValueError, match="not found"):
        await resume_tool_approval(
            sess, registry, "ghost", approved=True
        )
