"""P2-14 ``run_demo_loop`` — streaming demo REPL,對齊上游 ``agents.repl.run_demo_loop``。

本模組對應 enhancement roadmap **P2-14**:給 developer 試新 agent 的「一行打開
streaming demo」shortcut。對齊上游 ``openai-agents-python/src/agents/repl.py`` 的
``run_demo_loop`` 寫法,但接 ANILA 自家三層 StreamEvent + HITL approval + cost
summary + tracing。

# 設計取捨

* **agent 形狀**:本 task 不接 openai-agents SDK 真實 ``Runner.run_streamed``,
  改接 ``Callable[[str], AsyncIterator[StreamChunk]]`` — 開發者用 mock chunk 立刻
  跑;真實 SDK 整合(P1-11)時只需把 SDK 的 ``stream_events`` 轉成同形狀 chunk。
* **不取代 ``cli/app.py``**:既有 REPL(177 行)有 slash command / FileHistory /
  metrics,功能更完整;本 helper 只給「想看 streaming demo 跑起來」的快速入口。
* **input provider 抽 callable**:預設用 ``prompt_toolkit.PromptSession``,測試
  可注入 stub callable 直接傳入 user input list。
* **HITL pause 偵測**:整段 stream 跑完後若 :class:`RunState` 處於 ``is_paused``,
  改進 :func:`anila_agent.cli.approval_repl.run_approval_repl`;若 user 在
  approval REPL 內 ``continue``,本 helper 不主動 retry 該輪 — 由 caller 決定下
  一步(對齊上游 SDK 把 ``RunState`` 還給 caller 的契約)。
* **cost summary**:每輪 LLM call(``raw_token`` chunk 的 model)用簡單啟發式
  記到 :class:`anila_agent.core.cost_tracker.CostTracker`;退出 loop 前印出總額。
* **tracing**:每輪開一個 ``agent.demo.turn`` trace(P0-9),loop 結束印
  summary。

# 為什麼要寫這個

* 給文件 / 教學用:``python -m anila_agent.cli.run_demo_loop`` 一條指令就能看到
  P1-7 streaming + P1-8 HITL + P1-15 cost summary 全部串起來。
* 給 sub-agent template 使用者用:clone 一個新 agent 後想「立刻看看跑起來長
  什麼樣子」,不必先設定 yaml / 接 LLM。
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from anila_agent.cli.approval_repl import run_approval_repl
from anila_agent.cli.demo_agents import (
    DemoAgent,
    echo_agent_stream,
    list_demo_agents,
    weather_agent_stream,
)
from anila_agent.cli.stream_renderer import render_stream
from anila_agent.core.cost_tracker import CostTracker, PricingRegistry
from anila_agent.core.events import EventBus
from anila_agent.core.hooks import HookRegistry
from anila_agent.core.run_state import (
    HITLController,
    JsonFileStateStore,
    RunState,
)
from anila_agent.core.streaming import (
    AnilaStreamRunner,
    RawResponseEvent,
    StreamChunk,
    StreamEvent,
)
from anila_agent.tracing import Tracer

logger = logging.getLogger(__name__)


# Async input provider:接收 prompt 字串、回傳一行 user input(async)。預設用
# prompt_toolkit ``PromptSession.prompt_async``;測試可注入 stub iterator coroutine。
AsyncInputProvider = Callable[[str], Awaitable[str]]


DEFAULT_STOP_WORDS: tuple[str, ...] = ("quit", "exit")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _default_input_provider() -> AsyncInputProvider:
    """建立 prompt_toolkit 預設 PromptSession,並包成 :data:`AsyncInputProvider`。

    抽成 async factory 是為了避免 module import 時就建 PromptSession(可能影響
    pytest collect 時的 stdin)。
    """
    from prompt_toolkit import PromptSession

    session: PromptSession[str] = PromptSession()

    async def _provider(prompt: str) -> str:
        return await session.prompt_async(prompt)

    return _provider


def _list_to_async_provider(inputs: Iterable[str]) -> AsyncInputProvider:
    """把同步 list/iterable 包成 async input provider — 測試專用 helper。

    每次呼叫吐下一筆;耗盡時 raise ``EOFError`` 模擬 Ctrl+D。
    """
    it = iter(inputs)

    async def _provider(prompt: str) -> str:
        try:
            return next(it)
        except StopIteration as exc:
            raise EOFError("stub inputs exhausted") from exc

    return _provider


async def _agent_to_chunks(
    agent: DemoAgent,
    user_input: str,
) -> AsyncIterator[StreamChunk]:
    """把 demo agent callable 的回傳 async iterator 原樣轉發。

    抽成 helper 是為了在 callable 直接 raise 時把錯誤包成一個 ``message`` chunk,
    避免整個 stream runner 中斷。
    """
    try:
        async for chunk in agent(user_input):
            yield chunk
    except Exception as exc:
        logger.exception("demo agent raised; converting to error message chunk")
        yield {
            "kind": "message",
            "text": f"[demo agent error] {type(exc).__name__}: {exc}",
        }


def _record_cost_from_event(
    tracker: CostTracker,
    event: StreamEvent,
    *,
    default_completion_tokens: int = 1,
) -> None:
    """每收到一個 ``RawResponseEvent`` 累計 cost — 每 delta 視為 1 token output。

    這是 demo 用的簡單啟發式;真實 SDK 整合(P1-11)時要改用 LLM response 的
    ``usage`` 欄位精確計費。本 helper 只是讓「跑完看到一個非零 cost summary」
    的 UX 成立。
    """
    if isinstance(event, RawResponseEvent):
        model = event.model or "anila-gemma4"
        tracker.record(
            model=model,
            prompt_tokens=0,
            completion_tokens=default_completion_tokens,
        )


def _print_cost_summary(tracker: CostTracker, console: Console) -> None:
    """印出 CostTracker 的 summary panel。"""
    console.print(
        Panel(
            Text(tracker.summary_str()),
            title="cost summary",
            border_style="cyan",
        )
    )


def _print_trace_summary(turn_count: int, console: Console) -> None:
    """印出 demo loop 結束時的 trace 摘要(用 turn 計數代替詳細 trace 樹)。"""
    console.print(
        Panel(
            Text(f"completed turns: {turn_count}"),
            title="trace summary",
            border_style="magenta",
        )
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def run_demo_loop_async(
    agent: DemoAgent,
    *,
    store: JsonFileStateStore | None = None,
    stop_words: tuple[str, ...] = DEFAULT_STOP_WORDS,
    input_provider: AsyncInputProvider | None = None,
    console: Console | None = None,
    cost_tracker: CostTracker | None = None,
    tracer: Tracer | None = None,
    max_turns: int = 1000,
    prompt: str = " > ",
) -> dict[str, Any]:
    """跑 streaming demo REPL(async 版本)。

    本函式對齊上游 ``agents.repl.run_demo_loop``,但接 ANILA 自家:

    * P1-7 :class:`AnilaStreamRunner` 三層 StreamEvent。
    * P1-8 :class:`HITLController` paused state → :func:`run_approval_repl`。
    * P1-15 :class:`CostTracker` 累計 token + USD。
    * P0-9 :class:`Tracer` 每輪開一個 trace。

    流程
    ====

    1. user input(``input_provider``);遇 stop word / EOF 結束。
    2. 把 input 餵給 ``agent`` callable → ``AsyncIterator[StreamChunk]``。
    3. ``AnilaStreamRunner.run_streamed`` 推導三層 StreamEvent。
    4. :func:`render_stream` 即時印到 ``console``;同時 :func:`_record_cost_from_event`
       累計 cost。
    5. 若 ``store`` 非 None 且 store 內存在 paused state(同 ``run_id``),整輪
       結束後跳進 approval REPL。
    6. loop 退出時印 cost summary + trace summary。

    Args:
        agent: ``Callable[[str], AsyncIterator[StreamChunk]]`` — demo agent stub
            或真實 SDK adapter。可用 :func:`echo_agent_stream` /
            :func:`weather_agent_stream` 測試。
        store: 可選的 :class:`JsonFileStateStore`;若提供,每輪結束會檢查同名
            run_id 是否處於 paused 狀態,觸發 approval REPL。預設 ``None``。
        stop_words: 觸發退出 loop 的 user input 字串(case-insensitive)。
        input_provider: 提供 user input 的 async callable;預設用
            ``prompt_toolkit.PromptSession``。測試可注入 stub。
        console: 輸出用的 rich :class:`Console`;預設新建一個。測試可注入
            ``Console(file=io.StringIO())``。
        cost_tracker: 自訂 cost tracker;預設新建一個帶 :class:`PricingRegistry`
            預設表的 tracker。
        tracer: 自訂 tracer;預設新建一個 vanilla :class:`Tracer`。
        max_turns: 防呆上限,避免 stub input 無窮 yield 同字串時 loop 不結束。
        prompt: 顯示給 user 的 prompt 字串,預設 ``" > "``(對齊上游)。

    Returns:
        ``{"turns": int, "cost_usd": float, "tracker": CostTracker}`` — 結束時
        的統計摘要,方便 caller 寫測試或進一步處理。
    """
    out = console or Console()
    ask = input_provider or await _default_input_provider()
    tracker = cost_tracker or CostTracker(PricingRegistry())
    used_tracer = tracer or Tracer()

    runner = AnilaStreamRunner(
        registry=HookRegistry(),
        bus=EventBus(),
        tracer=used_tracer,
    )

    turn_count = 0
    out.print(
        Text(
            "anila demo loop — type 'help' for tips, "
            f"{'/'.join(stop_words)} to exit.",
            style="dim",
        )
    )

    for _ in range(max_turns):
        try:
            user_input = await ask(prompt)
        except (EOFError, KeyboardInterrupt):
            out.print("")
            break

        stripped = user_input.strip()
        if not stripped:
            continue
        if stripped.lower() in stop_words:
            break

        # 每輪開一個 trace(P0-9)— 用 metadata 帶 user input 摘要。
        with used_tracer.start_trace(
            "agent.demo.turn",
            metadata={"turn": turn_count + 1, "user_input": stripped[:120]},
        ):
            chunks = _agent_to_chunks(agent, stripped)
            events = runner.run_streamed(chunks, model="demo")

            async def _events_with_cost(
                src: AsyncIterator[StreamEvent],
            ) -> AsyncIterator[StreamEvent]:
                """同時 render 一份 + 累計 cost 一份。"""
                async for ev in src:
                    _record_cost_from_event(tracker, ev)
                    yield ev

            await render_stream(_events_with_cost(events), console=out)

        turn_count += 1

        # P1-8 HITL pause 偵測:若 store 內有同 run_id 的 paused state,跳入
        # approval REPL。store 為 None 時略過(純 streaming demo)。
        if store is not None:
            _maybe_run_approval_repl(store, console=out)

    # loop 結束 — 印 cost summary + trace summary。
    out.print("")
    _print_cost_summary(tracker, out)
    _print_trace_summary(turn_count, out)

    return {
        "turns": turn_count,
        "cost_usd": tracker.total_usd,
        "tracker": tracker,
    }


def run_demo_loop(
    agent: DemoAgent,
    *,
    store: JsonFileStateStore | None = None,
    stop_words: tuple[str, ...] = DEFAULT_STOP_WORDS,
    input_provider: AsyncInputProvider | None = None,
    console: Console | None = None,
    cost_tracker: CostTracker | None = None,
    tracer: Tracer | None = None,
    max_turns: int = 1000,
    prompt: str = " > ",
) -> dict[str, Any]:
    """同步版 :func:`run_demo_loop_async` — 內部用 ``asyncio.run`` 跑。

    給「快速跑 demo,不想自己 wrap event loop」的 caller 用。所有參數與 async
    版一致。
    """
    return asyncio.run(
        run_demo_loop_async(
            agent,
            store=store,
            stop_words=stop_words,
            input_provider=input_provider,
            console=console,
            cost_tracker=cost_tracker,
            tracer=tracer,
            max_turns=max_turns,
            prompt=prompt,
        )
    )


# ---------------------------------------------------------------------------
# Approval REPL bridge
# ---------------------------------------------------------------------------


def _maybe_run_approval_repl(
    store: JsonFileStateStore,
    *,
    console: Console,
) -> None:
    """如果 store 內有 paused state,跳進 :func:`run_approval_repl`。

    搜尋規則:列出 store 內所有 run_id,挑第一個 ``is_paused`` 的;若無 paused
    state,直接 return(不打擾使用者)。
    """
    run_ids = store.list_run_ids()
    for run_id in run_ids:
        try:
            state: RunState = store.load(run_id)
        except (FileNotFoundError, ValueError) as exc:
            logger.warning("failed to load run_state %s: %s", run_id, exc)
            continue
        if not state.is_paused:
            continue

        console.print(
            Text(
                f"\n[HITL paused] run_id={run_id} "
                f"reason={state.pause_reason.value if state.pause_reason else 'n/a'}",
                style="bold yellow",
            )
        )
        run_approval_repl(
            state,
            store,
            controller=HITLController(),
            console=console,
        )
        # 一次只處理一個 paused run,讓 user 對其他 paused state 下輪再決定。
        return


# ---------------------------------------------------------------------------
# `python -m anila_agent.cli.run_demo_loop` 入口
# ---------------------------------------------------------------------------


def _select_demo_agent(name: str) -> DemoAgent:
    """從 :func:`list_demo_agents` 選 agent;找不到 fallback echo。"""
    agents = list_demo_agents()
    if name in agents:
        return agents[name]
    # 不直接 raise,只印警告 — 讓 ``python -m ... badname`` 仍可跑。
    print(
        f"unknown demo agent: {name!r}; "
        f"available={sorted(agents.keys())!r}; fallback to 'echo'.",
        file=sys.stderr,
    )
    return echo_agent_stream


def _main() -> None:
    """CLI entry — ``python -m anila_agent.cli.run_demo_loop [agent_name]``。"""
    name = sys.argv[1] if len(sys.argv) > 1 else "weather"
    agent = _select_demo_agent(name)
    run_demo_loop(agent)


if __name__ == "__main__":
    _main()


__all__ = [
    "DEFAULT_STOP_WORDS",
    "AsyncInputProvider",
    "DemoAgent",
    "echo_agent_stream",
    "run_demo_loop",
    "run_demo_loop_async",
    "weather_agent_stream",
]
