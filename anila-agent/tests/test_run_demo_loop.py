"""P2-14 ``run_demo_loop`` unit tests。

驗證項目:
- :func:`run_demo_loop_async` basic flow:多輪 user input → 三層 StreamEvent
  正確 render 到 stdout。
- ``quit`` / ``exit`` stop word 觸發退出。
- EOFError(模擬 Ctrl+D)觸發退出。
- 整合 P1-7 stream event 印出對(用 capsys / Console buffer 驗 stdout)。
- 整合 P1-8 paused state → 顯示 pending + 模擬 approve。
- 整合 P1-15 cost summary 結尾印出。
- :mod:`anila_agent.cli.demo_agents` 兩個 stub(echo / weather)都可獨立跑。
"""

from __future__ import annotations

import io
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from rich.console import Console

from anila_agent.cli.demo_agents import (
    echo_agent_stream,
    list_demo_agents,
    weather_agent_stream,
)
from anila_agent.cli.run_demo_loop import (
    DEFAULT_STOP_WORDS,
    _list_to_async_provider,
    run_demo_loop_async,
)
from anila_agent.core.concurrency import ToolCall
from anila_agent.core.cost_tracker import CostTracker, PricingRegistry
from anila_agent.core.run_state import (
    HITLController,
    JsonFileStateStore,
    PauseReason,
    RunState,
)
from anila_agent.core.streaming import StreamChunk
from anila_agent.tracing import Tracer

# ---------------------------------------------------------------------------
# 工具:把 Console 包成 StringIO buffer,測試完讀回 stdout 內容
# ---------------------------------------------------------------------------


def _buffer_console() -> tuple[Console, io.StringIO]:
    """建一個寫到 StringIO 的 rich Console,回 (console, buffer)。

    ``force_terminal=False`` 確保 rich 不會插 ANSI 控制碼;``no_color=True``
    再保險把顏色關掉,assertion 才好找文字。
    """
    buf = io.StringIO()
    console = Console(
        file=buf,
        force_terminal=False,
        no_color=True,
        width=120,
    )
    return console, buf


# ---------------------------------------------------------------------------
# demo agent stubs 基本驗證
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_echo_agent_stream_emits_expected_chunks() -> None:
    """echo agent:agent_started → 多個 raw_token → agent_ended,output 為 echo: ...。"""
    chunks: list[StreamChunk] = []
    async for c in echo_agent_stream("hi"):
        chunks.append(c)

    kinds = [c["kind"] for c in chunks]
    assert kinds[0] == "agent_started"
    assert kinds[-1] == "agent_ended"
    # 每個字一個 raw_token。
    raw_tokens = [c for c in chunks if c["kind"] == "raw_token"]
    assert len(raw_tokens) == len("hi")
    assert "".join(c["delta"] for c in raw_tokens) == "hi"
    # 收尾 chunk 的 output 為 "echo: hi"。
    assert chunks[-1]["output"] == "echo: hi"


@pytest.mark.asyncio
async def test_weather_agent_stream_emits_tool_call_sequence() -> None:
    """weather agent:agent_started → raw_token+ → tool_started → tool_completed →
    message → agent_ended。"""
    chunks: list[StreamChunk] = []
    async for c in weather_agent_stream("Tokyo"):
        chunks.append(c)

    kinds = [c["kind"] for c in chunks]
    # 至少包含這六種 kind,順序大致符合。
    assert kinds[0] == "agent_started"
    assert "tool_started" in kinds
    assert "tool_completed" in kinds
    assert "message" in kinds
    assert kinds[-1] == "agent_ended"

    tool_started = next(c for c in chunks if c["kind"] == "tool_started")
    assert tool_started["tool"] == "get_weather"
    assert tool_started["args"]["city"] == "Tokyo"

    tool_completed = next(c for c in chunks if c["kind"] == "tool_completed")
    # call_id 必須跟 tool_started 對齊。
    assert tool_completed["call_id"] == tool_started["call_id"]
    assert tool_completed["output"]["city"] == "Tokyo"


def test_list_demo_agents_contains_echo_and_weather() -> None:
    agents = list_demo_agents()
    assert "echo" in agents
    assert "weather" in agents
    assert callable(agents["echo"])
    assert callable(agents["weather"])


# ---------------------------------------------------------------------------
# run_demo_loop_async basic flow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_demo_loop_async_quit_immediately() -> None:
    """user 直接打 quit → loop 退出,turns=0,cost=0。"""
    console, buf = _buffer_console()
    provider = _list_to_async_provider(["quit"])

    result = await run_demo_loop_async(
        echo_agent_stream,
        input_provider=provider,
        console=console,
    )

    assert result["turns"] == 0
    assert result["cost_usd"] == 0.0
    text = buf.getvalue()
    # 開頭歡迎訊息 + 結尾 cost / trace summary 必印出。
    assert "anila demo loop" in text
    assert "cost summary" in text
    assert "trace summary" in text


