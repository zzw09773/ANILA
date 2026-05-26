"""P1-7 Streaming — `run_streamed` + 三層 StreamEvent。

本模組對應 enhancement roadmap **P1-7**:對齊 openai-agents-python
``src/agents/stream_events.py`` 的三層 streaming event,讓 anila-agent CLI /
TUI / future Web UI 可以「逐字 + 逐 tool call + 逐 agent switch」分層觀察 run。

三層 StreamEvent
================

1. **`RawResponseEvent`** — 來自 LLM 的 raw token / chunk。對應 CLI 顯示
   「正在打字」的 delta。
2. **`AgentUpdatedStreamEvent`** — agent state 變化:handoff 觸發、tool call
   開始、tool call 完成。對應 CLI 顯示「切換到 verifier」/「正在用 search」。
3. **`RunItemStreamEvent`** — high-level item(完整 message / tool call /
   tool result / handoff / final_output)。對應 CLI 顯示完整 panel。

設計取捨
========

- **本 task 不接 openai-agents 真實 streaming API**:`AnilaStreamRunner`
  接受 ``stream_source: AsyncIterator[dict]``,讓單元測試可注入 mock chunk。
  真實 SDK 整合在 P1-11 第二段(backend connection)做。
- **hook 整合 P0-1**:當 mock chunk 出現特定 ``kind`` 時,fire 對應 lifecycle
  hook(`AGENT_START` / `PRE_TOOL_USE` / `POST_TOOL_USE` / `HANDOFF` /
  `AGENT_END`)。
- **tracing 整合 P0-9**:整個 run 開一個 trace、tool / agent layer 開 span;
  raw token chunk 不開 span(避免噪音)。
- **三層 yield 順序**:同一個邏輯事件可能 yield 多層。例如 tool call 完成會
  先 yield ``AgentUpdatedStreamEvent`` (state change) 再 yield
  ``RunItemStreamEvent`` (item created)。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from anila_agent.core.concurrency import ToolCall, ToolResult
from anila_agent.core.events import EventBus
from anila_agent.core.hooks import (
    AgentEndInput,
    AgentStartInput,
    HandoffInput,
    HookEvent,
    HookRegistry,
    PostToolUseInput,
    PreToolUseInput,
    StopInput,
    fire,
)
from anila_agent.tracing import Tracer

logger = logging.getLogger(__name__)


# `RunItemStreamEvent.item_type` 的可能值;與 openai-agents 的 RunItem 種類
# 對齊,並額外加上 ``"final_output"`` 表示整個 run 結束的最終結果。
RunItemType = Literal[
    "message",
    "tool_call",
    "tool_result",
    "handoff",
    "final_output",
]


# ---------------------------------------------------------------------------
# StreamEvent 三層
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StreamEvent:
    """三層 StreamEvent 的共同基底。

    Attributes:
        type: event 種類字串(由 subclass 固定填值)。
        timestamp: event 產生時間(UTC)。
    """

    type: str = ""
    timestamp: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


@dataclass(frozen=True)
class RawResponseEvent(StreamEvent):
    """第一層:來自 LLM 的 raw token / chunk。

    對應 openai-agents 的 ``RawResponsesStreamEvent``;ANILA 版本把
    delta 直接抽成 ``str``,避免 SDK 內部 ``TResponseStreamEvent`` 型別洩漏。

    Attributes:
        delta: 此 chunk 的 partial text(token 或 fragment)。
        model: 產生此 chunk 的模型識別字串(例如 ``"gemma-3-4b-it"``)。
    """

    delta: str = ""
    model: str = ""
    type: str = field(default="raw_response_event", init=False)


@dataclass(frozen=True)
class AgentUpdatedStreamEvent(StreamEvent):
    """第二層:agent state 變化事件。

    涵蓋三種 sub-state:
    - **handoff**:``new_agent_name`` 為接手的 agent 名稱;``tool_call_*`` 皆為 None。
    - **tool_call_started**:``tool_call_started`` 為觸發的 :class:`ToolCall`。
    - **tool_call_completed**:``tool_call_completed`` 為 :class:`ToolResult`。

    使用 ``Optional`` 而非分成三個 subclass,是為了讓 consumer 用單一 isinstance
    判斷後再依屬性分流(對齊 openai-agents 單一 ``AgentUpdatedStreamEvent`` 風格)。

    Attributes:
        new_agent_name: 當 handoff 發生時為接手 agent 名稱;其他情況為 None。
        tool_call_started: 當 tool call 即將開始時填;其他情況為 None。
        tool_call_completed: 當 tool call 完成時填;其他情況為 None。
    """

    new_agent_name: str | None = None
    tool_call_started: ToolCall | None = None
    tool_call_completed: ToolResult | None = None
    type: str = field(default="agent_updated_stream_event", init=False)


@dataclass(frozen=True)
class RunItemStreamEvent(StreamEvent):
    """第三層:high-level item event。

    對齊 openai-agents 的 ``RunItemStreamEvent``;承載完整的 message /
    tool call / tool result / handoff / final_output item。

    ``item`` 為對應 item 的 dataclass(或 dict);本層不限制具體型別,
    讓 consumer 根據 ``item_type`` 做 downcast。

    Attributes:
        item_type: 此 item 的種類(見 :data:`RunItemType`)。
        item: 完整 item dataclass / dict。
    """

    item_type: RunItemType = "message"
    item: Any = None
    type: str = field(default="run_item_stream_event", init=False)


# ---------------------------------------------------------------------------
# AnilaStreamRunner
# ---------------------------------------------------------------------------


# Mock chunk source 的 chunk 格式約定(讓單元測試可注入):
# - ``{"kind": "raw_token", "delta": "...", "model": "..."}``
# - ``{"kind": "agent_started", "agent": "..."}``
# - ``{"kind": "tool_started", "tool": "...", "args": {...}, "call_id": "..."}``
# - ``{"kind": "tool_completed", "tool": "...", "call_id": "...", "output": ...}``
# - ``{"kind": "handoff", "from": "...", "to": "..."}``
# - ``{"kind": "message", "text": "..."}``
# - ``{"kind": "agent_ended", "agent": "...", "output": ...}``
StreamChunk = dict[str, Any]


class AnilaStreamRunner:
    """以 ``run_streamed`` 介面把 agent run 轉成三層 StreamEvent 串流。

    本 task 不接 openai-agents 真實 streaming API — `run_streamed`
    接受 ``stream_source: AsyncIterator[StreamChunk]`` 為輸入,從 mock
    chunk 推導三層 event。當 P1-11 第二段做真實 SDK 整合時,只需新增
    一支 adapter 把 SDK 的 stream_events 轉成 StreamChunk 即可。

    整合層
    ======

    - **P0-1 lifecycle hook**:當收到對應 kind 的 chunk 時,fire 對應
      hook(`AGENT_START` / `PRE_TOOL_USE` / `POST_TOOL_USE` / `HANDOFF` /
      `AGENT_END` / `STOP`)。
    - **P0-9 tracing**:整個 run 開一個 trace(``"agent.run.streamed"``),
      tool call 與 agent switch 各自開 span(parent 為 run trace 頂層);
      raw token chunk 不開 span(避免每 token 一筆事件)。
    """

    def __init__(
        self,
        registry: HookRegistry,
        bus: EventBus,
        *,
        tracer: Tracer | None = None,
        default_agent_name: str = "root",
    ) -> None:
        """初始化 runner。

        Args:
            registry: P0-1 hook registry — runner 在對應事件 fire hook chain。
            bus: in-process EventBus — runner 同時 emit 觀測事件(供 renderer / CLI 用)。
            tracer: P0-9 tracer — runner 對整個 run 開 trace、tool / agent 開 span。
                預設為 ``None`` 代表不開 trace(測試或不需要觀測時用)。
            default_agent_name: 若 chunk 沒帶 agent name 時的預設值。
        """
        self._registry = registry
        self._bus = bus
        self._tracer = tracer
        self._default_agent_name = default_agent_name

    async def run_streamed(
        self,
        stream_source: AsyncIterator[StreamChunk],
        *,
        agent_name: str | None = None,
        model: str = "",
    ) -> AsyncIterator[StreamEvent]:
        """從 mock chunk source 推導三層 StreamEvent 並 yield。

        Args:
            stream_source: 真實 SDK 的 stream events 透過 adapter 包成此格式;
                測試可用 ``async def gen(): yield {...}`` pattern 注入。
            agent_name: 整個 run 的 root agent 名稱;未指定時用 ``default_agent_name``。
            model: 用於 ``RawResponseEvent.model`` 的模型識別字串。

        Yields:
            StreamEvent: 三層中的某一層;順序與 chunk 順序對齊。整個 run
            結束時必定 yield 一個 ``RunItemStreamEvent(item_type="final_output", ...)``。
        """
        current_agent = agent_name or self._default_agent_name
        # collect 累積的 raw delta 拼出 final_output text(若 chunk 流沒給 final
        # message,就用 raw delta 拼出來當 fallback)。
        delta_buffer: list[str] = []
        # 累積的完整 message text(來自 ``message`` kind chunk)。
        message_buffer: list[str] = []
        # 紀錄已完成 tool call 的 result,供 final_output collect 用。
        completed_tools: list[ToolResult] = []
        # 用 dict 紀錄 in-flight tool call 的 ToolCall(以 call_id 為 key),
        # 在 ``tool_completed`` chunk 抵達時可從這查到原 args。
        in_flight_tools: dict[str, ToolCall] = {}
        # 最終 agent 輸出(來自 ``agent_ended`` chunk);若無則由 message_buffer 或
        # delta_buffer 推導。
        explicit_final_output: Any = None
        explicit_final_set = False

        # 整個 run 開一個 trace;tool / agent layer 開 span;raw token 不開。
        trace_ctx = (
            self._tracer.start_trace(
                "agent.run.streamed",
                metadata={"agent": current_agent, "model": model},
            )
            if self._tracer is not None
            else _null_context()
        )

        with trace_ctx:
            self._bus.emit("stream_started", agent=current_agent)

            async for chunk in stream_source:
                kind = chunk.get("kind", "")

                if kind == "raw_token":
                    delta = str(chunk.get("delta", ""))
                    delta_buffer.append(delta)
                    yield RawResponseEvent(
                        delta=delta,
                        model=str(chunk.get("model", model)),
                    )

                elif kind == "agent_started":
                    new_agent = str(chunk.get("agent", current_agent))
                    current_agent = new_agent
                    # 開 agent span 但不長久持有 — agent_ended 來臨時不顯式 close,
                    # tracer 內部已用 context manager 在 run 結束時清理。
                    if self._tracer is not None:
                        with self._tracer.start_span(
                            "agent.start",
                            attributes={"agent.name": new_agent},
                        ):
                            pass
                    await fire(
                        self._registry,
                        HookEvent.AGENT_START,
                        AgentStartInput(agent_name=new_agent),
                        bus=self._bus,
                    )
                    yield AgentUpdatedStreamEvent(new_agent_name=new_agent)

                elif kind == "tool_started":
                    tool_name = str(chunk.get("tool", ""))
                    args_raw = chunk.get("args") or {}
                    args = args_raw if isinstance(args_raw, dict) else {}
                    call_id = str(chunk.get("call_id", ""))
                    tool_call = ToolCall(name=tool_name, args=args, call_id=call_id)
                    in_flight_tools[call_id] = tool_call

                    if self._tracer is not None:
                        with self._tracer.start_span(
                            "tool.call",
                            attributes={
                                "tool.name": tool_name,
                                "tool.call_id": call_id,
                            },
                        ):
                            pass
                    await fire(
                        self._registry,
                        HookEvent.PRE_TOOL_USE,
                        PreToolUseInput(
                            tool_name=tool_name,
                            tool_input=args,
                            tool_call_id=call_id or None,
                            agent_name=current_agent,
                        ),
                        tool_name=tool_name,
                        bus=self._bus,
                    )
                    yield AgentUpdatedStreamEvent(tool_call_started=tool_call)
                    yield RunItemStreamEvent(
                        item_type="tool_call",
                        item=tool_call,
                    )

                elif kind == "tool_completed":
                    tool_name = str(chunk.get("tool", ""))
                    call_id = str(chunk.get("call_id", ""))
                    output = chunk.get("output")
                    error = chunk.get("error")
                    tool_result = ToolResult(
                        call_id=call_id,
                        output=output,
                        error=str(error) if error is not None else None,
                    )
                    completed_tools.append(tool_result)
                    original = in_flight_tools.pop(call_id, None)
                    original_args = original.args if original else {}

                    if self._tracer is not None:
                        with self._tracer.start_span(
                            "tool.result",
                            attributes={
                                "tool.name": tool_name,
                                "tool.call_id": call_id,
                                "tool.error": tool_result.error,
                            },
                        ):
                            pass
                    await fire(
                        self._registry,
                        HookEvent.POST_TOOL_USE,
                        PostToolUseInput(
                            tool_name=tool_name,
                            tool_input=original_args,
                            tool_output=output,
                            tool_call_id=call_id or None,
                            agent_name=current_agent,
                        ),
                        tool_name=tool_name,
                        bus=self._bus,
                    )
                    yield AgentUpdatedStreamEvent(tool_call_completed=tool_result)
                    yield RunItemStreamEvent(
                        item_type="tool_result",
                        item=tool_result,
                    )

                elif kind == "handoff":
                    from_agent = str(chunk.get("from", current_agent))
                    to_agent = str(chunk.get("to", current_agent))
                    current_agent = to_agent

                    if self._tracer is not None:
                        with self._tracer.start_span(
                            "agent.handoff",
                            attributes={
                                "agent.from": from_agent,
                                "agent.to": to_agent,
                            },
                        ):
                            pass
                    await fire(
                        self._registry,
                        HookEvent.HANDOFF,
                        HandoffInput(from_agent=from_agent, to_agent=to_agent),
                        bus=self._bus,
                    )
                    yield AgentUpdatedStreamEvent(new_agent_name=to_agent)
                    yield RunItemStreamEvent(
                        item_type="handoff",
                        item={"from": from_agent, "to": to_agent},
                    )

                elif kind == "message":
                    text = str(chunk.get("text", ""))
                    message_buffer.append(text)
                    yield RunItemStreamEvent(
                        item_type="message",
                        item={"text": text, "agent": current_agent},
                    )

                elif kind == "agent_ended":
                    explicit_final_output = chunk.get("output")
                    explicit_final_set = True
                    ended_agent = str(chunk.get("agent", current_agent))
                    await fire(
                        self._registry,
                        HookEvent.AGENT_END,
                        AgentEndInput(
                            agent_name=ended_agent,
                            output=explicit_final_output,
                        ),
                        bus=self._bus,
                    )

                else:
                    logger.warning("unknown stream chunk kind: %r", kind)

            # 整個 chunk source 耗盡 — 推導 final_output。
            if explicit_final_set:
                final_output: Any = explicit_final_output
            elif message_buffer:
                final_output = "".join(message_buffer)
            else:
                final_output = "".join(delta_buffer)

            # Stop hook(對齊 P0-1 既有 AnilaRunHooks.on_agent_end 雙 fire 行為:
            # AgentEnd 已在上頭 fire,這邊只補 Stop)。
            await fire(
                self._registry,
                HookEvent.STOP,
                StopInput(
                    agent_name=current_agent,
                    final_output=final_output,
                    turns_used=0,
                ),
                bus=self._bus,
            )

            self._bus.emit("stream_ended", agent=current_agent)

            yield RunItemStreamEvent(
                item_type="final_output",
                item=final_output,
            )


# ---------------------------------------------------------------------------
# 工具:沒 tracer 時用的 null context manager
# ---------------------------------------------------------------------------


class _NullContext:
    """當 ``tracer`` 為 None 時用的 no-op context manager。

    避免在 ``run_streamed`` 內用 ``if self._tracer:`` 包整段 logic,改用
    ``with trace_ctx:`` 統一寫法。
    """

    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: Any) -> Literal[False]:
        return False


def _null_context() -> _NullContext:
    """回傳一個共用的 null context(不需要每次 new 一個 instance)。"""
    return _NullContext()


__all__ = [
    "AgentUpdatedStreamEvent",
    "AnilaStreamRunner",
    "RawResponseEvent",
    "RunItemStreamEvent",
    "RunItemType",
    "StreamChunk",
    "StreamEvent",
]
