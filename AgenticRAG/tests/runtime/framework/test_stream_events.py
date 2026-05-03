"""Tests for Runner.stream() async generator + StreamEvent types (A3)."""

from __future__ import annotations

import asyncio

import pytest

from agentic_rag.runtime.framework import (
    Action,
    ActionContext,
    ActionKind,
    ActionResult,
    Agent,
    ChatCompletionResponse,
    FinishReason,
    HandoffEvent,
    MessageDeltaEvent,
    Message,
    RunCompletedEvent,
    RunErrorEvent,
    Runner,
    ToolCall,
    ToolCallFinishedEvent,
    ToolCallStartedEvent,
    Usage,
    UsageUpdateEvent,
)


class _ScriptedProvider:
    def __init__(self, responses):
        self._scripted = list(responses)

    async def chat_completion(self, messages, tools=None, *, model, stream=False, **kw):
        return self._scripted.pop(0)

    async def embeddings(self, texts, *, model, **kw):
        raise NotImplementedError


def _resp(text="", tool_calls=(), finish=FinishReason.STOP, tokens=(10, 5)):
    return ChatCompletionResponse(
        message=Message.assistant(content=text, tool_calls=tool_calls),
        usage=Usage(
            requests=1,
            input_tokens=tokens[0],
            output_tokens=tokens[1],
            total_tokens=tokens[0] + tokens[1],
        ),
        finish_reason=finish,
    )


def _agent(provider, *, actions=()):
    return Agent(
        name="a",
        instructions="",
        provider=provider,
        model="m",
        actions=tuple(actions),
    )


# ── Single-turn stream ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stream_single_turn_emits_message_then_completed() -> None:
    provider = _ScriptedProvider([_resp(text="hello")])
    agent = _agent(provider)

    events = [e async for e in Runner().stream(agent, "hi")]

    types = [type(e).__name__ for e in events]
    assert types == ["MessageDeltaEvent", "UsageUpdateEvent", "RunCompletedEvent"]
    assert isinstance(events[0], MessageDeltaEvent)
    assert events[0].message.content == "hello"
    assert events[0].turn_index == 0
    completed = events[-1]
    assert isinstance(completed, RunCompletedEvent)
    assert completed.final_output == "hello"
    assert completed.turns == 1


# ── Multi-turn with tool dispatch ─────────────────────────────────────


@pytest.mark.asyncio
async def test_stream_tool_call_emits_started_finished_then_final() -> None:
    async def search(ctx: ActionContext) -> ActionResult:
        return ActionResult(output="hits")

    action = Action(
        name="search", description="", kind=ActionKind.SYNC_TOOL, handler=search
    )
    tc = ToolCall(id="c1", name="search", arguments="{}")
    provider = _ScriptedProvider(
        [
            _resp(tool_calls=(tc,), finish=FinishReason.TOOL_CALLS),
            _resp(text="done"),
        ]
    )
    agent = _agent(provider, actions=[action])

    events = [e async for e in Runner().stream(agent, "go")]

    # Expect: msg(turn 0) usage, tool_started, tool_finished, msg(turn 1) usage, completed
    starts = [e for e in events if isinstance(e, ToolCallStartedEvent)]
    fins = [e for e in events if isinstance(e, ToolCallFinishedEvent)]
    assert len(starts) == 1 and starts[0].call.name == "search"
    assert len(fins) == 1 and fins[0].result.output == "hits"
    completed = [e for e in events if isinstance(e, RunCompletedEvent)]
    assert len(completed) == 1
    assert completed[0].final_output == "done"


# ── Handoff event ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stream_emits_handoff_event_when_active_agent_changes() -> None:
    verifier_provider = _ScriptedProvider([_resp(text="verified")])
    verifier = Agent(
        name="verifier", instructions="", provider=verifier_provider, model="m"
    )

    handoff_call = ToolCall(
        id="c1",
        name="transfer_to_verifier",
        arguments='{"reason": "needs check"}',
    )
    primary_provider = _ScriptedProvider(
        [_resp(tool_calls=(handoff_call,), finish=FinishReason.TOOL_CALLS)]
    )
    primary = Agent(
        name="primary",
        instructions="",
        provider=primary_provider,
        model="m",
        handoffs=[verifier],
    )

    events = [e async for e in Runner().stream(primary, "x")]

    handoffs = [e for e in events if isinstance(e, HandoffEvent)]
    assert len(handoffs) == 1
    assert handoffs[0].from_agent == "primary"
    assert handoffs[0].to_agent == "verifier"
    assert handoffs[0].reason == "needs check"


# ── Cancellation surfaces as RunErrorEvent ────────────────────────────


@pytest.mark.asyncio
async def test_stream_cancellation_emits_error_event_not_exception() -> None:
    """Raw stream() consumers see error events, not exceptions."""
    signal = asyncio.Event()
    signal.set()
    provider = _ScriptedProvider([_resp(text="never reached")])
    agent = _agent(provider)

    events = [
        e async for e in Runner().stream(agent, "hi", cancel_signal=signal)
    ]
    error_events = [e for e in events if isinstance(e, RunErrorEvent)]
    assert len(error_events) == 1
    assert error_events[0].error_type == "RunCancelled"
    assert error_events[0].metadata["reason"] == "signal"


# ── run() drains stream and returns RunResult ─────────────────────────


@pytest.mark.asyncio
async def test_run_still_returns_run_result_after_refactor() -> None:
    """Compatibility: existing run() callers see no behaviour change."""
    provider = _ScriptedProvider([_resp(text="ok")])
    agent = _agent(provider)
    result = await Runner().run(agent, "hi")
    assert result.final_output == "ok"
    assert result.turns == 1


# ── Usage delta + cumulative both populated ──────────────────────────


@pytest.mark.asyncio
async def test_usage_event_carries_delta_and_cumulative() -> None:
    async def t(ctx):
        return ActionResult(output="ok")

    action = Action(name="t", description="", kind=ActionKind.SYNC_TOOL, handler=t)
    tc = ToolCall(id="c1", name="t", arguments="{}")
    provider = _ScriptedProvider(
        [
            _resp(tool_calls=(tc,), finish=FinishReason.TOOL_CALLS, tokens=(100, 20)),
            _resp(text="done", tokens=(50, 10)),
        ]
    )
    agent = _agent(provider, actions=[action])

    usage_events = [
        e
        async for e in Runner().stream(agent, "go")
        if isinstance(e, UsageUpdateEvent)
    ]
    assert len(usage_events) == 2
    assert usage_events[0].delta.input_tokens == 100
    assert usage_events[0].cumulative.input_tokens == 100
    assert usage_events[1].delta.input_tokens == 50
    assert usage_events[1].cumulative.input_tokens == 150