@pytest.mark.asyncio
async def test_run_demo_loop_async_exit_word_also_quits() -> None:
    """``exit`` 視同 ``quit``(都在 DEFAULT_STOP_WORDS 內)。"""
    assert "exit" in DEFAULT_STOP_WORDS
    console, _ = _buffer_console()
    provider = _list_to_async_provider(["exit"])

    result = await run_demo_loop_async(
        echo_agent_stream,
        input_provider=provider,
        console=console,
    )
    assert result["turns"] == 0


@pytest.mark.asyncio
async def test_run_demo_loop_async_eof_quits_gracefully() -> None:
    """input provider 丟 EOFError → loop 安全退出,不 raise。"""
    console, buf = _buffer_console()
    provider = _list_to_async_provider([])  # 立刻 EOF

    result = await run_demo_loop_async(
        echo_agent_stream,
        input_provider=provider,
        console=console,
    )
    assert result["turns"] == 0
    assert "cost summary" in buf.getvalue()


@pytest.mark.asyncio
async def test_run_demo_loop_async_empty_input_skipped() -> None:
    """空字串 input 應該跳過,不算一輪。"""
    console, _ = _buffer_console()
    provider = _list_to_async_provider(["", "   ", "quit"])

    result = await run_demo_loop_async(
        echo_agent_stream,
        input_provider=provider,
        console=console,
    )
    assert result["turns"] == 0


# ---------------------------------------------------------------------------
# P1-7 stream event 印出驗證(整合測)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_demo_loop_async_echo_one_turn_renders_stream() -> None:
    """echo agent 跑一輪 → raw_token delta 被印進 console;final_output panel 出現。"""
    console, buf = _buffer_console()
    provider = _list_to_async_provider(["hi", "quit"])

    result = await run_demo_loop_async(
        echo_agent_stream,
        input_provider=provider,
        console=console,
    )

    assert result["turns"] == 1
    text = buf.getvalue()
    # echo agent 把 "hi" 逐字 raw_token,render_stream 會 inline append "h" + "i"。
    # 為避免 rich 換行 / 樣式干擾,直接檢查 "hi" 出現過 + final output panel 標題。
    assert "hi" in text
    assert "final output" in text
    assert "echo: hi" in text


@pytest.mark.asyncio
async def test_run_demo_loop_async_weather_renders_tool_events() -> None:
    """weather agent 跑一輪 → tool start / tool done 訊息都印出來。"""
    console, buf = _buffer_console()
    provider = _list_to_async_provider(["Taipei", "quit"])

    result = await run_demo_loop_async(
        weather_agent_stream,
        input_provider=provider,
        console=console,
    )
    assert result["turns"] == 1
    text = buf.getvalue()
    assert "tool start" in text
    assert "get_weather" in text
    assert "tool done" in text
    assert "final output" in text


# ---------------------------------------------------------------------------
# P1-15 cost summary 結尾印出 + tracker 累計正確
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_demo_loop_async_records_cost(tmp_path: Path) -> None:
    """echo agent 一輪 → cost tracker 該記到 raw_token 數量的 completion tokens。"""
    console, buf = _buffer_console()
    provider = _list_to_async_provider(["abc", "quit"])

    # 用一個 fresh tracker / pricing — anila-gemma4 預設 0 USD,記 token 量就好。
    registry = PricingRegistry()
    tracker = CostTracker(registry)

    result = await run_demo_loop_async(
        echo_agent_stream,
        input_provider=provider,
        console=console,
        cost_tracker=tracker,
    )

    assert result["turns"] == 1
    # echo("abc") 會 emit 3 個 raw_token;每個記 1 completion token。
    # demo agent 把 model 寫成 "demo-echo",PricingRegistry 沒這條,允許 missing → $0。
    assert tracker.total_completion_tokens == 3
    assert tracker.total_records == 3
    # cost summary 一定印出來。
    text = buf.getvalue()
    assert "cost summary" in text
    assert "demo-echo" in text


# ---------------------------------------------------------------------------
# P1-8 HITL paused state → approval REPL 整合
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_demo_loop_async_triggers_approval_repl_on_paused_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """store 內預先放一個 paused state → 跑完一輪後 demo loop 偵測到 paused,
    跳進 approval REPL;測試把 approval REPL 的 input 換成 stub 模擬 approve +
    continue,驗證 state 變回 running。
    """
    store = JsonFileStateStore(tmp_path / "state")
    # 預先放一個 paused for approval 的 state。
    state = RunState.create(agent_name="demo")
    HITLController().pause(
        state,
        PauseReason.TOOL_APPROVAL,
        pending_tool_calls=[
            ToolCall(name="write_file", args={"path": "/tmp/x"}, call_id="c1"),
        ],
    )
    store.save(state)

    # demo loop user input:一輪 echo + quit。
    console, buf = _buffer_console()
    user_inputs = _list_to_async_provider(["hello", "quit"])

    # approval REPL 內部用 ``input`` 拿指令。monkeypatch 換成 stub:
    # approve c1 → continue。
    approval_cmds = iter(["approve c1", "continue"])

    def _fake_input(prompt: str = "") -> str:
        return next(approval_cmds)

    monkeypatch.setattr("builtins.input", _fake_input)

    result = await run_demo_loop_async(
        echo_agent_stream,
        store=store,
        input_provider=user_inputs,
        console=console,
    )

    assert result["turns"] == 1

    # 驗 state 已被 approval REPL 改成 running。
    reloaded = store.load(state.run_id)
    assert reloaded.status == "running"
    assert "c1" in reloaded.approved_tool_call_ids
    assert reloaded.pending_tool_calls == []

    # 應該印出 HITL paused 標籤。
    text = buf.getvalue()
    assert "HITL paused" in text
    assert state.run_id in text


