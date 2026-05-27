"""P2-1 — ``as_tool`` shorthand:把 sub-agent 包成 :class:`AgentTool` 的簡潔 API。

本模組對應 openai-agents 上游 :meth:`agents.Agent.as_tool`(在 ``src/agents/agent.py``
第 508 行起)的 ANILA 等價物。設計目的:**讓開發者用慣 SDK 的 ``.as_tool()`` 介面也能
無痛叫到 ANILA 的 sub-routine 機制**(走 :func:`anila_agent.core.agent_tool.make_agent_tool`)。

跟上游 SDK ``Agent.as_tool`` 的差別:

============================  ==================================================  =================================================
維度                          openai-agents 上游 ``Agent.as_tool``                ANILA ``as_tool`` (本模組)
============================  ==================================================  =================================================
回傳型別                      ``FunctionTool``                                    :class:`AgentTool` (內含 ``.tool: FunctionTool``)
nested run                    走 SDK ``Runner`` 重新展開 conversation             走 :func:`make_agent_tool` + ``prefix_strategy``
prompt-cache prefix           無(每次 nested run 從 sub-agent instructions 起頭)  有(P1-1 ``"share"`` / ``"fork"`` 兩種)
hook                          走 SDK ``RunHooks``                                 走 ANILA ``HookRegistry``(P1-17 dispatch event)
timeout                       無(靠 ``RunConfig``)                                有(``DEFAULT_SUBAGENT_TIMEOUT_SECONDS``)
============================  ==================================================  =================================================

兩條路徑可共存:呼叫端若想用 SDK 原生 nested run,就用 ``agent.as_tool(...)``(SDK 自帶);
若想吃 ANILA prompt-cache + hook + timeout,就用本模組的 :func:`as_tool` free function
(或 :func:`enable_anila_as_tool_method` 把 ``.anila_as_tool`` 掛到 :class:`Agent` 上)。

範例 — 多 agent 協作:

.. code-block:: python

    from agents import Agent
    from anila_agent.core import as_tool

    retriever = Agent(name="retriever", instructions="只跑 vector_search 並回 top-5 chunks")
    writer = Agent(name="writer", instructions="把 chunks 變成中文摘要")

    main = Agent(
        name="main",
        instructions="收到 query 後先 search 再 summarize",
        tools=[
            as_tool(retriever, "search", "搜尋相關 chunks").tool,
            as_tool(writer, "summarize", "把 chunks 變摘要").tool,
        ],
    )

跟 :func:`make_agent_tool` 的選擇準則:

* **shorthand**(本模組 :func:`as_tool`):只想設 ``name`` + ``description``,其他預設值
  夠用(``prefix_strategy="share"`` / 預設 timeout / 預設 metadata)。對齊 SDK ``Agent.as_tool``。
* **full control**(:func:`make_agent_tool`):需要客製 ``prefix_strategy`` / ``fork_point``
  / ``timeout_seconds`` / ``metadata`` / ``runner`` / ``tracer`` / ``hook_registry`` /
  ``event_bus`` 任一項時。

兩者最終都產同樣的 :class:`AgentTool` 物件,本模組只是 thin wrapper(沒有額外 runtime
邏輯),因此既有 P0-8 行為 100% 保留,效能也無差別。
"""

from __future__ import annotations

from typing import Any

from agents import Agent

from anila_agent.core.agent_tool import (
    DEFAULT_SUBAGENT_TIMEOUT_SECONDS,
    AgentTool,
    PrefixStrategy,
    SubAgentRunner,
    make_agent_tool,
)
from anila_agent.core.events import EventBus
from anila_agent.core.hooks import HookRegistry
from anila_agent.tools.base import ToolMetadata
from anila_agent.tracing import Tracer


def as_tool(
    sub_agent: Agent[Any],
    tool_name: str | None = None,
    tool_description: str | None = None,
    *,
    prefix_strategy: PrefixStrategy = "share",
) -> AgentTool:
    """把 sub-agent 包成 :class:`AgentTool` 的 shorthand。

    API shape 對齊 openai-agents 上游 :meth:`agents.Agent.as_tool` 的前兩個必傳參數
    (``tool_name`` / ``tool_description``),其餘進階參數(``prefix_strategy``)用 keyword
    給出,並全部 delegate 給 :func:`make_agent_tool`。

    Args:
        sub_agent: 被包裝的子 agent(對應上游 ``self``)。
        tool_name: tool 名稱(LLM 看到的);若為 ``None`` 走
            :func:`make_agent_tool` 預設(``f"call_{sub_agent.name}"``)。
        tool_description: tool 描述;若為 ``None`` 走 :func:`make_agent_tool` 預設
            (從 ``sub_agent.instructions`` 推導前 ~100 字)。
        prefix_strategy: P1-1 prompt-cache prefix 策略,預設 ``"share"`` 與 P0-8 對齊。
            若呼叫端要 ``"fork"`` / 客製 ``fork_point`` / ``timeout`` / ``metadata`` /
            tracer / hooks,**請直接用 :func:`make_agent_tool`**;本 shorthand 刻意維持
            極簡 surface 以對齊上游 SDK。

    Returns:
        :class:`AgentTool` — 內含已組好的 ``tool`` (FunctionTool),可餵給 parent agent
        的 tools list 或 :func:`register_agent_as_tool`。

    Example:
        >>> from agents import Agent
        >>> from anila_agent.core import as_tool
        >>> sub = Agent(name="retriever", instructions="跑 vector_search 回 top-5 chunks")
        >>> spec = as_tool(sub, "search", "搜尋相關 chunks")
        >>> spec.name
        'search'
        >>> spec.prefix_strategy
        'share'

    Note:
        本 function 跟 :func:`make_agent_tool` 行為**完全等價**(只是 surface 較小)。
        詳見 :mod:`anila_agent.core.agent_tool` module docstring 對 sub-routine pattern
        的完整說明,以及 ``docs/agent-tool-vs-as-tool.md`` 對兩條路徑的決策對照。
    """
    return make_agent_tool(
        sub_agent,
        name=tool_name,
        description=tool_description,
        prefix_strategy=prefix_strategy,
    )


