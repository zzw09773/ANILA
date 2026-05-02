"""Tests for Runner extensions added in Phase A:
A1 cancellation, A2 correlation, A4 input validation, A5 structured output.
"""

from __future__ import annotations

import asyncio

import pytest
from pydantic import BaseModel, Field

from agentic_rag.runtime.framework import (
    Action,
    ActionContext,
    ActionKind,
    ActionResult,
    Agent,
    ChatCompletionResponse,
    FinishReason,
    Message,
    Runner,
    ToolCall,
    Usage,
)
from agentic_rag.runtime.framework.exceptions import (
    OutputValidationError,
    RunCancelled,
)
from agentic_rag.runtime.framework.runner import (
    _strip_json_fence,
    _validate_structured_output,
    _validate_tool_input,
)


# ── shared scaffolding ────────────────────────────────────────────────


class _ScriptedProvider:
    def __init__(self, responses):
        self._scripted = list(responses)

    async def chat_completion(self, messages, tools=None, *, model, stream=False, **kw):
        if not self._scripted:
            raise AssertionError("provider exhausted")
        return self._scripted.pop(0)

    async def embeddings(self, texts, *, model, **kw):
        raise NotImplementedError


def _resp(text="", tool_calls=(), finish=FinishReason.STOP):
    return ChatCompletionResponse(
        message=Message.assistant(content=text, tool_calls=tool_calls),
        usage=Usage(requests=1, input_tokens=10, output_tokens=5, total_tokens=15),
        finish_reason=finish,
    )


async def _ok_handler(ctx: ActionContext) -> ActionResult:
    return ActionResult(output={"params": dict(ctx.params)})


def _agent(provider, *, actions=(), output_type=None):
    return Agent(
        name="a",
        instructions="",
        provider=provider,
        model="m",
        actions=tuple(actions),
        output_type=output_type,
    )


# ── A1 cancellation ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_signal_aborts_at_top_of_loop() -> None:
    """Caller-set Event raises RunCancelled before next LLM call."""
    signal = asyncio.Event()
    signal.set()  # already cancelled before run starts

    provider = _ScriptedProvider([_resp(text="should not be reached")])
    agent = _agent(provider)

    with pytest.raises(RunCancelled) as exc_info:
        await Runner().run(agent, "hi", cancel_signal=signal)
    assert exc_info.value.reason == "signal"
    assert exc_info.value.turns_completed == 0


@pytest.mark.asyncio
async def test_deadline_aborts_before_next_turn() -> None:
    """deadline_seconds in the past → cancellation on first check."""
    provider = _ScriptedProvider([_resp(text="should not be reached")])
    agent = _agent(provider)

    with pytest.raises(RunCancelled) as exc_info:
        # Negative deadline ⇒ already exceeded.
        await Runner().run(agent, "hi", deadline_seconds=-0.001)
    assert exc_info.value.reason == "deadline"


@pytest.mark.asyncio
async def test_cancel_signal_fires_between_tool_dispatches() -> None:
    """Signal set during tool dispatch loop aborts before the next tool."""
    signal = asyncio.Event()

    async def first_tool(ctx: ActionContext) -> ActionResult:
        signal.set()  # cancellation requested mid-dispatch
        return ActionResult(output="done")

    action = Action(
        name="t1", description="", kind=ActionKind.SYNC_TOOL, handler=first_tool
    )
    # LLM emits two tool calls in one turn. After t1 sets the signal,
    # the runner should refuse to dispatch t2.
    tcs = (
        ToolCall(id="c1", name="t1", arguments="{}"),
        ToolCall(id="c2", name="t1", arguments="{}"),
    )
    provider = _ScriptedProvider(
        [_resp(tool_calls=tcs, finish=FinishReason.TOOL_CALLS)]
    )
    agent = _agent(provider, actions=[action])

    with pytest.raises(RunCancelled):
        await Runner().run(agent, "go", cancel_signal=signal)


@pytest.mark.asyncio
async def test_cancellation_unset_signal_runs_normally() -> None:
    """An Event that's never set must not interfere with the run."""
    signal = asyncio.Event()
    provider = _ScriptedProvider([_resp(text="ok")])
    agent = _agent(provider)
    result = await Runner().run(agent, "hi", cancel_signal=signal)
    assert result.final_output == "ok"


@pytest.mark.asyncio
async def test_generous_deadline_does_not_cancel() -> None:
    provider = _ScriptedProvider([_resp(text="ok")])
    agent = _agent(provider)
    result = await Runner().run(agent, "hi", deadline_seconds=10.0)
    assert result.final_output == "ok"


# ── A2 correlation ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_correlation_fields_round_trip_to_run_result() -> None:
    provider = _ScriptedProvider([_resp(text="ok")])
    agent = _agent(provider)
    result = await Runner().run(
        agent,
        "hi",
        parent_run_id="parent_123",
        group_id="conv_abc",
        trace_metadata={"customer_id": "cx_42"},
    )
    assert result.parent_run_id == "parent_123"
    assert result.group_id == "conv_abc"
    assert result.trace_metadata == {"customer_id": "cx_42"}


@pytest.mark.asyncio
async def test_correlation_fields_flow_into_action_context_metadata() -> None:
    """Middleware / handlers can read parent_run_id etc. via ctx.metadata."""

    seen: dict = {}

    async def capture_metadata(ctx: ActionContext) -> ActionResult:
        seen.update(ctx.metadata)
        return ActionResult(output="ok")

    action = Action(
        name="t", description="", kind=ActionKind.SYNC_TOOL, handler=capture_metadata
    )
    tc = ToolCall(id="c1", name="t", arguments="{}")
    provider = _ScriptedProvider(
        [
            _resp(tool_calls=(tc,), finish=FinishReason.TOOL_CALLS),
            _resp(text="done"),
        ]
    )
    agent = _agent(provider, actions=[action])

    await Runner().run(
        agent,
        "go",
        parent_run_id="p1",
        group_id="g1",
        trace_metadata={"customer": "x"},
    )
    assert seen["_parent_run_id"] == "p1"
    assert seen["_group_id"] == "g1"
    assert seen["customer"] == "x"