@pytest.mark.asyncio
async def test_run_demo_loop_async_no_paused_state_skips_approval(
    tmp_path: Path,
) -> None:
    """store 內無 paused state → 不跳 approval REPL,直接結束。"""
    store = JsonFileStateStore(tmp_path / "state")
    # 放一個 running state(不是 paused)— 應該被略過。
    running = RunState.create(agent_name="demo")
    store.save(running)

    console, buf = _buffer_console()
    provider = _list_to_async_provider(["hi", "quit"])

    result = await run_demo_loop_async(
        echo_agent_stream,
        store=store,
        input_provider=provider,
        console=console,
    )
    assert result["turns"] == 1
    # 不應觸發 approval REPL。
    text = buf.getvalue()
    assert "HITL paused" not in text


# ---------------------------------------------------------------------------
# tracer 整合(P0-9)— trace summary 印出
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_demo_loop_async_prints_trace_summary() -> None:
    """跑完 N 輪後 trace summary 必印出 ``completed turns: N``。"""
    console, buf = _buffer_console()
    provider = _list_to_async_provider(["a", "b", "c", "quit"])

    result = await run_demo_loop_async(
        echo_agent_stream,
        input_provider=provider,
        console=console,
        tracer=Tracer(),
    )
    assert result["turns"] == 3
    text = buf.getvalue()
    assert "completed turns: 3" in text


# ---------------------------------------------------------------------------
# 防呆:max_turns 上限避免無窮 loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_demo_loop_async_respects_max_turns() -> None:
    """max_turns=2 + 無窮 yield 同字串 → 最多跑 2 輪就結束。"""
    console, _ = _buffer_console()

    # 自製無窮 input provider — 永遠回 "hi"(不 raise EOF,不 hit stop word)。
    async def _infinite(_prompt: str) -> str:
        return "hi"

    result = await run_demo_loop_async(
        echo_agent_stream,
        input_provider=_infinite,
        console=console,
        max_turns=2,
    )
    assert result["turns"] == 2


# ---------------------------------------------------------------------------
# Agent 內 raise 例外 → 不破壞 loop,回 error message chunk
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_demo_loop_async_handles_agent_error_gracefully() -> None:
    """demo agent stub 內 raise 例外 → 轉成 message chunk,loop 繼續往下走。"""

    async def _broken_agent(_: str) -> AsyncIterator[StreamChunk]:
        yield {"kind": "agent_started", "agent": "broken"}
        raise RuntimeError("boom")

    console, buf = _buffer_console()
    provider = _list_to_async_provider(["go", "quit"])

    result = await run_demo_loop_async(
        _broken_agent,
        input_provider=provider,
        console=console,
    )
    # broken agent 仍算一輪。
    assert result["turns"] == 1
    text = buf.getvalue()
    assert "demo agent error" in text
    assert "RuntimeError" in text


# ---------------------------------------------------------------------------
# 同步包裝 run_demo_loop
# ---------------------------------------------------------------------------


def test_run_demo_loop_sync_wrapper(monkeypatch: pytest.MonkeyPatch) -> None:
    """同步版 run_demo_loop:用 stub provider + buffer console 跑一輪。"""
    from anila_agent.cli.run_demo_loop import run_demo_loop

    console, buf = _buffer_console()

    # 直接餵 stub provider — sync wrapper 內部 asyncio.run 把 async function 跑掉。
    provider = _list_to_async_provider(["hi", "quit"])

    result = run_demo_loop(
        echo_agent_stream,
        input_provider=provider,
        console=console,
    )
    assert result["turns"] == 1
    text = buf.getvalue()
    assert "echo: hi" in text


# ---------------------------------------------------------------------------
# 工具:_list_to_async_provider 自己也要被測
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_to_async_provider_yields_in_order_then_eof() -> None:
    """list provider 該按順序吐 input,耗盡時 raise EOFError。"""
    provider = _list_to_async_provider(["a", "b"])

    assert await provider("p1") == "a"
    assert await provider("p2") == "b"
    with pytest.raises(EOFError):
        await provider("p3")
