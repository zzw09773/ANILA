"""Tests for QueryEngine — turn loop, budget tracking, diminishing returns, max turns."""

from __future__ import annotations

import pytest

from anila_core.engine.budget_tracker import (
    BudgetTracker,
    DIMINISHING_THRESHOLD,
    check_token_budget,
)
from anila_core.engine.query_engine import QueryConfig, QueryEngine
from anila_core.models.message import UserMessage
from anila_core.models.tool import ToolDefinition
from anila_core.providers.mock import MockProvider, ScriptedResponse, ScriptedToolCall
from anila_core.router.tool_router import ToolRegistry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_engine(
    script: list[ScriptedResponse],
    tools: list[ToolDefinition] | None = None,
    max_turns: int = 10,
    budget_tokens: int | None = None,
) -> tuple[QueryEngine, MockProvider]:
    provider = MockProvider(script)
    registry = ToolRegistry()
    if tools:
        for t in tools:
            registry.register(t)

    config = QueryConfig(
        max_turns=max_turns,
        model="test-model",
        budget_tokens=budget_tokens,
    )
    engine = QueryEngine(provider, registry, config)
    return engine, provider


def make_echo_tool(name: str = "echo") -> ToolDefinition:
    async def impl(input: dict, **_):
        return f"echoed:{input.get('text', '')}"

    return ToolDefinition(
        name=name,
        description="Echo",
        input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
        implementation=impl,
    )


# ---------------------------------------------------------------------------
# BudgetTracker unit tests
# ---------------------------------------------------------------------------

class TestBudgetTracker:
    def test_no_budget_returns_stop(self) -> None:
        tracker = BudgetTracker()
        decision = check_token_budget(tracker, None, None, 1000)
        assert decision.action == "stop"

    def test_zero_budget_returns_stop(self) -> None:
        tracker = BudgetTracker()
        decision = check_token_budget(tracker, None, 0, 1000)
        assert decision.action == "stop"

    def test_agent_id_returns_stop(self) -> None:
        tracker = BudgetTracker()
        decision = check_token_budget(tracker, "some-agent-id", 10000, 1000)
        assert decision.action == "stop"

    def test_under_threshold_continues(self) -> None:
        tracker = BudgetTracker()
        budget = 10_000
        # 50% usage -> well under 90% threshold
        decision = check_token_budget(tracker, None, budget, 5000)
        assert decision.action == "continue"
        assert "%" in decision.nudge_message
        assert tracker.continuation_count == 1

    def test_over_threshold_stops(self) -> None:
        tracker = BudgetTracker()
        budget = 10_000
        # 95% usage -> over threshold
        decision = check_token_budget(tracker, None, budget, 9500)
        assert decision.action == "stop"

    def test_diminishing_returns_stops(self) -> None:
        tracker = BudgetTracker()
        tracker.continuation_count = 3
        tracker.last_delta_tokens = DIMINISHING_THRESHOLD - 1

        budget = 100_000
        # Small delta from last check
        tracker.last_global_turn_tokens = 1000
        decision = check_token_budget(tracker, None, budget, 1400)  # delta=400 < 500
        assert decision.action == "stop"
        assert decision.diminishing_returns

    def test_continuation_count_increments(self) -> None:
        tracker = BudgetTracker()
        budget = 10_000
        check_token_budget(tracker, None, budget, 1000)
        assert tracker.continuation_count == 1
        check_token_budget(tracker, None, budget, 2000)
        assert tracker.continuation_count == 2

    def test_nudge_message_contains_percentage(self) -> None:
        tracker = BudgetTracker()
        decision = check_token_budget(tracker, None, 10_000, 4000)
        assert decision.action == "continue"
        assert "40%" in decision.nudge_message


# ---------------------------------------------------------------------------
# QueryEngine basic turn
# ---------------------------------------------------------------------------

class TestQueryEngineBasicTurn:
    @pytest.mark.asyncio
    async def test_simple_text_response(self) -> None:
        script = [ScriptedResponse(text="Hello, world!")]
        engine, provider = make_engine(script)
        result = await engine.run([UserMessage(content="Hi")])
        assert provider.call_count == 1
        from anila_core.models.message import AssistantMessage
        last = result.messages[-1]
        assert isinstance(last, AssistantMessage)
        assert "Hello, world!" in last.get_text()

    @pytest.mark.asyncio
    async def test_tool_call_and_result(self) -> None:
        echo_tool = make_echo_tool()
        script = [
            ScriptedResponse(
                tool_calls=[ScriptedToolCall(name="echo", input={"text": "test"})],
                finish_reason="tool_use",
            ),
            ScriptedResponse(text="Done after echo."),
        ]
        engine, provider = make_engine(script, tools=[echo_tool])
        result = await engine.run([UserMessage(content="echo something")])
        # Two API calls: one for tool call, one after result
        assert provider.call_count == 2
        assert result.turn_count >= 1

    @pytest.mark.asyncio
    async def test_max_turns_stops_loop(self) -> None:
        # Infinite tool calls script
        script = [
            ScriptedResponse(
                tool_calls=[ScriptedToolCall(name="echo", input={"text": "x"})],
                finish_reason="tool_use",
            )
        ] * 20  # more than max_turns
        script.append(ScriptedResponse(text="Final"))

        engine, provider = make_engine(script, tools=[make_echo_tool()], max_turns=3)
        result = await engine.run([UserMessage(content="start")])
        assert result.turn_count <= 3
        assert result.stop_reason == "max_turns"

    @pytest.mark.asyncio
    async def test_usage_accumulated(self) -> None:
        from anila_core.models.message import Usage
        script = [
            ScriptedResponse(
                text="Hi",
                usage=Usage(input_tokens=100, output_tokens=50),
            )
        ]
        engine, provider = make_engine(script)
        result = await engine.run([UserMessage(content="test")])
        assert result.total_usage.input_tokens >= 100
        assert result.total_usage.output_tokens >= 50

    @pytest.mark.asyncio
    async def test_budget_stops_loop(self) -> None:
        from anila_core.models.message import Usage
        # Each response uses tokens that will approach budget
        script = [
            ScriptedResponse(
                text="Step " + str(i),
                usage=Usage(input_tokens=100, output_tokens=1000),
            )
            for i in range(20)
        ]
        engine, provider = make_engine(script, budget_tokens=2000, max_turns=20)
        result = await engine.run([UserMessage(content="go")])
        assert result.turn_count < 10

    @pytest.mark.asyncio
    async def test_post_turn_hook_fires(self) -> None:
        hook_results: list[str] = []

        async def my_hook(turn_result) -> None:
            hook_results.append("fired")

        script = [ScriptedResponse(text="Done")]
        engine, _ = make_engine(script)
        engine.add_post_turn_hook(my_hook)
        await engine.run([UserMessage(content="test")])
        await engine.drain_hooks()
        assert "fired" in hook_results

    @pytest.mark.asyncio
    async def test_stream_delta_callback(self) -> None:
        received: list[str] = []

        async def on_delta(delta) -> None:
            if delta.type == "text" and delta.text:
                received.append(delta.text)

        script = [ScriptedResponse(text="streaming text")]
        engine, _ = make_engine(script)
        await engine.run([UserMessage(content="test")], on_stream_delta=on_delta)
        assert "streaming text" in received