def as_tool_full(
    sub_agent: Agent[Any],
    tool_name: str | None = None,
    tool_description: str | None = None,
    *,
    metadata: ToolMetadata | None = None,
    prefix_strategy: PrefixStrategy = "share",
    fork_point: int | None = None,
    timeout_seconds: float = DEFAULT_SUBAGENT_TIMEOUT_SECONDS,
    runner: SubAgentRunner | None = None,
    tracer: Tracer | None = None,
    hook_registry: HookRegistry | None = None,
    event_bus: EventBus | None = None,
) -> AgentTool:
    """:func:`as_tool` 的 full-control 版本(等同 :func:`make_agent_tool`)。

    保留這個 alias 是讓「想用 `.as_tool` 命名風格但又需要全部進階參數」的呼叫端不必
    切換到另一個 function。實作純 delegation,不引入任何額外邏輯。
    """
    return make_agent_tool(
        sub_agent,
        name=tool_name,
        description=tool_description,
        metadata=metadata,
        prefix_strategy=prefix_strategy,
        fork_point=fork_point,
        timeout_seconds=timeout_seconds,
        runner=runner,
        tracer=tracer,
        hook_registry=hook_registry,
        event_bus=event_bus,
    )


# ---------------------------------------------------------------------------
# Opt-in:把 ``anila_as_tool`` method 掛到 :class:`agents.Agent` 上
# ---------------------------------------------------------------------------


# attribute 名稱刻意不取 ``as_tool``,避免覆蓋上游 SDK 同名 method(SDK 版回
# ``FunctionTool``、走 nested ``Runner.run``;本 ANILA 版回 ``AgentTool`` spec、走
# prompt-cache prefix dispatch)。兩者語意不同,共存才安全。
_ANILA_AS_TOOL_METHOD_NAME = "anila_as_tool"


def enable_anila_as_tool_method() -> None:
    """把 ``anila_as_tool`` method 掛到 :class:`agents.Agent` class 上(idempotent)。

    呼叫一次後,所有 ``Agent`` 實體都可寫:

    .. code-block:: python

        spec = retriever.anila_as_tool("search", "搜尋相關 chunks")
        main.tools.append(spec.tool)

    本 function:

    * 不覆蓋上游 SDK 的 ``Agent.as_tool`` — 兩者透過不同名共存。
    * **idempotent**:重複呼叫不會疊加,也不會 raise。
    * 安裝後若呼叫端從未 import 本 module,則 `Agent` 不受影響(P0-8 預設行為保留)。

    一般不建議在 library code 內呼叫(會有 import-side-effect);較好的位置是:

    * 應用啟動 bootstrap(例如 ``main.py`` 開頭)
    * 測試 fixture 一次性啟用

    若要 disable / 還原,呼叫 :func:`disable_anila_as_tool_method`。
    """

    def _method(
        self: Agent[Any],
        tool_name: str | None = None,
        tool_description: str | None = None,
        *,
        prefix_strategy: PrefixStrategy = "share",
    ) -> AgentTool:
        return as_tool(
            self,
            tool_name=tool_name,
            tool_description=tool_description,
            prefix_strategy=prefix_strategy,
        )

    _method.__name__ = _ANILA_AS_TOOL_METHOD_NAME
    _method.__qualname__ = f"Agent.{_ANILA_AS_TOOL_METHOD_NAME}"
    _method.__doc__ = (
        "ANILA P2-1 shorthand — 把 self 包成 :class:`AgentTool` (走 prompt-cache prefix)。\n\n"
        "與 SDK 原生 ``Agent.as_tool`` 不同:回 :class:`AgentTool` 而非 ``FunctionTool``,且走 "
        "ANILA :func:`make_agent_tool` 的 sub-routine dispatch(P1-1 prefix / P1-17 hook / timeout)。"
    )

    setattr(Agent, _ANILA_AS_TOOL_METHOD_NAME, _method)


def disable_anila_as_tool_method() -> None:
    """還原 :func:`enable_anila_as_tool_method` 的安裝(idempotent)。

    主要供測試在 teardown 把 :class:`Agent` 還原乾淨,避免 method 殘留汙染其他測試。
    """
    if hasattr(Agent, _ANILA_AS_TOOL_METHOD_NAME):
        delattr(Agent, _ANILA_AS_TOOL_METHOD_NAME)


__all__ = [
    "as_tool",
    "as_tool_full",
    "disable_anila_as_tool_method",
    "enable_anila_as_tool_method",
]
