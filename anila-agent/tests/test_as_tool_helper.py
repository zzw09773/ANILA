"""P2-1 ``as_tool`` shorthand 的 unit test。

涵蓋:

* :func:`as_tool` shorthand 跟 :func:`make_agent_tool` 行為等效。
* :func:`as_tool` 預設 ``prefix_strategy="share"`` (P1-1 cache 友善)。
* :func:`as_tool` 包出來的 spec 可正常被 sub-agent dispatch loop 跑。
* :func:`as_tool_full` 等同 :func:`make_agent_tool` (full-control alias)。
* :func:`enable_anila_as_tool_method` 把 ``anila_as_tool`` method patch 到
  :class:`agents.Agent` class 上,**不**覆蓋上游 SDK 同名 ``as_tool``。
* :func:`enable_anila_as_tool_method` idempotent;:func:`disable_anila_as_tool_method`
  能 clean up。
* 多 agent 協作場景:兩個 sub-agent 同時 :func:`as_tool` 後給 main agent 用,
  各自 dispatch 都能跑(用 mock runner 驗證 routing 正確)。

對齊上游 ``openai-agents`` SDK ``Agent.as_tool`` 的 API shape:前兩個必傳參數為
``tool_name`` / ``tool_description``(本 module signature 與之一致)。
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
    as_tool,
    as_tool_full,
    disable_anila_as_tool_method,
    enable_anila_as_tool_method,
    make_agent_tool,
)
from anila_agent.tools.base import ToolMetadata, get_metadata
from anila_agent.tools.registry import ToolRegistry

# ---------------------------------------------------------------------------
# 測試輔助 — 跟 test_agent_tool.py 對齊 (避免 fixture 跨檔耦合,直接複製極簡 helper)
# ---------------------------------------------------------------------------


def _make_sub_agent(name: str = "retriever", instructions: str = "Search and return chunks.") -> Agent[Any]:
    """建一個最小 sub-agent 樣本 (僅作為 dispatch target,不真的跑 LLM)。"""
    return Agent(name=name, instructions=instructions)


def _make_parent_context() -> AnilaToolContext:
    """建一個 parent :class:`AnilaToolContext`,給 sub-context 衍生用。"""
    return AnilaToolContext(
        session_id="sess-parent",
        turn_id=1,
        tool_call_id="call-parent-xyz",
        agent_name="orchestrator",
        user_id="user-1",
        caller_id="api-key-1",
        metadata={},
    )


def _make_tool_context(parent_anila_ctx: AnilaToolContext | None) -> ToolContext[Any]:
    """組一個 openai-agents :class:`ToolContext`,把 parent AnilaToolContext 塞進 ``context``。"""
    return ToolContext(
        context=parent_anila_ctx,
        usage=None,  # type: ignore[arg-type]
        tool_name="call_retriever",
        tool_call_id="call-parent-xyz",
        tool_arguments="",
    )


# ---------------------------------------------------------------------------
# 1. as_tool 跟 make_agent_tool 行為等效
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_as_tool_returns_agent_tool_spec() -> None:
    """:func:`as_tool` 回 :class:`AgentTool` (不是 FunctionTool — 與 SDK 上游語意差別)。"""
    sub = _make_sub_agent()
    spec = as_tool(sub, "search", "搜尋 top-5 chunks")
    assert isinstance(spec, AgentTool)
    assert spec.tool is not None  # 內含 FunctionTool


@pytest.mark.unit
def test_as_tool_preserves_explicit_name_and_description() -> None:
    """顯式給的 ``tool_name`` / ``tool_description`` 應原樣保留到 spec 跟 FunctionTool。"""
    sub = _make_sub_agent(name="retriever")
    spec = as_tool(sub, "search_chunks", "向 vector DB 抓 top-5 chunks")
    assert spec.name == "search_chunks"
    assert spec.description == "向 vector DB 抓 top-5 chunks"
    assert spec.tool.name == "search_chunks"
    assert spec.tool.description == "向 vector DB 抓 top-5 chunks"


@pytest.mark.unit
def test_as_tool_falls_back_to_default_name_when_none() -> None:
    """``tool_name=None`` 應 fallback 到 :func:`make_agent_tool` 預設 (``f"call_{name}"``)。"""
    sub = _make_sub_agent(name="searcher")
    spec = as_tool(sub, None, "搜尋")
    assert spec.name == "call_searcher"


@pytest.mark.unit
def test_as_tool_falls_back_to_default_description_when_none() -> None:
    """``tool_description=None`` 應 fallback 到從 sub_agent.instructions 取前 ~100 字。"""
    sub = _make_sub_agent(name="searcher", instructions="Specialized chunk retrieval agent.")
    spec = as_tool(sub, "search", None)
    assert "searcher" in spec.description
    assert "Specialized" in spec.description


@pytest.mark.unit
def test_as_tool_default_prefix_strategy_is_share() -> None:
    """shorthand 預設 ``prefix_strategy="share"`` (對齊 :func:`make_agent_tool`)。"""
    sub = _make_sub_agent()
    spec = as_tool(sub, "search", "搜尋")
    assert spec.prefix_strategy == "share"


@pytest.mark.unit
def test_as_tool_accepts_explicit_prefix_strategy_fork() -> None:
    """shorthand 仍可 keyword override 成 ``"fork"``。"""
    sub = _make_sub_agent()
    spec = as_tool(sub, "search", "搜尋", prefix_strategy="fork")
    assert spec.prefix_strategy == "fork"


@pytest.mark.unit
def test_as_tool_equivalent_to_make_agent_tool() -> None:
    """同樣參數下,:func:`as_tool` 與 :func:`make_agent_tool` 產出的 spec 欄位應等效。

    驗證範圍:``name`` / ``description`` / ``prefix_strategy`` / ``metadata.category`` /
    ``tool.params_json_schema``。frozen dataclass 自身 instance 不會相等 (兩次 call
    各建一個),所以逐欄位比對。
    """
    sub = _make_sub_agent(name="echoer", instructions="Echo back the input.")

    a = as_tool(sub, "ask_echoer", "讓 echoer 回應")
    b = make_agent_tool(sub, name="ask_echoer", description="讓 echoer 回應")

    assert a.name == b.name
    assert a.description == b.description
    assert a.prefix_strategy == b.prefix_strategy
    assert a.timeout_seconds == b.timeout_seconds
    assert get_metadata(a.tool).category == get_metadata(b.tool).category
    assert get_metadata(a.tool).cost_estimate == get_metadata(b.tool).cost_estimate
    assert a.tool.params_json_schema == b.tool.params_json_schema


# ---------------------------------------------------------------------------
# 2. as_tool 產出的 spec 真的能跑 dispatch
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_as_tool_dispatches_to_sub_agent_runner() -> None:
    """shorthand 包出來的 ``tool.on_invoke_tool`` 應真的 dispatch 到 sub-agent runner。"""
    sub = _make_sub_agent(name="echoer")
    mock_runner = AsyncMock(return_value="echoer done")

    # shorthand 不暴露 runner 參數;直接用 as_tool_full 帶 runner 才能驗證 dispatch
    # (這也順便驗證 as_tool_full 是 make_agent_tool 的 alias)。
    spec = as_tool_full(sub, "ask_echoer", "讓 echoer 回應", runner=mock_runner)

    tool_ctx = _make_tool_context(_make_parent_context())
    args = json.dumps({"prompt": "say hi", "context_summary": ""})
    result = asyncio.run(spec.tool.on_invoke_tool(tool_ctx, args))

    assert result == "echoer done"
    mock_runner.assert_awaited_once()
    called_agent, called_prompt = mock_runner.await_args.args
    assert called_agent is sub
    assert "say hi" in called_prompt


# ---------------------------------------------------------------------------
# 3. as_tool_full 等同 make_agent_tool (full-control alias)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_as_tool_full_preserves_all_advanced_parameters() -> None:
    """:func:`as_tool_full` 應把所有進階參數透傳給 :func:`make_agent_tool`。"""
    sub = _make_sub_agent()
    meta = ToolMetadata(is_read_only=True, category="rag", cost_estimate="medium")
    spec = as_tool_full(
        sub,
        "search_rag",
        "RAG 搜尋",
        metadata=meta,
        prefix_strategy="fork",
        fork_point=5,
        timeout_seconds=60.0,
    )
    assert spec.name == "search_rag"
    assert spec.description == "RAG 搜尋"
    assert spec.metadata is meta
    assert spec.prefix_strategy == "fork"
    assert spec.fork_point == 5
    assert spec.timeout_seconds == pytest.approx(60.0)


# ---------------------------------------------------------------------------
# 4. enable_anila_as_tool_method — opt-in 把 method patch 到 Agent class 上
# ---------------------------------------------------------------------------


@pytest.fixture()
def _clean_anila_as_tool_method() -> Any:
    """每個 patch 測試都 setup/teardown,避免污染其他測試。

    teardown 把 ``anila_as_tool`` 從 ``Agent`` class 清掉(若有)。
    """
    disable_anila_as_tool_method()  # 確保 setup 時是乾淨的
    yield
    disable_anila_as_tool_method()


@pytest.mark.unit
def test_enable_anila_as_tool_method_installs_attribute(_clean_anila_as_tool_method: None) -> None:
    """安裝後 :class:`Agent` 應有 ``anila_as_tool`` attribute。"""
    assert not hasattr(Agent, "anila_as_tool")
    enable_anila_as_tool_method()
    assert hasattr(Agent, "anila_as_tool")


@pytest.mark.unit
def test_enable_anila_as_tool_method_does_not_overwrite_sdk_as_tool(
    _clean_anila_as_tool_method: None,
) -> None:
    """安裝 ANILA shorthand **不**該覆蓋上游 SDK 的 :meth:`Agent.as_tool` (兩者語意不同)。"""
    sdk_as_tool_before = Agent.as_tool
    enable_anila_as_tool_method()
    # 上游 as_tool 應原封不動
    assert Agent.as_tool is sdk_as_tool_before
    # ANILA 走另一個名字
    assert hasattr(Agent, "anila_as_tool")
    assert Agent.anila_as_tool is not Agent.as_tool


@pytest.mark.unit
def test_anila_as_tool_method_returns_agent_tool_spec(_clean_anila_as_tool_method: None) -> None:
    """patch 後 ``agent.anila_as_tool(...)`` 應回 :class:`AgentTool` (而非 SDK 的 FunctionTool)。"""
    enable_anila_as_tool_method()
    sub = _make_sub_agent(name="lookup")

    spec = sub.anila_as_tool("ask_lookup", "查 lookup")  # type: ignore[attr-defined]
    assert isinstance(spec, AgentTool)
    assert spec.name == "ask_lookup"
    assert spec.description == "查 lookup"
    assert spec.prefix_strategy == "share"


@pytest.mark.unit
def test_enable_anila_as_tool_method_is_idempotent(_clean_anila_as_tool_method: None) -> None:
    """重複 enable 不該疊加或 raise。"""
    enable_anila_as_tool_method()
    first = Agent.anila_as_tool  # type: ignore[attr-defined]
    enable_anila_as_tool_method()
    second = Agent.anila_as_tool  # type: ignore[attr-defined]
    # 重新 install 後一定有 method,但因為每次都重 setattr,instance 可能不同;只要存在就 OK
    assert callable(first) and callable(second)


@pytest.mark.unit
def test_disable_anila_as_tool_method_removes_attribute(_clean_anila_as_tool_method: None) -> None:
    """:func:`disable_anila_as_tool_method` 應 cleanly 移除 method;重複呼叫不 raise。"""
    enable_anila_as_tool_method()
    assert hasattr(Agent, "anila_as_tool")
    disable_anila_as_tool_method()
    assert not hasattr(Agent, "anila_as_tool")
    # 重複 disable 也 OK (idempotent)
    disable_anila_as_tool_method()
    assert not hasattr(Agent, "anila_as_tool")


# ---------------------------------------------------------------------------
# 5. 多 agent 協作場景 — retriever + writer 都 as_tool 給 main agent 用
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_multi_agent_collaboration_two_subagents_as_tool() -> None:
    """兩個 sub-agent 分別 :func:`as_tool` 後,main agent 可正確各自 dispatch。

    驗證:
    * 兩個 spec 都正常產出。
    * 兩個 tool 都可加進同一個 :class:`ToolRegistry`。
    * 各自 dispatch 時路由到正確的 sub-agent runner (透過 capture)。
    """
    retriever = _make_sub_agent(name="retriever", instructions="Fetch top-5 chunks.")
    writer = _make_sub_agent(name="writer", instructions="Turn chunks into summary.")

    retriever_calls: list[str] = []
    writer_calls: list[str] = []

    async def retriever_runner(_agent: Agent[Any], prompt: str) -> str:
        retriever_calls.append(prompt)
        return '["chunk1", "chunk2", "chunk3"]'

    async def writer_runner(_agent: Agent[Any], prompt: str) -> str:
        writer_calls.append(prompt)
        return "綜合摘要: ..."

    # shorthand 包兩個 sub-agent
    retriever_spec = as_tool_full(retriever, "search", "搜尋 top-5 chunks", runner=retriever_runner)
    writer_spec = as_tool_full(writer, "summarize", "把 chunks 變摘要", runner=writer_runner)

    # 都丟進 main agent 的 registry
    registry = ToolRegistry()
    registry.add(retriever_spec.tool)
    registry.add(writer_spec.tool)
    assert "search" in registry.tools
    assert "summarize" in registry.tools

    # 各自 dispatch
    parent_ctx = _make_parent_context()
    tool_ctx = _make_tool_context(parent_ctx)

    search_result = asyncio.run(
        retriever_spec.tool.on_invoke_tool(
            tool_ctx, json.dumps({"prompt": "what is anila?", "context_summary": ""})
        )
    )
    summarize_result = asyncio.run(
        writer_spec.tool.on_invoke_tool(
            tool_ctx,
            json.dumps({"prompt": "把上面 chunks 變摘要", "context_summary": "chunk1, chunk2"}),
        )
    )

    # 各自的 runner 收到對應 prompt,沒互相串
    assert len(retriever_calls) == 1
    assert "what is anila?" in retriever_calls[0]
    assert len(writer_calls) == 1
    assert "把上面 chunks 變摘要" in writer_calls[0]

    # tool result 也對
    assert "chunk1" in search_result
    assert "綜合摘要" in summarize_result


@pytest.mark.unit
def test_multi_agent_collaboration_preserves_sub_agent_identity() -> None:
    """多 sub-agent 場景下,每個 dispatch 收到的 ``agent`` 應指回自己的 sub-agent (不串)。"""
    retriever = _make_sub_agent(name="retriever")
    writer = _make_sub_agent(name="writer")

    captured_agents: list[Agent[Any]] = []

    async def runner(agent: Agent[Any], _prompt: str) -> str:
        captured_agents.append(agent)
        return "ok"

    retriever_spec = as_tool_full(retriever, "search", "...", runner=runner)
    writer_spec = as_tool_full(writer, "summarize", "...", runner=runner)

    tool_ctx = _make_tool_context(_make_parent_context())
    args = json.dumps({"prompt": "x", "context_summary": ""})
    asyncio.run(retriever_spec.tool.on_invoke_tool(tool_ctx, args))
    asyncio.run(writer_spec.tool.on_invoke_tool(tool_ctx, args))

    assert captured_agents == [retriever, writer]