# ── A4 tool input validation ─────────────────────────────────────────


def test_validate_tool_input_passes_empty_schema() -> None:
    assert _validate_tool_input({}, {"q": "x"}) is None
    assert _validate_tool_input(None, {}) is None


def test_validate_tool_input_catches_missing_required() -> None:
    schema = {
        "type": "object",
        "properties": {"q": {"type": "string"}},
        "required": ["q"],
    }
    assert "missing required" in _validate_tool_input(schema, {})
    assert _validate_tool_input(schema, {"q": "rag"}) is None


def test_validate_tool_input_catches_wrong_primitive_type() -> None:
    schema = {
        "type": "object",
        "properties": {"top_k": {"type": "integer"}},
    }
    assert "wrong type" in _validate_tool_input(schema, {"top_k": "5"})
    assert _validate_tool_input(schema, {"top_k": 5}) is None


def test_validate_tool_input_rejects_bool_for_numeric_slot() -> None:
    """Python bool is int subclass; the LLM doesn't actually mean True."""
    schema = {"type": "object", "properties": {"limit": {"type": "integer"}}}
    assert "wrong type" in _validate_tool_input(schema, {"limit": True})


def test_validate_tool_input_blocks_extra_fields_when_strict() -> None:
    schema = {
        "type": "object",
        "properties": {"q": {"type": "string"}},
        "additionalProperties": False,
    }
    assert "unexpected field" in _validate_tool_input(schema, {"q": "x", "extra": 1})
    # Default (additionalProperties not specified) → permissive.
    schema_loose = {"type": "object", "properties": {"q": {"type": "string"}}}
    assert _validate_tool_input(schema_loose, {"q": "x", "extra": 1}) is None


@pytest.mark.asyncio
async def test_runner_surfaces_input_validation_as_recoverable_error() -> None:
    """Invalid args → tool error message back to LLM, no handler run."""

    handler_called = False

    async def search(ctx: ActionContext) -> ActionResult:
        nonlocal handler_called
        handler_called = True
        return ActionResult(output="ok")

    action = Action(
        name="search",
        description="",
        kind=ActionKind.SYNC_TOOL,
        handler=search,
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    )
    # LLM forgets the required ``query`` field.
    tc = ToolCall(id="c1", name="search", arguments="{}")
    provider = _ScriptedProvider(
        [
            _resp(tool_calls=(tc,), finish=FinishReason.TOOL_CALLS),
            _resp(text="recovered"),
        ]
    )
    agent = _agent(provider, actions=[action])

    result = await Runner().run(agent, "go")
    assert handler_called is False
    assert result.final_output == "recovered"
    # Tool message in history carries the validation error.
    tool_msgs = [m for m in result.history if m.role.value == "tool"]
    assert any("[input-validation]" in (m.content or "") for m in tool_msgs)


# ── A5 structured output ─────────────────────────────────────────────


def test_strip_json_fence_handles_markdown() -> None:
    assert _strip_json_fence('```json\n{"a": 1}\n```') == '{"a": 1}'
    assert _strip_json_fence('```\n{"a": 1}\n```') == '{"a": 1}'
    assert _strip_json_fence('{"a": 1}') == '{"a": 1}'  # no fence — passthrough


class _Answer(BaseModel):
    text: str
    confidence: float = Field(ge=0.0, le=1.0)


def test_validate_structured_output_pydantic_v2_path() -> None:
    agent = _agent(
        _ScriptedProvider([]),
        output_type=_Answer,
    )
    parsed = _validate_structured_output(agent, '{"text": "hi", "confidence": 0.9}')
    assert isinstance(parsed, _Answer)
    assert parsed.text == "hi"


def test_validate_structured_output_raises_on_decode_failure() -> None:
    agent = _agent(_ScriptedProvider([]), output_type=_Answer)
    with pytest.raises(OutputValidationError, match="JSON decode"):
        _validate_structured_output(agent, "not json {")


def test_validate_structured_output_raises_on_schema_mismatch() -> None:
    agent = _agent(_ScriptedProvider([]), output_type=_Answer)
    with pytest.raises(OutputValidationError):
        # confidence > 1 violates Field(ge=0, le=1)
        _validate_structured_output(agent, '{"text": "hi", "confidence": 5.0}')


@pytest.mark.asyncio
async def test_runner_returns_parsed_output_when_output_type_set() -> None:
    provider = _ScriptedProvider(
        [_resp(text='```json\n{"text": "hello", "confidence": 0.8}\n```')]
    )
    agent = _agent(provider, output_type=_Answer)
    result = await Runner().run(agent, "hi")
    assert isinstance(result.parsed_output, _Answer)
    assert result.parsed_output.text == "hello"
    assert result.parsed_output.confidence == 0.8


@pytest.mark.asyncio
async def test_runner_no_validation_when_output_type_not_set() -> None:
    provider = _ScriptedProvider([_resp(text="just text")])
    agent = _agent(provider)
    result = await Runner().run(agent, "hi")
    assert result.parsed_output is None
    assert result.final_output == "just text"


@pytest.mark.asyncio
async def test_runner_propagates_output_validation_error() -> None:
    provider = _ScriptedProvider([_resp(text="not json")])
    agent = _agent(provider, output_type=_Answer)
    with pytest.raises(OutputValidationError):
        await Runner().run(agent, "hi")
