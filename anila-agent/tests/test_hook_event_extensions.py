"""P1-17 Hook event 擴充 — SUBAGENT_DISPATCH_* / MCP_* / COMPACTION_* /
GUARDRAIL_TRIPWIRE / POLICY_DENY / TRIGGER_FIRE / STOP_HOOK_PREVENT 的單元測試。

涵蓋:
- ``HookEvent`` enum 新加 11 個 event 正確掛上。
- :class:`HookRegistry` 為新事件提供 decorator factory(``subagent_dispatch_start`` 等)。
- ``AgentTool`` dispatch:成功與 timeout 兩種路徑都會 fire ``SUBAGENT_DISPATCH_START`` + ``END``。
- :class:`MCPServerManager`:start/stop 與 call_tool 會 fire ``MCP_SERVER_CONNECT``
  / ``MCP_SERVER_DISCONNECT`` / ``MCP_TOOL_CALL``。
- :class:`CompactingSession.get_items` 會 fire ``COMPACTION_START`` + ``END``。
- :class:`ToolInputGuardrail.aenforce` 在 BLOCK 時 fire ``GUARDRAIL_TRIPWIRE`` 再 raise。
- :class:`PolicyEngine.aevaluate` 在 DENY / DISABLE 時 fire ``POLICY_DENY``。
- ``fire_trigger_fire`` 與 ``fire_stop_hook_prevent`` 的 payload schema(P1-4 / P1-14 預留)。
- 既有 hook 用法(``HookEvent.PRE_TOOL_USE`` 等)仍可運作 — 沒掛新事件不破壞老程式。
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest
from agents import Agent
from agents.tool_context import ToolContext

from anila_agent.core.agent_tool import make_agent_tool
from anila_agent.core.events import EventBus
from anila_agent.core.guardrails import GuardrailTripwireTriggered
from anila_agent.core.hooks import (
    CompactionEndInput,
    CompactionStartInput,
    GuardrailTripwireInput,
    HookEvent,
    HookRegistry,
    HookSpec,
    McpServerConnectInput,
    McpServerDisconnectInput,
    McpToolCallInput,
    PolicyDenyInput,
    StopHookPreventInput,
    SubagentDispatchEndInput,
    SubagentDispatchStartInput,
    TriggerFireInput,
    fire_stop_hook_prevent,
    fire_trigger_fire,
)
from anila_agent.core.policy import PolicyEffect, PolicyEngine, PolicyRule
from anila_agent.mcp.manager import MCPServerManager
from anila_agent.mcp.server import MCPServer, MCPTool
from anila_agent.memory.compaction import (
    CompactingSession,
    Message,
    MicroCompactor,
)
from anila_agent.models.schemas import HookOutput
from anila_agent.tools.guardrails import (
    ToolGuardrailResult,
    ToolInputGuardrail,
    ToolOutputGuardrail,
)
from anila_agent.tools.registry import ToolRegistry

# ---------------------------------------------------------------------------
# Enum 完整性
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_hook_event_enum_has_new_p1_17_events() -> None:
    """P1-17 新加的 11 個 event 都在 HookEvent enum 上。"""
    expected = {
        "SUBAGENT_DISPATCH_START",
        "SUBAGENT_DISPATCH_END",
        "MCP_SERVER_CONNECT",
        "MCP_SERVER_DISCONNECT",
        "MCP_TOOL_CALL",
        "COMPACTION_START",
        "COMPACTION_END",
        "TRIGGER_FIRE",
        "GUARDRAIL_TRIPWIRE",
        "POLICY_DENY",
        "STOP_HOOK_PREVENT",
    }
    member_names = {m.name for m in HookEvent}
    missing = expected - member_names
    assert not missing, f"HookEvent 缺少: {missing}"


@pytest.mark.unit
def test_hook_event_values_are_pascal_case() -> None:
    """新 event 的 value 採 PascalCase,對齊上游 claude-code-src hook event 命名。"""
    expected_values = {
        HookEvent.SUBAGENT_DISPATCH_START.value: "SubagentDispatchStart",
        HookEvent.SUBAGENT_DISPATCH_END.value: "SubagentDispatchEnd",
        HookEvent.MCP_SERVER_CONNECT.value: "McpServerConnect",
        HookEvent.MCP_SERVER_DISCONNECT.value: "McpServerDisconnect",
        HookEvent.MCP_TOOL_CALL.value: "McpToolCall",
        HookEvent.COMPACTION_START.value: "CompactionStart",
        HookEvent.COMPACTION_END.value: "CompactionEnd",
        HookEvent.TRIGGER_FIRE.value: "TriggerFire",
        HookEvent.GUARDRAIL_TRIPWIRE.value: "GuardrailTripwire",
        HookEvent.POLICY_DENY.value: "PolicyDeny",
        HookEvent.STOP_HOOK_PREVENT.value: "StopHookPrevent",
    }
    for value, expected in expected_values.items():
        assert value == expected


# ---------------------------------------------------------------------------
# Decorator factory(P0-4 decorator-style 註冊)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_registry_provides_decorator_for_each_new_event() -> None:
    """HookRegistry 為每個新 event 都提供 decorator factory(對齊 P0-4 風格)。"""
    registry = HookRegistry()

    # 每個 decorator 用 callable 套上去後,該 callback 應被 register。
    @registry.subagent_dispatch_start
    def _on_dispatch_start(payload: SubagentDispatchStartInput) -> HookOutput:
        del payload
        return HookOutput()

    @registry.subagent_dispatch_end
    def _on_dispatch_end(payload: SubagentDispatchEndInput) -> HookOutput:
        del payload
        return HookOutput()

    @registry.mcp_server_connect
    def _on_mcp_connect(payload: McpServerConnectInput) -> HookOutput:
        del payload
        return HookOutput()

    @registry.mcp_server_disconnect
    def _on_mcp_disconnect(payload: McpServerDisconnectInput) -> HookOutput:
        del payload
        return HookOutput()

    @registry.mcp_tool_call
    def _on_mcp_tool(payload: McpToolCallInput) -> HookOutput:
        del payload
        return HookOutput()

    @registry.compaction_start
    def _on_compaction_start(payload: CompactionStartInput) -> HookOutput:
        del payload
        return HookOutput()

    @registry.compaction_end
    def _on_compaction_end(payload: CompactionEndInput) -> HookOutput:
        del payload
        return HookOutput()

    @registry.trigger_fire
    def _on_trigger(payload: TriggerFireInput) -> HookOutput:
        del payload
        return HookOutput()

    @registry.guardrail_tripwire
    def _on_tripwire(payload: GuardrailTripwireInput) -> HookOutput:
        del payload
        return HookOutput()

    @registry.policy_deny
    def _on_policy(payload: PolicyDenyInput) -> HookOutput:
        del payload
        return HookOutput()

    @registry.stop_hook_prevent
    def _on_stop(payload: StopHookPreventInput) -> HookOutput:
        del payload
        return HookOutput()

    # 每個 event 應有正好一條 spec 註冊
    for event in (
        HookEvent.SUBAGENT_DISPATCH_START,
        HookEvent.SUBAGENT_DISPATCH_END,
        HookEvent.MCP_SERVER_CONNECT,
        HookEvent.MCP_SERVER_DISCONNECT,
        HookEvent.MCP_TOOL_CALL,
        HookEvent.COMPACTION_START,
        HookEvent.COMPACTION_END,
        HookEvent.TRIGGER_FIRE,
        HookEvent.GUARDRAIL_TRIPWIRE,
        HookEvent.POLICY_DENY,
        HookEvent.STOP_HOOK_PREVENT,
    ):
        assert len(registry.specs_for(event)) == 1, f"{event.value} 應有 1 條 spec"


# ---------------------------------------------------------------------------
# Sub-agent dispatch — START + END
# ---------------------------------------------------------------------------


def _make_sub_agent(name: str = "researcher") -> Agent[Any]:
    """測試用最小 sub-agent — 不會真的跑 LLM(runner 用 AsyncMock 取代)。"""
    return Agent(name=name, instructions="Just respond.")


def _make_tool_context() -> ToolContext[Any]:
    """組一個 ToolContext;此處沒 parent AnilaToolContext,測試 backward compat。"""
    return ToolContext(
        context=None,
        usage=None,  # type: ignore[arg-type]
        tool_name="call_researcher",
        tool_call_id="call-xyz",
        tool_arguments="",
    )


@pytest.mark.asyncio
async def test_subagent_dispatch_fires_start_and_end_on_success() -> None:
    """sub-agent 成功跑完 — 應依序 fire SUBAGENT_DISPATCH_START 然後 SUBAGENT_DISPATCH_END。"""
    registry = HookRegistry()
    bus = EventBus()

    starts: list[SubagentDispatchStartInput] = []
    ends: list[SubagentDispatchEndInput] = []

    @registry.subagent_dispatch_start
    def _on_start(payload: SubagentDispatchStartInput) -> HookOutput:
        starts.append(payload)
        return HookOutput()

    @registry.subagent_dispatch_end
    async def _on_end(payload: SubagentDispatchEndInput) -> HookOutput:
        ends.append(payload)
        return HookOutput()

    sub = _make_sub_agent()
    runner = AsyncMock(return_value="sub-output-text")
    spec = make_agent_tool(
        sub, runner=runner, hook_registry=registry, event_bus=bus
    )
    assert spec.tool is not None

    result = await spec.tool.on_invoke_tool(  # type: ignore[arg-type]
        _make_tool_context(),
        '{"prompt": "go find facts", "context_summary": "topic=foo"}',
    )

    assert result == "sub-output-text"
    assert len(starts) == 1
    assert len(ends) == 1
    assert starts[0].sub_agent == "researcher"
    assert starts[0].tool_name == spec.name
    assert "go find facts" in starts[0].prompt
    assert ends[0].sub_agent == "researcher"
    assert ends[0].error is None
    assert ends[0].output == "sub-output-text"


@pytest.mark.asyncio
async def test_subagent_dispatch_fires_end_with_error_on_timeout() -> None:
    """sub-agent timeout — SUBAGENT_DISPATCH_END 應帶 error 摘要、output 為 error JSON。"""
    registry = HookRegistry()

    ends: list[SubagentDispatchEndInput] = []

    @registry.subagent_dispatch_end
    def _on_end(payload: SubagentDispatchEndInput) -> HookOutput:
        ends.append(payload)
        return HookOutput()

    async def _slow_runner(_agent: Agent[Any], _prompt: str) -> Any:
        await asyncio.sleep(1.0)
        return "never"

    sub = _make_sub_agent()
    spec = make_agent_tool(
        sub,
        runner=_slow_runner,
        timeout_seconds=0.01,
        hook_registry=registry,
    )
    assert spec.tool is not None

    result = await spec.tool.on_invoke_tool(  # type: ignore[arg-type]
        _make_tool_context(),
        '{"prompt": "do thing", "context_summary": ""}',
    )

    assert "error" in result
    assert len(ends) == 1
    assert ends[0].error is not None
    assert "timed out" in ends[0].error


@pytest.mark.asyncio
async def test_subagent_dispatch_no_registry_no_fire() -> None:
    """未注入 hook_registry — 不 fire 任何 hook(向後相容)。"""
    # 用一個第三方 registry 來偵測:即使這個 registry 有 hook,
    # 也不應該被 fire(spec 沒拿到這個 registry)。
    detector = HookRegistry()
    captured: list[Any] = []

    @detector.subagent_dispatch_start
    def _on_start(payload: SubagentDispatchStartInput) -> HookOutput:
        captured.append(payload)
        return HookOutput()

    sub = _make_sub_agent()
    runner = AsyncMock(return_value="done")
    spec = make_agent_tool(sub, runner=runner)  # 故意不傳 hook_registry
    assert spec.tool is not None

    await spec.tool.on_invoke_tool(  # type: ignore[arg-type]
        _make_tool_context(),
        '{"prompt": "x", "context_summary": ""}',
    )

    assert captured == [], "未注入 registry 時不應 fire 任何 hook"


# ---------------------------------------------------------------------------
# MCP — connect / disconnect / call_tool
# ---------------------------------------------------------------------------


class _FakeMcpServer(MCPServer):
    """測試用 MCP server — 跳過 transport,直接給 tools / call_result。"""

    def __init__(
        self,
        name: str,
        tools: list[MCPTool],
        call_result: Any = "ok",
    ) -> None:
        super().__init__(name=name)
        self._tools = tools
        self._call_result = call_result

    async def _open_transport(self) -> None:
        return None

    async def _close_transport(self) -> None:
        return None

    async def _send_raw(self, payload: dict[str, Any]) -> None:
        return None

    async def _receive_raw(self) -> dict[str, Any]:
        return {}

    async def connect(self) -> None:
        if self.is_connected:
            return
        self._transport_ready = True
        self._connected = True

    async def disconnect(self) -> None:
        self._transport_ready = False
        self._connected = False

    async def list_tools(self) -> list[MCPTool]:
        return list(self._tools)

    async def call_tool(self, name: str, args: dict[str, Any]) -> Any:
        del name, args
        return self._call_result


@pytest.mark.asyncio
async def test_mcp_manager_fires_connect_and_disconnect() -> None:
    """start_all / stop_all 流程應 fire MCP_SERVER_CONNECT 與 MCP_SERVER_DISCONNECT。"""
    registry = HookRegistry()
    bus = EventBus()

    connects: list[McpServerConnectInput] = []
    disconnects: list[McpServerDisconnectInput] = []

    @registry.mcp_server_connect
    def _on_conn(payload: McpServerConnectInput) -> HookOutput:
        connects.append(payload)
        return HookOutput()

    @registry.mcp_server_disconnect
    def _on_disc(payload: McpServerDisconnectInput) -> HookOutput:
        disconnects.append(payload)
        return HookOutput()

    tool_registry = ToolRegistry()
    manager = MCPServerManager(
        registry=tool_registry,
        event_bus=bus,
        hook_registry=registry,
    )
    manager.add(
        _FakeMcpServer(
            name="alpha",
            tools=[
                MCPTool(
                    server_name="alpha",
                    name="ping",
                    description="ping",
                    input_schema={"type": "object"},
                )
            ],
        )
    )

    await manager.start_all()
    assert [c.server_name for c in connects] == ["alpha"]

    await manager.stop_all()
    assert [d.server_name for d in disconnects] == ["alpha"]


@pytest.mark.asyncio
async def test_mcp_manager_fires_tool_call_before_dispatch() -> None:
    """call_tool 之前應 fire MCP_TOOL_CALL,payload 含 server / tool / args。"""
    registry = HookRegistry()
    bus = EventBus()

    captured: list[McpToolCallInput] = []

    @registry.mcp_tool_call
    def _on_call(payload: McpToolCallInput) -> HookOutput:
        captured.append(payload)
        return HookOutput()

    tool_registry = ToolRegistry()
    manager = MCPServerManager(
        registry=tool_registry,
        event_bus=bus,
        hook_registry=registry,
    )
    manager.add(
        _FakeMcpServer(
            name="beta",
            tools=[
                MCPTool(
                    server_name="beta",
                    name="echo",
                    description="echo",
                    input_schema={"type": "object"},
                )
            ],
            call_result="echo-back",
        )
    )

    await manager.start_all()
    result = await manager.call_tool("beta", "echo", {"msg": "hi"})
    assert result == "echo-back"
    assert len(captured) == 1
    assert captured[0].server_name == "beta"
    assert captured[0].tool_name == "echo"
    assert captured[0].args == {"msg": "hi"}

    await manager.stop_all()


@pytest.mark.asyncio
async def test_mcp_manager_without_hook_registry_does_not_fire() -> None:
    """未注入 hook_registry — MCP lifecycle 不 fire(向後相容,P1-5 原行為)。"""
    detector = HookRegistry()
    captured: list[Any] = []

    @detector.mcp_server_connect
    def _on_conn(payload: McpServerConnectInput) -> HookOutput:
        captured.append(payload)
        return HookOutput()

    tool_registry = ToolRegistry()
    manager = MCPServerManager(registry=tool_registry)  # 沒給 hook_registry
    manager.add(_FakeMcpServer("noisy", tools=[]))

    await manager.start_all()
    await manager.stop_all()

    assert captured == []


# ---------------------------------------------------------------------------
# Compaction — START + END
# ---------------------------------------------------------------------------


class _FakeSession:
    """測試用 in-memory Session — 符合 openai-agents Session Protocol。"""

    def __init__(self, session_id: str, items: list[Message]) -> None:
        self.session_id = session_id
        self._items = items

    async def get_items(self, limit: int | None = None) -> list[Message]:
        del limit
        return list(self._items)

    async def add_items(self, items: list[Message]) -> None:
        self._items.extend(items)

    async def pop_item(self) -> Message | None:
        return self._items.pop() if self._items else None

    async def clear_session(self) -> None:
        self._items.clear()


@pytest.mark.asyncio
async def test_compacting_session_fires_start_and_end() -> None:
    """CompactingSession.get_items 應 fire COMPACTION_START 然後 COMPACTION_END。"""
    registry = HookRegistry()
    bus = EventBus()

    starts: list[CompactionStartInput] = []
    ends: list[CompactionEndInput] = []

    @registry.compaction_start
    def _on_start(payload: CompactionStartInput) -> HookOutput:
        starts.append(payload)
        return HookOutput()

    @registry.compaction_end
    def _on_end(payload: CompactionEndInput) -> HookOutput:
        ends.append(payload)
        return HookOutput()

    # 建 15 則 message(包括舊 tool 互動),用 MicroCompactor 保留最近 5 則
    # → 應有 messages dropped。
    items: list[Message] = []
    for i in range(5):
        items.append({"role": "user", "content": f"u{i}"})
        items.append({"type": "function_call", "call_id": f"c{i}", "name": "x", "arguments": "{}"})
        items.append({"type": "function_call_output", "call_id": f"c{i}", "output": "ok"})

    session = CompactingSession(
        underlying=_FakeSession("sess-x", items),
        compactors=[MicroCompactor(retain_last_n=5)],
        hook_registry=registry,
        event_bus=bus,
    )

    out = await session.get_items()
    assert len(starts) == 1
    assert len(ends) == 1
    assert starts[0].session_id == "sess-x"
    assert starts[0].compactors == ("micro",)
    assert starts[0].before_messages == len(items)
    assert ends[0].session_id == "sess-x"
    assert ends[0].before_messages == len(items)
    assert ends[0].after_messages == len(out)
    assert ends[0].messages_dropped == max(0, len(items) - len(out))


@pytest.mark.asyncio
async def test_compacting_session_no_registry_no_fire() -> None:
    """未注入 hook_registry — 不 fire(向後相容,P1-6 原行為)。"""
    detector = HookRegistry()
    captured: list[Any] = []

    @detector.compaction_start
    def _on_start(payload: CompactionStartInput) -> HookOutput:
        captured.append(payload)
        return HookOutput()

    items: list[Message] = [{"role": "user", "content": "hi"}]
    session = CompactingSession(
        underlying=_FakeSession("sess-y", items),
        compactors=[MicroCompactor()],
    )
    await session.get_items()
    assert captured == []


# ---------------------------------------------------------------------------
# Guardrail tripwire
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_input_guardrail_aenforce_fires_tripwire_then_raises() -> None:
    """aenforce 命中 BLOCK 時,應先 fire GUARDRAIL_TRIPWIRE 再 raise tripwire。"""
    registry = HookRegistry()
    bus = EventBus()

    captured: list[GuardrailTripwireInput] = []

    @registry.guardrail_tripwire
    def _on_trip(payload: GuardrailTripwireInput) -> HookOutput:
        captured.append(payload)
        return HookOutput()

    def _check(_ctx: Any, _name: str, _args: dict[str, Any]) -> ToolGuardrailResult:
        return ToolGuardrailResult.block(output_info={"why": "policy"})

    guardrail = ToolInputGuardrail(guardrail_function=_check, name="my_input_guard")

    with pytest.raises(GuardrailTripwireTriggered) as exc_info:
        await guardrail.aenforce(
            None,
            "shell.run",
            {"cmd": "rm -rf /"},
            hook_registry=registry,
            event_bus=bus,
        )

    assert exc_info.value.guardrail_name == "my_input_guard"
    # hook 應在 raise 前 fire
    assert len(captured) == 1
    assert captured[0].guardrail_name == "my_input_guard"
    assert captured[0].stage == "input"
    assert captured[0].tool_name == "shell.run"
    assert captured[0].info.get("why") == "policy"


@pytest.mark.asyncio
async def test_tool_output_guardrail_aenforce_fires_tripwire() -> None:
    """ToolOutputGuardrail.aenforce 也應 fire GUARDRAIL_TRIPWIRE(stage='output')。"""
    registry = HookRegistry()

    captured: list[GuardrailTripwireInput] = []

    @registry.guardrail_tripwire
    def _on_trip(payload: GuardrailTripwireInput) -> HookOutput:
        captured.append(payload)
        return HookOutput()

    def _check(_ctx: Any, _name: str, _result: Any) -> ToolGuardrailResult:
        return ToolGuardrailResult.block(output_info={"contains_secret": True})

    guardrail = ToolOutputGuardrail(guardrail_function=_check, name="secret_scan")

    with pytest.raises(GuardrailTripwireTriggered):
        await guardrail.aenforce(
            None,
            "file.read",
            "secret payload",
            hook_registry=registry,
        )

    assert len(captured) == 1
    assert captured[0].stage == "output"
    assert captured[0].tool_name == "file.read"


@pytest.mark.asyncio
async def test_tool_guardrail_aenforce_allow_does_not_fire_tripwire() -> None:
    """aenforce 在 ALLOW 路徑不應 fire GUARDRAIL_TRIPWIRE。"""
    registry = HookRegistry()

    captured: list[Any] = []

    @registry.guardrail_tripwire
    def _on_trip(payload: GuardrailTripwireInput) -> HookOutput:
        captured.append(payload)
        return HookOutput()

    def _check(_ctx: Any, _name: str, _args: dict[str, Any]) -> ToolGuardrailResult:
        return ToolGuardrailResult.allow()

    guardrail = ToolInputGuardrail(guardrail_function=_check, name="ok")
    result = await guardrail.aenforce(None, "x", {}, hook_registry=registry)
    assert result.behavior.value == "allow"
    assert captured == []


# ---------------------------------------------------------------------------
# Policy DENY / DISABLE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_policy_aevaluate_fires_policy_deny_on_deny() -> None:
    """PolicyEngine.aevaluate 回 DENY 時應 fire POLICY_DENY。"""
    registry = HookRegistry()
    bus = EventBus()

    captured: list[PolicyDenyInput] = []

    @registry.policy_deny
    def _on_deny(payload: PolicyDenyInput) -> HookOutput:
        captured.append(payload)
        return HookOutput()

    engine = PolicyEngine()
    engine.add_rule(
        PolicyRule(
            name="no_shell",
            tool_pattern="shell.run",
            effect=PolicyEffect.DENY,
            reason="prod 不允許 shell",
        )
    )

    decision = await engine.aevaluate(
        None, "shell.run", {}, hook_registry=registry, event_bus=bus
    )
    assert decision.denied
    assert decision.rule_name == "no_shell"
    assert len(captured) == 1
    assert captured[0].tool_name == "shell.run"
    assert captured[0].rule_name == "no_shell"
    assert captured[0].effect == "deny"
    assert captured[0].reason == "prod 不允許 shell"


@pytest.mark.asyncio
async def test_policy_aevaluate_fires_policy_deny_on_disable() -> None:
    """DISABLE 也算 deny — 應 fire POLICY_DENY,effect='disable'。"""
    registry = HookRegistry()
    captured: list[PolicyDenyInput] = []

    @registry.policy_deny
    def _on_deny(payload: PolicyDenyInput) -> HookOutput:
        captured.append(payload)
        return HookOutput()

    engine = PolicyEngine()
    engine.add_rule(
        PolicyRule(
            name="hide_secret",
            tool_pattern="secret_tool",
            effect=PolicyEffect.DISABLE,
            reason="capability filter",
        )
    )

    decision = await engine.aevaluate(
        None, "secret_tool", {}, hook_registry=registry
    )
    assert decision.disabled
    assert len(captured) == 1
    assert captured[0].effect == "disable"


@pytest.mark.asyncio
async def test_policy_aevaluate_allow_does_not_fire() -> None:
    """ALLOW 不應 fire POLICY_DENY。"""
    registry = HookRegistry()
    captured: list[Any] = []

    @registry.policy_deny
    def _on_deny(payload: PolicyDenyInput) -> HookOutput:
        captured.append(payload)
        return HookOutput()

    engine = PolicyEngine()  # default open → ALLOW
    decision = await engine.aevaluate(
        None, "anything", {}, hook_registry=registry
    )
    assert decision.allowed
    assert captured == []


# ---------------------------------------------------------------------------
# Trigger fire / Stop hook prevent — payload schema(P1-4 / P1-14 預留)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fire_trigger_fire_dispatches_to_registered_hook() -> None:
    """fire_trigger_fire helper 直接呼叫 → 對應 hook 收到 TriggerFireInput。"""
    registry = HookRegistry()
    captured: list[TriggerFireInput] = []

    @registry.trigger_fire
    def _on_trigger(payload: TriggerFireInput) -> HookOutput:
        captured.append(payload)
        return HookOutput()

    await fire_trigger_fire(
        registry,
        None,
        trigger_name="re_embed",
        source="file_watch",
        details={"path": "/data/new.pdf"},
    )

    assert len(captured) == 1
    assert captured[0].trigger_name == "re_embed"
    assert captured[0].source == "file_watch"
    assert captured[0].details == {"path": "/data/new.pdf"}


@pytest.mark.asyncio
async def test_fire_stop_hook_prevent_dispatches_to_registered_hook() -> None:
    """fire_stop_hook_prevent helper → 對應 hook 收到 StopHookPreventInput。"""
    registry = HookRegistry()
    captured: list[StopHookPreventInput] = []

    @registry.stop_hook_prevent
    def _on_prevent(payload: StopHookPreventInput) -> HookOutput:
        captured.append(payload)
        return HookOutput()

    await fire_stop_hook_prevent(
        registry,
        None,
        agent_name="root",
        reason="still has TODO",
        turns_used=7,
    )

    assert len(captured) == 1
    assert captured[0].agent_name == "root"
    assert captured[0].reason == "still has TODO"
    assert captured[0].turns_used == 7


@pytest.mark.asyncio
async def test_fire_helpers_noop_when_registry_is_none() -> None:
    """所有 P1-17 fire helper 在 registry=None 時應 silent no-op、不 raise。"""
    # 全部用 None 跑一遍,確認不丟例外。
    await fire_trigger_fire(None, None, trigger_name="t", source="s", details=None)
    await fire_stop_hook_prevent(None, None, agent_name="a", reason="r", turns_used=0)


# ---------------------------------------------------------------------------
# Backward compat — 沒掛新 event 的 hook 不受影響
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_existing_pre_tool_use_still_works_without_new_events() -> None:
    """既有 PRE_TOOL_USE hook 用法不被新事件破壞。"""
    registry = HookRegistry()
    called: list[str] = []

    def _legacy_pre_tool(payload: Any) -> HookOutput:
        del payload
        called.append("pre")
        return HookOutput()

    registry.register(
        HookSpec(event=HookEvent.PRE_TOOL_USE, callback=_legacy_pre_tool, matcher=".*")
    )

    # 新 event 沒掛 hook → specs_for 回空,不該影響既有 PRE_TOOL_USE 行為。
    assert registry.specs_for(HookEvent.PRE_TOOL_USE) != []
    assert registry.specs_for(HookEvent.SUBAGENT_DISPATCH_START) == []
    assert registry.specs_for(HookEvent.MCP_TOOL_CALL) == []
    assert called == []  # 還沒 fire
