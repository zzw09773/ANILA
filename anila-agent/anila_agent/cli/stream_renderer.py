"""P1-7 CLI streaming renderer — 用 rich 即時把三層 StreamEvent 印到 terminal。

對應 enhancement roadmap **P1-7** 的 deliverable #2:CLI 端把 ``AnilaStreamRunner``
產出的 ``StreamEvent`` 流即時繪到 terminal。三層 event 各有顯示樣式:

- ``RawResponseEvent`` — 逐字 inline append(模擬「正在打字」)。
- ``AgentUpdatedStreamEvent`` — 用色彩標籤標出 tool call / handoff。
- ``RunItemStreamEvent`` — 完整 panel 印出 message / tool result / final_output。

設計取捨
========

- 為了單元測試方便 capture stdout,renderer 接受 optional ``Console`` 參數;
  預設用 module-level 共用 console(對齊 ``anila_agent/cli/renderer.py``)。
- ``render_stream`` 是一個 async function,把整條 stream 跑完即可看到完整輸出;
  沒用 ``rich.Live`` 是因為三層 event 不會頻繁覆蓋同一行(token delta 是 inline
  append,不重畫)。若未來要做 spinner / progress bar 可再上 ``Live``。
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from anila_agent.core.streaming import (
    AgentUpdatedStreamEvent,
    RawResponseEvent,
    RunItemStreamEvent,
    StreamEvent,
)

_default_console = Console()


def _get_console(console: Console | None) -> Console:
    """回傳 caller 指定的 console 或 module-level default。"""
    return console if console is not None else _default_console


async def render_stream(
    events: AsyncIterator[StreamEvent],
    *,
    console: Console | None = None,
) -> None:
    """把整條 StreamEvent 串流即時繪到 terminal。

    Args:
        events: ``AnilaStreamRunner.run_streamed`` 回傳的 async iterator。
        console: 可選的 rich Console;測試可注入 ``Console(file=io.StringIO())``
            來 capture 輸出。
    """
    out = _get_console(console)

    async for event in events:
        if isinstance(event, RawResponseEvent):
            # 逐字 inline append — 不換行、不加 markdown。
            out.print(event.delta, end="", style="white", highlight=False)

        elif isinstance(event, AgentUpdatedStreamEvent):
            if event.new_agent_name is not None:
                out.print(
                    Text(f"\n[agent: {event.new_agent_name}]", style="bold magenta")
                )
            elif event.tool_call_started is not None:
                tc = event.tool_call_started
                out.print(
                    Text(
                        f"\n[tool start: {tc.name}({_brief_args(tc.args)})]",
                        style="bold cyan",
                    )
                )
            elif event.tool_call_completed is not None:
                tr = event.tool_call_completed
                if tr.error is not None:
                    out.print(
                        Text(
                            f"\n[tool error: {tr.call_id} :: {tr.error}]",
                            style="bold red",
                        )
                    )
                else:
                    out.print(
                        Text(
                            f"\n[tool done: {tr.call_id} -> {_brief(tr.output)}]",
                            style="bold green",
                        )
                    )

        elif isinstance(event, RunItemStreamEvent):
            if event.item_type == "message":
                # message 已由 raw_response 逐字印過,這邊不重複;但仍可選擇換行做斷句。
                out.print("")
            elif event.item_type == "tool_call":
                # tool_call panel 已由 AgentUpdatedStreamEvent 印過 tool start;這邊略過。
                pass
            elif event.item_type == "tool_result":
                # 已由 AgentUpdatedStreamEvent 印過 tool done;略過避免重複。
                pass
            elif event.item_type == "handoff":
                item = event.item or {}
                from_a = item.get("from") if isinstance(item, dict) else "?"
                to_a = item.get("to") if isinstance(item, dict) else "?"
                out.print(
                    Text(f"\n[handoff: {from_a} -> {to_a}]", style="bold yellow")
                )
            elif event.item_type == "final_output":
                out.print(
                    Panel(
                        Text(_brief(event.item, max_len=2000)),
                        title="final output",
                        border_style="green",
                    )
                )


def _brief(value: Any, *, max_len: int = 120) -> str:
    """把任意值轉成簡短 str — 用於 tool output / final_output 摘要。"""
    s = repr(value) if not isinstance(value, str) else value
    if len(s) > max_len:
        return s[: max_len - 1] + "…"
    return s


def _brief_args(args: dict[str, Any]) -> str:
    """簡化的 args 顯示 — key=value 逗號分隔,每值 _brief(40)。"""
    if not args:
        return ""
    parts = [f"{k}={_brief(v, max_len=40)}" for k, v in args.items()]
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# `python -m anila_agent.cli.stream_renderer` demo 入口
# ---------------------------------------------------------------------------


async def _demo(prompt: str) -> None:
    """範例 usage — 接受 prompt 跑一個 mock streaming run 並 render 到 stdout。"""
    from anila_agent.core.events import EventBus
    from anila_agent.core.hooks import HookRegistry
    from anila_agent.core.streaming import AnilaStreamRunner
    from anila_agent.tracing import Tracer

    async def mock_chunks() -> AsyncIterator[dict[str, Any]]:
        """模擬一輪 agent run 的 chunk 串流 — agent_started -> raw deltas ->
        tool_started -> tool_completed -> message -> agent_ended。"""
        yield {"kind": "agent_started", "agent": "root"}
        for word in ("hello", " ", "from", " ", "anila", "."):
            yield {"kind": "raw_token", "delta": word, "model": "demo-model"}
            await asyncio.sleep(0.02)
        yield {
            "kind": "tool_started",
            "tool": "vector_search",
            "args": {"query": prompt},
            "call_id": "call_1",
        }
        await asyncio.sleep(0.05)
        yield {
            "kind": "tool_completed",
            "tool": "vector_search",
            "call_id": "call_1",
            "output": {"chunks": 3},
        }
        yield {"kind": "message", "text": "result ready"}
        yield {"kind": "agent_ended", "agent": "root", "output": "hello from anila."}

    runner = AnilaStreamRunner(
        registry=HookRegistry(),
        bus=EventBus(),
        tracer=Tracer(),
    )
    await render_stream(runner.run_streamed(mock_chunks(), model="demo-model"))


def _main() -> None:
    prompt = sys.argv[1] if len(sys.argv) > 1 else "demo prompt"
    asyncio.run(_demo(prompt))


if __name__ == "__main__":
    _main()


__all__ = ["render_stream"]
