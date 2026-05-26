"""P0-1 lifecycle hooks 單元測試。

驗證項目：
- `AnilaRunHooks.on_agent_start` 會觸發 `AGENT_START` 事件並發佈到 event bus。
- `AnilaRunHooks.on_handoff` 會觸發 `HANDOFF` 事件並帶來源 / 目標 Agent 名稱。
- per-agent `AnilaAgentHooks` 各 callback 預設為 no-op，subclass 覆寫後會被呼叫。

測試刻意不依賴 LLM，所有 lifecycle callback 都用最小的 fake context / agent / tool 直接驅動。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from anila_agent.core.events import EventBus
from anila_agent.core.hooks import (
    AgentEndInput,
    AgentStartInput,
    AnilaAgentHooks,
    AnilaRunHooks,
    HandoffInput,
    HookEvent,
    HookRegistry,
    HookSpec,
)
from anila_agent.models.schemas import HookOutput


@dataclass
class _FakeAgent:
    """模擬 openai-agents `Agent`，只需要 `name` 屬性。"""

    name: str


@dataclass
class _FakeTool:
    name: str


@pytest.fixture()
def bus() -> EventBus:
    return EventBus()


@pytest.fixture()
def registry() -> HookRegistry:
    return HookRegistry()


# ---------------------------------------------------------------------------
# RunHooks：on_agent_start
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_agent_start_fires_hook_and_emits_event(
    registry: HookRegistry, bus: EventBus
) -> None:
    """`on_agent_start` 應觸發註冊在 `AGENT_START` 的 hook callback。"""
    captured: list[AgentStartInput] = []

    def _cb(payload: AgentStartInput) -> HookOutput:
        captured.append(payload)
        return HookOutput()

    registry.register(HookSpec(event=HookEvent.AGENT_START, callback=_cb))

    events: list[tuple[str, dict[str, Any]]] = []
    bus.on_any(lambda e: events.append((e.kind, dict(e.payload))))

    hooks = AnilaRunHooks(registry, bus, agent_name="root")
    agent = _FakeAgent(name="root")

    await hooks.on_agent_start(context=object(), agent=agent)  # type: ignore[arg-type]

    assert len(captured) == 1
    assert captured[0].agent_name == "root"
    assert ("agent_started", {"agent": "root"}) in events


# ---------------------------------------------------------------------------
# RunHooks：on_handoff
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_handoff_fires_hook_with_from_and_to_agent(
    registry: HookRegistry, bus: EventBus
) -> None:
    """`on_handoff` 應帶上來源 / 目標 Agent 名稱進入 hook callback。"""
    captured: list[HandoffInput] = []

    async def _cb(payload: HandoffInput) -> HookOutput:
        captured.append(payload)
        return HookOutput()

    registry.register(HookSpec(event=HookEvent.HANDOFF, callback=_cb))

    hooks = AnilaRunHooks(registry, bus, agent_name="planner")
    from_agent = _FakeAgent(name="planner")
    to_agent = _FakeAgent(name="executor")

    await hooks.on_handoff(
        context=object(),  # type: ignore[arg-type]
        from_agent=from_agent,  # type: ignore[arg-type]
        to_agent=to_agent,  # type: ignore[arg-type]
    )

    assert len(captured) == 1
    assert captured[0].from_agent == "planner"
    assert captured[0].to_agent == "executor"


# ---------------------------------------------------------------------------
# RunHooks：on_agent_end 同時 fire AgentEnd + Stop（向後相容）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_agent_end_fires_both_agent_end_and_stop(
    registry: HookRegistry, bus: EventBus
) -> None:
    """on_agent_end 應同時觸發 AGENT_END 與 STOP，確保舊有 Stop 註冊不會 break。"""
    agent_end_calls: list[AgentEndInput] = []
    stop_calls: list[Any] = []

    def _agent_end(payload: AgentEndInput) -> HookOutput:
        agent_end_calls.append(payload)
        return HookOutput()

    def _stop(payload: Any) -> HookOutput:
        stop_calls.append(payload)
        return HookOutput()

    registry.register(HookSpec(event=HookEvent.AGENT_END, callback=_agent_end))
    registry.register(HookSpec(event=HookEvent.STOP, callback=_stop))

    hooks = AnilaRunHooks(registry, bus, agent_name="root")
    await hooks.on_agent_end(
        context=object(),
        agent=_FakeAgent(name="root"),  # type: ignore[arg-type]
        output="done",
    )

    assert len(agent_end_calls) == 1
    assert agent_end_calls[0].agent_name == "root"
    assert agent_end_calls[0].output == "done"
    assert len(stop_calls) == 1


# ---------------------------------------------------------------------------
# Per-agent AgentHooks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_per_agent_hooks_subclass_overrides_fire(bus: EventBus) -> None:
    """`AnilaAgentHooks` subclass 覆寫的 method 應該被呼叫並收到對應 Agent。"""

    class _MyAgentHooks(AnilaAgentHooks):
        def __init__(self, bus: EventBus) -> None:
            super().__init__(bus=bus)
            self.started_with: list[str] = []
            self.ended_with: list[tuple[str, Any]] = []
            self.handoff_pairs: list[tuple[str, str]] = []

        async def on_start(self, context: Any, agent: Any) -> None:
            self.started_with.append(agent.name)
            await super().on_start(context, agent)

        async def on_end(self, context: Any, agent: Any, output: Any) -> None:
            self.ended_with.append((agent.name, output))
            await super().on_end(context, agent, output)

        async def on_handoff(self, context: Any, agent: Any, source: Any) -> None:
            self.handoff_pairs.append((source.name, agent.name))
            await super().on_handoff(context, agent, source)

    hooks = _MyAgentHooks(bus)
    target = _FakeAgent(name="executor")
    source = _FakeAgent(name="planner")

    await hooks.on_start(context=object(), agent=target)
    await hooks.on_handoff(context=object(), agent=target, source=source)  # type: ignore[arg-type]
    await hooks.on_end(context=object(), agent=target, output="ok")

    assert hooks.started_with == ["executor"]
    assert hooks.handoff_pairs == [("planner", "executor")]
    assert hooks.ended_with == [("executor", "ok")]


@pytest.mark.asyncio
async def test_per_agent_hooks_default_is_noop() -> None:
    """`AnilaAgentHooks` 不傳入 bus 時，所有 callback 應為 no-op（不丟 exception）。"""
    hooks = AnilaAgentHooks()
    agent = _FakeAgent(name="solo")
    tool = _FakeTool(name="search")

    # 全部 callback 都應該安全執行完畢。
    await hooks.on_start(context=object(), agent=agent)  # type: ignore[arg-type]
    await hooks.on_end(context=object(), agent=agent, output=None)  # type: ignore[arg-type]
    await hooks.on_handoff(context=object(), agent=agent, source=agent)  # type: ignore[arg-type]
    await hooks.on_tool_start(context=object(), agent=agent, tool=tool)  # type: ignore[arg-type]
    await hooks.on_tool_end(context=object(), agent=agent, tool=tool, result="r")  # type: ignore[arg-type]
    await hooks.on_llm_start(  # type: ignore[arg-type]
        context=object(), agent=agent, system_prompt=None, input_items=[]
    )
    await hooks.on_llm_end(context=object(), agent=agent, response=object())  # type: ignore[arg-type]
