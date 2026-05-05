"""Hook bridge tests — exercise PreToolUse / PostToolUse / Stop firing without an LLM."""

from __future__ import annotations

from typing import Any

import pytest

from anila_agent.core.events import EventBus
from anila_agent.core.hooks import (
    HookEvent,
    HookOutput,
    HookRegistry,
    HookSpec,
    PostToolUseInput,
    PreToolUseInput,
    StopInput,
    fire,
)


@pytest.mark.unit
async def test_pre_tool_use_block() -> None:
    async def deny(_payload: PreToolUseInput) -> HookOutput:
        return HookOutput(decision="block", reason="never")

    registry = HookRegistry([HookSpec(event=HookEvent.PRE_TOOL_USE, callback=deny, matcher=".*")])
    bus = EventBus()
    payload = PreToolUseInput(
        tool_name="search_documents",
        tool_input={"query": "x"},
        tool_call_id="c1",
        agent_name="anila",
    )
    result = await fire(registry, HookEvent.PRE_TOOL_USE, payload, tool_name="search_documents", bus=bus)
    assert result.block is True
    assert result.reason == "never"


@pytest.mark.unit
async def test_matcher_filters_by_tool_name() -> None:
    fired: list[str] = []

    async def cb(payload: PreToolUseInput) -> HookOutput:
        fired.append(payload.tool_name)
        return HookOutput()

    registry = HookRegistry(
        [
            HookSpec(event=HookEvent.PRE_TOOL_USE, callback=cb, matcher=r"search_.*"),
            HookSpec(event=HookEvent.PRE_TOOL_USE, callback=cb, matcher=r"write_.*"),
        ]
    )
    payload = PreToolUseInput(
        tool_name="search_documents",
        tool_input={},
        tool_call_id=None,
        agent_name="anila",
    )
    await fire(registry, HookEvent.PRE_TOOL_USE, payload, tool_name="search_documents", bus=EventBus())
    assert fired == ["search_documents"]


@pytest.mark.unit
async def test_additional_context_aggregates() -> None:
    async def a(_p: Any) -> HookOutput:
        return HookOutput(additional_context="alpha")

    async def b(_p: Any) -> HookOutput:
        return HookOutput(additional_context="beta")

    registry = HookRegistry(
        [
            HookSpec(event=HookEvent.POST_TOOL_USE, callback=a),
            HookSpec(event=HookEvent.POST_TOOL_USE, callback=b),
        ]
    )
    payload = PostToolUseInput(
        tool_name="t",
        tool_input={},
        tool_output="ok",
        tool_call_id="c",
        agent_name="anila",
    )
    result = await fire(registry, HookEvent.POST_TOOL_USE, payload, tool_name="t", bus=EventBus())
    assert result.additional_contexts == ["alpha", "beta"]


@pytest.mark.unit
async def test_stop_event_runs_callbacks() -> None:
    fired = []

    async def cb(payload: StopInput) -> HookOutput:
        fired.append(payload.turns_used)
        return HookOutput()

    registry = HookRegistry([HookSpec(event=HookEvent.STOP, callback=cb)])
    payload = StopInput(agent_name="anila", final_output="done", turns_used=3)
    await fire(registry, HookEvent.STOP, payload, bus=EventBus())
    assert fired == [3]


@pytest.mark.unit
async def test_continue_false_aborts() -> None:
    async def stop(_p: Any) -> HookOutput:
        return HookOutput(continue_=False, stop_reason="halt")  # type: ignore[call-arg]

    registry = HookRegistry([HookSpec(event=HookEvent.STOP, callback=stop)])
    payload = StopInput(agent_name="anila", final_output="x", turns_used=1)
    result = await fire(registry, HookEvent.STOP, payload, bus=EventBus())
    assert result.abort is True
    assert result.stop_reason == "halt"


@pytest.mark.unit
def test_event_bus_isolates_listener_errors() -> None:
    bus = EventBus()
    seen: list[str] = []

    def bad(_event: Any) -> None:
        raise RuntimeError("boom")

    def good(event: Any) -> None:
        seen.append(event.kind)

    bus.on("x", bad)
    bus.on("x", good)
    bus.emit("x", value=1)
    assert seen == ["x"]
