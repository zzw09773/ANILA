"""P0-8 AgentTool / sub-agent dispatch 的 unit test。

涵蓋:

* :func:`make_agent_tool` 建出的 :class:`AgentTool` 有正確 name / description /
  metadata / args schema。
* tool invoke 真的 dispatch 到 sub-agent runner(用 mock runner 驗證收到正確輸入)。
* sub-context 拿到新的 ``tool_call_id`` 與 ``agent_name = sub_agent.name``,且
  parent metadata 有被繼承(parent_agent / parent_tool_call_id 寫進 sub.metadata)。
* sub-agent runner 拋例外時,tool 回 error JSON 而不 raise。
* sub-agent runner 超時 → 同樣回 error JSON。
* tracing span 有被 fire(用 :class:`_CapturingProcessor` 蒐集事件)。
* ``prefix_strategy`` ``"share"`` / ``"fork"`` 兩種值都能正確記在 spec 上(本 task
  只測 metadata 設對,不測 prompt cache 命中,那是 P1-1 範疇)。
* :func:`register_agent_as_tool` 把 sub-agent 包好並加進 registry。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock

import pytest
from agents import Agent
from agents.tool_context import ToolContext

from anila_agent.core import (
    AgentTool,
    AnilaToolContext,
    get_agent_tool_spec,
    make_agent_tool,
    register_agent_as_tool,
)
from anila_agent.tools.base import ToolMetadata, get_metadata
from anila_agent.tools.registry import ToolRegistry
from anila_agent.tracing import Span, Trace, Tracer

# ---------------------------------------------------------------------------
# 測試輔助 — sub-agent fixture / capturing tracer processor / fake ToolContext
# ---------------------------------------------------------------------------


def _make_sub_agent(name: str = "researcher", instructions: str = "Answer briefly.") -> Agent[Any]:
    """建一個最小 sub-agent 樣本;測試只看 dispatch 行為,不真的跑 LLM。"""
    return Agent(name=name, instructions=instructions)


def _make_parent_context(
    *,
    workspace: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> AnilaToolContext:
    """建一個 parent :class:`AnilaToolContext`,給 sub-context 衍生用。"""
    return AnilaToolContext(
        session_id="sess-parent",
        turn_id=3,
        tool_call_id="call-parent-abc",
        agent_name="orchestrator",
        workspace=workspace,
        user_id="user-1",
        caller_id="api-key-1",
        metadata=dict(metadata) if metadata else {"trace_root": "yes"},
    )


def _make_tool_context(parent_anila_ctx: AnilaToolContext | None) -> ToolContext[Any]:
    """組一個 openai-agents :class:`ToolContext`,把 parent AnilaToolContext 塞進 ``context``。"""
    return ToolContext(
        context=parent_anila_ctx,
        usage=None,  # type: ignore[arg-type]
        tool_name="call_researcher",
        tool_call_id="call-parent-abc",
        tool_arguments="",
    )


class _CapturingProcessor:
    """測試用 tracing processor — 把所有 trace / span 事件追加到 list。

    供測試驗證 dispatch 流程確實 fire 出 span(包括 error 路徑的 status='error')。
    """

    def __init__(self) -> None:
        self.events: list[tuple[str, Span | Trace]] = []

    def on_trace_start(self, trace: Trace) -> None:
        self.events.append(("trace.start", trace))

    def on_trace_end(self, trace: Trace) -> None:
        self.events.append(("trace.end", trace))

    def on_span_start(self, span: Span) -> None:
        self.events.append(("span.start", span))

    def on_span_end(self, span: Span) -> None:
        self.events.append(("span.end", span))

    def spans_ended(self) -> list[Span]:
        """取出所有 span.end 事件對應的 Span 物件(順序保留)。"""
        return [obj for name, obj in self.events if name == "span.end" and isinstance(obj, Span)]


# ---------------------------------------------------------------------------
# Factory — name / description / metadata / args schema
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_make_agent_tool_default_name_and_description() -> None:
    """未指定 name / description → 自動推導 (`call_<sub>` + sub instructions 前 100 字)。"""
    sub = _make_sub_agent(name="researcher", instructions="You research topics deeply.")
    spec = make_agent_tool(sub)

    assert isinstance(spec, AgentTool)
    assert spec.name == "call_researcher"
    assert spec.tool is not None
    assert spec.tool.name == "call_researcher"
    # description 內含 sub-agent name 與部分 instructions snippet
    assert "researcher" in spec.description
    assert "research topics" in spec.description


@pytest.mark.unit
def test_make_agent_tool_explicit_name_description_metadata() -> None:
    """顯式參數應原樣保留;metadata 由呼叫端決定就不走預設。"""
    sub = _make_sub_agent()
    meta = ToolMetadata(is_read_only=True, category="custom", cost_estimate="low")
    spec = make_agent_tool(
        sub,
        name="ask_researcher",
        description="Custom desc",
        metadata=meta,
        prefix_strategy="fork",
        timeout_seconds=10.0,
    )

    assert spec.name == "ask_researcher"
    assert spec.description == "Custom desc"
    assert spec.metadata is meta
    assert spec.prefix_strategy == "fork"
    assert spec.timeout_seconds == pytest.approx(10.0)
    # FunctionTool 也應該掛上同一份 metadata,讓 registry / get_metadata 看得到
    assert get_metadata(spec.tool).category == "custom"


@pytest.mark.unit
def test_make_agent_tool_default_metadata_is_high_cost_agent_category() -> None:
    """未指定 metadata → 預設 cost=high / category=agent / 非唯讀。"""
    sub = _make_sub_agent()
    spec = make_agent_tool(sub)
    meta = get_metadata(spec.tool)
    assert meta.category == "agent"
    assert meta.cost_estimate == "high"
    assert meta.is_read_only is False
    assert meta.concurrency_safe is False


@pytest.mark.unit
def test_make_agent_tool_args_schema_has_prompt_and_context_summary() -> None:
    """args schema 必須包含 prompt(必填)與 context_summary 兩個欄位。"""
    sub = _make_sub_agent()
    spec = make_agent_tool(sub)
    schema = spec.tool.params_json_schema

    assert schema["type"] == "object"
    assert "prompt" in schema["properties"]
    assert "context_summary" in schema["properties"]
    assert "prompt" in schema["required"]


@pytest.mark.unit
def test_get_agent_tool_spec_round_trip() -> None:
    """從 :class:`FunctionTool` 反查回 :class:`AgentTool` spec 應拿到同一份。"""
    sub = _make_sub_agent()
    spec = make_agent_tool(sub)
    assert get_agent_tool_spec(spec.tool) is spec


@pytest.mark.unit
def test_get_agent_tool_spec_returns_none_for_unrelated_tool() -> None:
    """非 AgentTool 包出來的 FunctionTool 反查應回 None,不誤判。"""
    from anila_agent.tools import filesystem_tools

    # `read_file` 是普通 anila_tool,不是 AgentTool。
    fs_tool = filesystem_tools.read_file
    assert get_agent_tool_spec(fs_tool) is None


# ---------------------------------------------------------------------------
# Dispatch — sub-agent runner 收到正確輸入、sub-context 建得對
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_dispatch_invokes_runner_with_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    """tool.on_invoke_tool 應該真的呼叫 sub-agent runner,且帶上 prompt 字串。"""
    sub = _make_sub_agent(name="echoer")
    mock_runner = AsyncMock(return_value="sub-agent done")
    spec = make_agent_tool(sub, runner=mock_runner)

    parent_ctx = _make_parent_context()
    tool_ctx = _make_tool_context(parent_ctx)

    args = json.dumps({"prompt": "what is anila?", "context_summary": "discuss platform"})
    result = asyncio.run(spec.tool.on_invoke_tool(tool_ctx, args))

    assert result == "sub-agent done"
    mock_runner.assert_awaited_once()
    called_agent, called_input = mock_runner.await_args.args
    assert called_agent is sub
    assert "what is anila?" in called_input
    assert "discuss platform" in called_input  # context_summary 也被串進去


@pytest.mark.unit
def test_dispatch_creates_sub_context_with_new_tool_call_id_and_agent_name() -> None:
    """sub-agent 對應的 :class:`AnilaToolContext` 應該:
    * tool_call_id 與 parent 不同(以 ``sub_`` 開頭)。
    * agent_name = sub_agent.name(不是 parent 的)。
    * metadata 內保留 parent 識別(parent_agent / parent_tool_call_id)。

    驗證方法:runner 透過閉包把當下 sub-context 內容捕捉下來。本 P0-8 沒把
    sub-context 主動注入 runner 簽名(維持與 default Runner.run 相容),但我們可
    以從 spec 的 dispatch 行為間接驗證 — 改用 trace span attributes 來確認。
    """
    sub = _make_sub_agent(name="worker")
    captured: dict[str, Any] = {}

    async def runner(agent: Agent[Any], prompt: str) -> str:
        captured["agent_name"] = agent.name
        captured["prompt"] = prompt
        return "ok"

    tracer = Tracer()
    cap = _CapturingProcessor()
    tracer.register_processor(cap)

    spec = make_agent_tool(sub, runner=runner, tracer=tracer)
    parent_ctx = _make_parent_context()
    tool_ctx = _make_tool_context(parent_ctx)

    with tracer.start_trace("test.dispatch"):
        result = asyncio.run(
            spec.tool.on_invoke_tool(
                tool_ctx,
                json.dumps({"prompt": "do thing", "context_summary": ""}),
            )
        )

    assert result == "ok"
    # runner 真的被叫到 sub-agent
    assert captured["agent_name"] == "worker"

    # 從 span attributes 反查 sub-context 識別資訊
    spans = cap.spans_ended()
    dispatch_spans = [s for s in spans if s.name == "agent_tool.dispatch.worker"]
    assert len(dispatch_spans) == 1
    attrs = dispatch_spans[0].attributes
    # sub_tool_call_id 必須是 sub_ 開頭(新 id,非 parent)
    assert attrs["agent_tool.sub_tool_call_id"].startswith("sub_")
    assert attrs["agent_tool.sub_tool_call_id"] != parent_ctx.tool_call_id
    # sub agent_name = sub_agent.name
    assert attrs["agent_tool.sub_agent_name"] == "worker"
    # parent 識別有被記下來
    assert attrs["agent_tool.parent_agent"] == parent_ctx.agent_name
    assert attrs["agent_tool.parent_tool_call_id"] == parent_ctx.tool_call_id


@pytest.mark.unit
def test_sub_context_inherits_parent_workspace_and_metadata(tmp_path: Any) -> None:
    """直接呼叫內部 _create_sub_context 驗證欄位繼承關係。"""
    from anila_agent.core.agent_tool import _create_sub_context

    parent = AnilaToolContext(
        session_id="s-1",
        turn_id=7,
        tool_call_id="parent-call",
        agent_name="orchestrator",
        workspace=tmp_path,
        user_id="u-1",
        caller_id="api-1",
        metadata={"existing": "yes"},
    )

    sub = _create_sub_context(parent, sub_agent_name="worker", sub_tool_call_id="sub-xxx")

    # 繼承的欄位
    assert sub.session_id == "s-1"
    assert sub.turn_id == 7
    assert sub.workspace == tmp_path.resolve()
    assert sub.user_id == "u-1"
    assert sub.caller_id == "api-1"
    # cache 共享(parent 已 read 過的檔,sub-agent 也能看到)
    assert sub.file_state_cache is parent.file_state_cache
    # 換過的欄位
    assert sub.agent_name == "worker"
    assert sub.tool_call_id == "sub-xxx"
    # metadata 應該是淺拷貝(不會反過來污染 parent)
    assert sub.metadata is not parent.metadata
    assert sub.metadata["existing"] == "yes"
    assert sub.metadata["parent_agent"] == "orchestrator"
    assert sub.metadata["parent_tool_call_id"] == "parent-call"


# ---------------------------------------------------------------------------
# 錯誤處理 — sub-agent raise / timeout 都回 error JSON 不 raise
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_dispatch_returns_error_json_when_sub_agent_raises() -> None:
    """sub-agent runner 拋例外時,tool 不該 raise,而是回 ``{"error": "..."}`` JSON。"""
    sub = _make_sub_agent(name="broken")

    async def runner(_agent: Agent[Any], _prompt: str) -> str:
        raise RuntimeError("sub broke")

    spec = make_agent_tool(sub, runner=runner)
    tool_ctx = _make_tool_context(_make_parent_context())

    result = asyncio.run(
        spec.tool.on_invoke_tool(tool_ctx, json.dumps({"prompt": "x", "context_summary": ""}))
    )
    payload = json.loads(result)
    assert "error" in payload
    assert "broken" in payload["error"]
    assert "sub broke" in payload["error"]


@pytest.mark.unit
def test_dispatch_returns_error_json_on_timeout() -> None:
    """sub-agent runner 超時(timeout_seconds 過了還沒回) → 回 error JSON,不 raise。"""
    sub = _make_sub_agent(name="slow")

    async def runner(_agent: Agent[Any], _prompt: str) -> str:
        await asyncio.sleep(5.0)
        return "never reached"

    spec = make_agent_tool(sub, runner=runner, timeout_seconds=0.05)
    tool_ctx = _make_tool_context(_make_parent_context())

    result = asyncio.run(
        spec.tool.on_invoke_tool(tool_ctx, json.dumps({"prompt": "x", "context_summary": ""}))
    )
    payload = json.loads(result)
    assert "error" in payload
    assert "timed out" in payload["error"]
    assert "slow" in payload["error"]


@pytest.mark.unit
def test_dispatch_returns_error_json_on_invalid_input() -> None:
    """input_json parse 失敗也走 error JSON 路徑。"""
    sub = _make_sub_agent()
    spec = make_agent_tool(sub, runner=AsyncMock(return_value="ok"))
    tool_ctx = _make_tool_context(_make_parent_context())

    # 故意傳非法 JSON
    result = asyncio.run(spec.tool.on_invoke_tool(tool_ctx, "{not valid json"))
    payload = json.loads(result)
    assert "error" in payload
    assert "invalid input_json" in payload["error"]


@pytest.mark.unit
def test_dispatch_returns_error_json_on_missing_prompt() -> None:
    """prompt 是空字串或缺欄 → 回 error JSON。"""
    sub = _make_sub_agent()
    spec = make_agent_tool(sub, runner=AsyncMock(return_value="ok"))
    tool_ctx = _make_tool_context(_make_parent_context())

    result = asyncio.run(spec.tool.on_invoke_tool(tool_ctx, json.dumps({"prompt": ""})))
    payload = json.loads(result)
    assert "error" in payload
    assert "prompt" in payload["error"]


# ---------------------------------------------------------------------------
# Tracing — span 真的 fire,error 路徑也記錄
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_dispatch_fires_tracing_span_on_success() -> None:
    """正常路徑下 tracing processor 應該收到 span.start + span.end(status=ok)。"""
    sub = _make_sub_agent(name="echoer")
    tracer = Tracer()
    cap = _CapturingProcessor()
    tracer.register_processor(cap)

    spec = make_agent_tool(sub, runner=AsyncMock(return_value="hi"), tracer=tracer)
    tool_ctx = _make_tool_context(_make_parent_context())

    with tracer.start_trace("t"):
        asyncio.run(
            spec.tool.on_invoke_tool(
                tool_ctx, json.dumps({"prompt": "say hi", "context_summary": ""})
            )
        )

    dispatch_spans = [s for s in cap.spans_ended() if s.name.startswith("agent_tool.dispatch")]
    assert len(dispatch_spans) == 1
    span = dispatch_spans[0]
    assert span.name == "agent_tool.dispatch.echoer"
    assert span.status == "ok"
    assert span.attributes["agent_tool.name"] == "call_echoer"
    assert span.attributes["agent_tool.sub_agent"] == "echoer"
    assert span.attributes["agent_tool.prefix_strategy"] == "share"


@pytest.mark.unit
def test_dispatch_fires_tracing_span_on_error() -> None:
    """sub-agent raise 時 span status 應為 error 並寫 error 描述。"""
    sub = _make_sub_agent(name="bad")
    tracer = Tracer()
    cap = _CapturingProcessor()
    tracer.register_processor(cap)

    async def runner(_a: Agent[Any], _p: str) -> str:
        raise ValueError("kaboom")

    spec = make_agent_tool(sub, runner=runner, tracer=tracer)
    tool_ctx = _make_tool_context(_make_parent_context())

    with tracer.start_trace("t"):
        asyncio.run(
            spec.tool.on_invoke_tool(
                tool_ctx, json.dumps({"prompt": "do", "context_summary": ""})
            )
        )

    dispatch_spans = [s for s in cap.spans_ended() if s.name.startswith("agent_tool.dispatch")]
    assert len(dispatch_spans) == 1
    span = dispatch_spans[0]
    assert span.status == "error"
    assert span.error is not None
    assert "kaboom" in span.error
    assert "bad" in span.error  # sub-agent 名稱也應出現在 error msg


# ---------------------------------------------------------------------------
# prefix_strategy — share 與 fork 兩種值都正確保存
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_prefix_strategy_default_is_share() -> None:
    """未指定 prefix_strategy → 預設 ``share``(cache 友善)。"""
    spec = make_agent_tool(_make_sub_agent())
    assert spec.prefix_strategy == "share"


@pytest.mark.unit
def test_prefix_strategy_fork_value_preserved_in_trace() -> None:
    """``prefix_strategy="fork"`` → spec 與 trace span attributes 都應保留 ``fork``。

    本 task 只測 metadata 設對;真正的 prompt-cache fork 行為(子 agent 共享
    byte-identical prefix 取得 cache 命中)由 P1-1 階段完成。
    """
    sub = _make_sub_agent(name="forked")
    tracer = Tracer()
    cap = _CapturingProcessor()
    tracer.register_processor(cap)

    spec = make_agent_tool(
        sub,
        runner=AsyncMock(return_value="ok"),
        prefix_strategy="fork",
        tracer=tracer,
    )
    assert spec.prefix_strategy == "fork"

    tool_ctx = _make_tool_context(_make_parent_context())
    with tracer.start_trace("t"):
        asyncio.run(
            spec.tool.on_invoke_tool(
                tool_ctx, json.dumps({"prompt": "task", "context_summary": ""})
            )
        )

    dispatch_spans = [s for s in cap.spans_ended() if s.name.startswith("agent_tool.dispatch")]
    assert dispatch_spans[0].attributes["agent_tool.prefix_strategy"] == "fork"


# ---------------------------------------------------------------------------
# Registry 整合
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_register_agent_as_tool_adds_to_registry() -> None:
    """``register_agent_as_tool`` 應同時建好 spec、把 tool 推入 registry。"""
    sub = _make_sub_agent(name="planner")
    registry = ToolRegistry()
    spec = register_agent_as_tool(sub, registry)

    assert spec.tool is not None
    assert "call_planner" in registry.tools
    assert registry.tools["call_planner"] is spec.tool


@pytest.mark.unit
def test_registry_find_by_metadata_picks_agent_tool() -> None:
    """以 metadata 過濾應該能找到 AgentTool(category='agent' / cost='high')。"""
    sub = _make_sub_agent(name="reporter")
    registry = ToolRegistry()
    register_agent_as_tool(sub, registry)

    agents_only = registry.find_by_metadata(category="agent")
    assert len(agents_only) == 1
    assert agents_only[0].name == "call_reporter"

    high_cost = registry.find_by_metadata(cost_estimate="high")
    assert len(high_cost) == 1
