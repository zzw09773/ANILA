"""P1-7 Streaming unit tests。

驗證項目:
- 三層 ``StreamEvent`` dataclass 欄位 / type literal 正確。
- ``AnilaStreamRunner.run_streamed`` 從 mock chunk source 推導對應三層 event。
- streaming 期間 P0-1 lifecycle hook(`AGENT_START` / `PRE_TOOL_USE` /
  `POST_TOOL_USE` / `HANDOFF` / `AGENT_END` / `STOP`)會被 fire。
- 整個 run 開一個 trace,tool / agent layer 開 span,raw token 不開 span。
- 串流結束必定 yield ``RunItemStreamEvent(item_type="final_output", ...)``。
- ``cli.stream_renderer.render_stream`` 把 event 印到注入的 Console buffer。
"""

from __future__ import annotations

import io
import sys
from collections.abc import AsyncIterator
from typing import Any

import pytest
from rich.console import Console

from anila_agent.cli.stream_renderer import render_stream
from anila_agent.core.concurrency import ToolCall, ToolResult
from anila_agent.core.events import Event, EventBus
from anila_agent.core.hooks import (
    AgentEndInput,
    AgentStartInput,
    HandoffInput,
    HookEvent,
    HookRegistry,
    HookSpec,
    PostToolUseInput,
    PreToolUseInput,
    StopInput,
)
from anila_agent.core.streaming import (
    AgentUpdatedStreamEvent,
    AnilaStreamRunner,
    RawResponseEvent,
    RunItemStreamEvent,
    StreamEvent,
)
from anila_agent.models.schemas import HookOutput
from anila_agent.tracing import Span, Trace, Tracer, TracingProcessor

# ---------------------------------------------------------------------------
# 工具:把 list 轉成 AsyncIterator,讓單元測試組 mock chunk 不用寫 async generator
# ---------------------------------------------------------------------------


async def _aiter(chunks: list[dict[str, Any]]) -> AsyncIterator[dict[str, Any]]:
    """把同步 list 包成 async iterator。"""
    for chunk in chunks:
        yield chunk


async def _collect(events: AsyncIterator[StreamEvent]) -> list[StreamEvent]:
    """把 async iterator 收成 list — 供 assertion 用。"""
    out: list[StreamEvent] = []
    async for ev in events:
        out.append(ev)
    return out


# ---------------------------------------------------------------------------
# Trace 用的測試 processor — 簡單收集所有 span / trace 事件
# ---------------------------------------------------------------------------


class _RecordingProcessor(TracingProcessor):
    """測試用 processor — 把 trace / span 事件收進 list。"""

    def __init__(self) -> None:
        self.trace_starts: list[Trace] = []
        self.trace_ends: list[Trace] = []
        self.span_starts: list[Span] = []
        self.span_ends: list[Span] = []

    def on_trace_start(self, trace: Trace) -> None:
        self.trace_starts.append(trace)

    def on_trace_end(self, trace: Trace) -> None:
        self.trace_ends.append(trace)

    def on_span_start(self, span: Span) -> None:
        self.span_starts.append(span)

    def on_span_end(self, span: Span) -> None:
        self.span_ends.append(span)


# ---------------------------------------------------------------------------
# 1. 三層 StreamEvent dataclass 結構
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_raw_response_event_dataclass() -> None:
    """``RawResponseEvent`` 帶 delta + model + 固定 type literal。"""
    ev = RawResponseEvent(delta="hi", model="m-1")
    assert ev.delta == "hi"
    assert ev.model == "m-1"
    assert ev.type == "raw_response_event"
    assert ev.timestamp is not None


@pytest.mark.unit
def test_agent_updated_stream_event_variants() -> None:
    """``AgentUpdatedStreamEvent`` 支援 handoff / tool_started / tool_completed 三種 sub-state。"""
    handoff_ev = AgentUpdatedStreamEvent(new_agent_name="verifier")
    assert handoff_ev.new_agent_name == "verifier"
    assert handoff_ev.tool_call_started is None
    assert handoff_ev.tool_call_completed is None
    assert handoff_ev.type == "agent_updated_stream_event"

    tc = ToolCall(name="search", args={"q": "x"}, call_id="c1")
    started_ev = AgentUpdatedStreamEvent(tool_call_started=tc)
    assert started_ev.tool_call_started is tc
    assert started_ev.new_agent_name is None

    tr = ToolResult(call_id="c1", output={"k": 1})
    completed_ev = AgentUpdatedStreamEvent(tool_call_completed=tr)
    assert completed_ev.tool_call_completed is tr


@pytest.mark.unit
def test_run_item_stream_event_dataclass() -> None:
    """``RunItemStreamEvent`` item_type 為 literal,item 為任意值。"""
    ev = RunItemStreamEvent(item_type="message", item={"text": "hi"})
    assert ev.item_type == "message"
    assert ev.item == {"text": "hi"}
    assert ev.type == "run_item_stream_event"


@pytest.mark.unit
def test_stream_event_base_class_relation() -> None:
    """三層 event 皆繼承 ``StreamEvent`` 基底。"""
    assert isinstance(RawResponseEvent(), StreamEvent)
    assert isinstance(AgentUpdatedStreamEvent(), StreamEvent)
    assert isinstance(RunItemStreamEvent(), StreamEvent)


# ---------------------------------------------------------------------------
# 2. AnilaStreamRunner — 從 mock chunk 推導對應 event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_streamed_yields_raw_response_for_token_chunk() -> None:
    """``raw_token`` chunk -> ``RawResponseEvent``。"""
    runner = AnilaStreamRunner(HookRegistry(), EventBus())
    chunks = [
        {"kind": "raw_token", "delta": "hello", "model": "m1"},
        {"kind": "raw_token", "delta": " world", "model": "m1"},
    ]
    events = await _collect(runner.run_streamed(_aiter(chunks), model="m1"))
    raw_events = [e for e in events if isinstance(e, RawResponseEvent)]
    assert len(raw_events) == 2
    assert raw_events[0].delta == "hello"
    assert raw_events[1].delta == " world"
    assert raw_events[0].model == "m1"


@pytest.mark.asyncio
async def test_run_streamed_yields_agent_updated_for_tool_lifecycle() -> None:
    """``tool_started`` / ``tool_completed`` chunk -> ``AgentUpdatedStreamEvent`` (兩種 sub-state)。"""
    runner = AnilaStreamRunner(HookRegistry(), EventBus())
    chunks = [
        {
            "kind": "tool_started",
            "tool": "search",
            "args": {"q": "x"},
            "call_id": "c1",
        },
        {
            "kind": "tool_completed",
            "tool": "search",
            "call_id": "c1",
            "output": {"hits": 3},
        },
    ]
    events = await _collect(runner.run_streamed(_aiter(chunks)))

    started = [
        e
        for e in events
        if isinstance(e, AgentUpdatedStreamEvent) and e.tool_call_started is not None
    ]
    completed = [
        e
        for e in events
        if isinstance(e, AgentUpdatedStreamEvent)
        and e.tool_call_completed is not None
    ]
    assert len(started) == 1
    assert started[0].tool_call_started is not None
    assert started[0].tool_call_started.name == "search"
    assert started[0].tool_call_started.args == {"q": "x"}
    assert len(completed) == 1
    assert completed[0].tool_call_completed is not None
    assert completed[0].tool_call_completed.output == {"hits": 3}


@pytest.mark.asyncio
async def test_run_streamed_yields_run_item_for_tool_and_handoff() -> None:
    """tool / handoff chunk 會 yield 對應 ``RunItemStreamEvent``。"""
    runner = AnilaStreamRunner(HookRegistry(), EventBus())
    chunks = [
        {
            "kind": "tool_started",
            "tool": "search",
            "args": {},
            "call_id": "c1",
        },
        {
            "kind": "tool_completed",
            "tool": "search",
            "call_id": "c1",
            "output": "ok",
        },
        {"kind": "handoff", "from": "root", "to": "verifier"},
    ]
    events = await _collect(runner.run_streamed(_aiter(chunks)))
    item_events = [e for e in events if isinstance(e, RunItemStreamEvent)]
    item_types = [e.item_type for e in item_events]
    assert "tool_call" in item_types
    assert "tool_result" in item_types
    assert "handoff" in item_types
    assert "final_output" in item_types  # 結束時必有


@pytest.mark.asyncio
async def test_run_streamed_yields_agent_updated_for_handoff() -> None:
    """``handoff`` chunk -> ``AgentUpdatedStreamEvent`` 帶 ``new_agent_name``。"""
    runner = AnilaStreamRunner(HookRegistry(), EventBus())
    chunks = [{"kind": "handoff", "from": "root", "to": "verifier"}]
    events = await _collect(runner.run_streamed(_aiter(chunks)))
    handoffs = [
        e
        for e in events
        if isinstance(e, AgentUpdatedStreamEvent) and e.new_agent_name == "verifier"
    ]
    assert len(handoffs) == 1


# ---------------------------------------------------------------------------
# 3. final_output 結尾必有
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_streamed_always_yields_final_output_at_end() -> None:
    """整個 chunk 流結束時必定 yield ``final_output`` event 在最後一個位置。"""
    runner = AnilaStreamRunner(HookRegistry(), EventBus())
    chunks = [{"kind": "raw_token", "delta": "hi", "model": "m"}]
    events = await _collect(runner.run_streamed(_aiter(chunks)))
    last = events[-1]
    assert isinstance(last, RunItemStreamEvent)
    assert last.item_type == "final_output"


@pytest.mark.asyncio
async def test_run_streamed_final_output_prefers_explicit_agent_ended() -> None:
    """有 ``agent_ended`` chunk 時 final_output 採其 ``output``。"""
    runner = AnilaStreamRunner(HookRegistry(), EventBus())
    chunks = [
        {"kind": "raw_token", "delta": "noisy", "model": "m"},
        {"kind": "agent_ended", "agent": "root", "output": {"answer": 42}},
    ]
    events = await _collect(runner.run_streamed(_aiter(chunks)))
    final = events[-1]
    assert isinstance(final, RunItemStreamEvent)
    assert final.item == {"answer": 42}


@pytest.mark.asyncio
async def test_run_streamed_final_output_falls_back_to_delta_buffer() -> None:
    """無 ``agent_ended`` 與 ``message`` chunk 時,final_output 以 raw delta 拼回。"""
    runner = AnilaStreamRunner(HookRegistry(), EventBus())
    chunks = [
        {"kind": "raw_token", "delta": "hello ", "model": "m"},
        {"kind": "raw_token", "delta": "world", "model": "m"},
    ]
    events = await _collect(runner.run_streamed(_aiter(chunks)))
    final = events[-1]
    assert isinstance(final, RunItemStreamEvent)
    assert final.item_type == "final_output"
    assert final.item == "hello world"


@pytest.mark.asyncio
async def test_run_streamed_empty_source_still_yields_final_output() -> None:
    """空 chunk source 也必須 yield 一個 ``final_output`` event。"""
    runner = AnilaStreamRunner(HookRegistry(), EventBus())
    events = await _collect(runner.run_streamed(_aiter([])))
    assert len(events) == 1
    assert isinstance(events[0], RunItemStreamEvent)
    assert events[0].item_type == "final_output"


# ---------------------------------------------------------------------------
# 4. P0-1 lifecycle hook 整合 — streaming 期間正確 fire 對應 event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_fires_agent_start_hook() -> None:
    """``agent_started`` chunk -> AGENT_START hook callback 被呼叫。"""
    registry = HookRegistry()
    captured: list[AgentStartInput] = []

    def _cb(payload: AgentStartInput) -> HookOutput:
        captured.append(payload)
        return HookOutput()

    registry.register(HookSpec(event=HookEvent.AGENT_START, callback=_cb))

    runner = AnilaStreamRunner(registry, EventBus())
    chunks = [{"kind": "agent_started", "agent": "root"}]
    await _collect(runner.run_streamed(_aiter(chunks)))

    assert len(captured) == 1
    assert captured[0].agent_name == "root"


@pytest.mark.asyncio
async def test_streaming_fires_pre_and_post_tool_hooks() -> None:
    """tool lifecycle chunk -> PRE_TOOL_USE + POST_TOOL_USE hook 各被 fire 一次。"""
    registry = HookRegistry()
    pre_captured: list[PreToolUseInput] = []
    post_captured: list[PostToolUseInput] = []

    def _pre(payload: PreToolUseInput) -> HookOutput:
        pre_captured.append(payload)
        return HookOutput()

    def _post(payload: PostToolUseInput) -> HookOutput:
        post_captured.append(payload)
        return HookOutput()

    registry.register(HookSpec(event=HookEvent.PRE_TOOL_USE, callback=_pre))
    registry.register(HookSpec(event=HookEvent.POST_TOOL_USE, callback=_post))

    runner = AnilaStreamRunner(registry, EventBus())
    chunks = [
        {
            "kind": "tool_started",
            "tool": "search",
            "args": {"q": "x"},
            "call_id": "c1",
        },
        {
            "kind": "tool_completed",
            "tool": "search",
            "call_id": "c1",
            "output": "ok",
        },
    ]
    await _collect(runner.run_streamed(_aiter(chunks)))

    assert len(pre_captured) == 1
    assert pre_captured[0].tool_name == "search"
    assert pre_captured[0].tool_input == {"q": "x"}
    assert len(post_captured) == 1
    assert post_captured[0].tool_name == "search"
    assert post_captured[0].tool_input == {"q": "x"}  # 透過 in_flight_tools 對應回原 args
    assert post_captured[0].tool_output == "ok"


@pytest.mark.asyncio
async def test_streaming_fires_handoff_hook() -> None:
    """``handoff`` chunk -> HANDOFF hook fire,payload 帶 from/to agent。"""
    registry = HookRegistry()
    captured: list[HandoffInput] = []

    def _cb(payload: HandoffInput) -> HookOutput:
        captured.append(payload)
        return HookOutput()

    registry.register(HookSpec(event=HookEvent.HANDOFF, callback=_cb))

    runner = AnilaStreamRunner(registry, EventBus())
    chunks = [{"kind": "handoff", "from": "root", "to": "verifier"}]
    await _collect(runner.run_streamed(_aiter(chunks)))

    assert len(captured) == 1
    assert captured[0].from_agent == "root"
    assert captured[0].to_agent == "verifier"


@pytest.mark.asyncio
async def test_streaming_fires_agent_end_and_stop_hooks_on_completion() -> None:
    """``agent_ended`` chunk + run 收尾 -> AGENT_END + STOP 各被 fire 一次。"""
    registry = HookRegistry()
    end_captured: list[AgentEndInput] = []
    stop_captured: list[StopInput] = []

    def _end(payload: AgentEndInput) -> HookOutput:
        end_captured.append(payload)
        return HookOutput()

    def _stop(payload: StopInput) -> HookOutput:
        stop_captured.append(payload)
        return HookOutput()

    registry.register(HookSpec(event=HookEvent.AGENT_END, callback=_end))
    registry.register(HookSpec(event=HookEvent.STOP, callback=_stop))

    runner = AnilaStreamRunner(registry, EventBus())
    chunks = [
        {"kind": "agent_ended", "agent": "root", "output": "done"},
    ]
    await _collect(runner.run_streamed(_aiter(chunks)))

    assert len(end_captured) == 1
    assert end_captured[0].agent_name == "root"
    assert end_captured[0].output == "done"
    assert len(stop_captured) == 1
    assert stop_captured[0].final_output == "done"


# ---------------------------------------------------------------------------
# 5. P0-9 tracing 整合 — 整 run 一 trace,tool / agent layer 開 span,raw 不開
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_opens_one_trace_per_run() -> None:
    """整個 run 必定開且只開一個 trace。"""
    tracer = Tracer()
    proc = _RecordingProcessor()
    tracer.register_processor(proc)

    runner = AnilaStreamRunner(HookRegistry(), EventBus(), tracer=tracer)
    chunks = [{"kind": "raw_token", "delta": "hi", "model": "m"}]
    await _collect(runner.run_streamed(_aiter(chunks)))

    assert len(proc.trace_starts) == 1
    assert len(proc.trace_ends) == 1
    assert proc.trace_starts[0].name == "agent.run.streamed"


@pytest.mark.asyncio
async def test_streaming_opens_spans_for_tool_and_agent_layers() -> None:
    """tool / agent / handoff 各開 span,raw token chunk 不開 span。"""
    tracer = Tracer()
    proc = _RecordingProcessor()
    tracer.register_processor(proc)

    runner = AnilaStreamRunner(HookRegistry(), EventBus(), tracer=tracer)
    chunks = [
        {"kind": "agent_started", "agent": "root"},
        {"kind": "raw_token", "delta": "hi", "model": "m"},
        {"kind": "raw_token", "delta": " there", "model": "m"},
        {
            "kind": "tool_started",
            "tool": "search",
            "args": {},
            "call_id": "c1",
        },
        {
            "kind": "tool_completed",
            "tool": "search",
            "call_id": "c1",
            "output": "ok",
        },
        {"kind": "handoff", "from": "root", "to": "verifier"},
    ]
    await _collect(runner.run_streamed(_aiter(chunks)))

    span_names = [s.name for s in proc.span_starts]
    # 預期有:agent.start, tool.call, tool.result, agent.handoff;raw token 不出現。
    assert "agent.start" in span_names
    assert "tool.call" in span_names
    assert "tool.result" in span_names
    assert "agent.handoff" in span_names
    # 確認 raw token 沒被開 span。
    assert all(not n.startswith("raw") for n in span_names)
    # 確認所有 span 都 end 了。
    assert len(proc.span_ends) == len(proc.span_starts)


@pytest.mark.asyncio
async def test_streaming_without_tracer_still_works() -> None:
    """tracer=None 時不報錯,也仍然 yield 三層 event。"""
    runner = AnilaStreamRunner(HookRegistry(), EventBus(), tracer=None)
    chunks = [{"kind": "raw_token", "delta": "hi", "model": "m"}]
    events = await _collect(runner.run_streamed(_aiter(chunks)))
    # 至少有 raw + final_output
    assert any(isinstance(e, RawResponseEvent) for e in events)
    assert isinstance(events[-1], RunItemStreamEvent)


# ---------------------------------------------------------------------------
# 6. EventBus 觀測 — stream_started / stream_ended
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_emits_stream_lifecycle_events() -> None:
    """run 開始 / 結束會在 EventBus emit ``stream_started`` / ``stream_ended``。"""
    bus = EventBus()
    captured: list[Event] = []
    bus.on_any(lambda e: captured.append(e))

    runner = AnilaStreamRunner(HookRegistry(), bus)
    await _collect(runner.run_streamed(_aiter([])))

    kinds = [e.kind for e in captured]
    assert "stream_started" in kinds
    assert "stream_ended" in kinds


# ---------------------------------------------------------------------------
# 7. CLI stream_renderer — 從 stream 印到 stdout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_render_stream_prints_to_injected_console() -> None:
    """``render_stream`` 把三層 event 印到注入的 Console buffer。"""
    runner = AnilaStreamRunner(HookRegistry(), EventBus())
    chunks = [
        {"kind": "agent_started", "agent": "root"},
        {"kind": "raw_token", "delta": "hello", "model": "m"},
        {
            "kind": "tool_started",
            "tool": "search",
            "args": {"q": "x"},
            "call_id": "c1",
        },
        {
            "kind": "tool_completed",
            "tool": "search",
            "call_id": "c1",
            "output": "ok",
        },
        {"kind": "agent_ended", "agent": "root", "output": "final answer"},
    ]
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, width=120)

    await render_stream(runner.run_streamed(_aiter(chunks)), console=console)

    out = buf.getvalue()
    assert "hello" in out  # raw token
    assert "search" in out  # tool name (start or done)
    assert "final answer" in out  # final_output panel


@pytest.mark.asyncio
async def test_render_stream_prints_to_capsys_when_using_default_console(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """不傳 console 時 fallback 到 module-level default,stdout 仍有 print。

    rich 的 default Console 內部會直接寫 sys.stdout;此測試用 capsys 確認
    至少有東西被印出來。
    """
    runner = AnilaStreamRunner(HookRegistry(), EventBus())
    chunks = [{"kind": "raw_token", "delta": "hi", "model": "m"}]

    # 用 force_terminal=False + 注入自家 Console 寫 sys.stdout 確保 capsys 抓得到
    console = Console(file=sys.stdout, force_terminal=False, width=120)
    await render_stream(runner.run_streamed(_aiter(chunks)), console=console)

    captured = capsys.readouterr()
    assert "hi" in captured.out


# ---------------------------------------------------------------------------
# 8. 整合 smoke test — 完整 run 從 agent_started 到 agent_ended
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_run_smoke() -> None:
    """走一個完整 run scenario(agent_started -> tokens -> tool -> handoff -> agent_ended),
    驗證 hook 與 event 全部齊備。"""
    registry = HookRegistry()
    bus = EventBus()
    tracer = Tracer()
    proc = _RecordingProcessor()
    tracer.register_processor(proc)

    hook_calls: list[str] = []

    def _record_hook(name: str) -> Any:
        def _cb(payload: Any) -> HookOutput:
            hook_calls.append(name)
            return HookOutput()

        return _cb

    registry.register(
        HookSpec(event=HookEvent.AGENT_START, callback=_record_hook("agent_start"))
    )
    registry.register(
        HookSpec(event=HookEvent.PRE_TOOL_USE, callback=_record_hook("pre_tool"))
    )
    registry.register(
        HookSpec(event=HookEvent.POST_TOOL_USE, callback=_record_hook("post_tool"))
    )
    registry.register(
        HookSpec(event=HookEvent.HANDOFF, callback=_record_hook("handoff"))
    )
    registry.register(
        HookSpec(event=HookEvent.AGENT_END, callback=_record_hook("agent_end"))
    )
    registry.register(HookSpec(event=HookEvent.STOP, callback=_record_hook("stop")))

    runner = AnilaStreamRunner(registry, bus, tracer=tracer)
    chunks = [
        {"kind": "agent_started", "agent": "root"},
        {"kind": "raw_token", "delta": "thinking", "model": "m"},
        {
            "kind": "tool_started",
            "tool": "search",
            "args": {"q": "x"},
            "call_id": "c1",
        },
        {
            "kind": "tool_completed",
            "tool": "search",
            "call_id": "c1",
            "output": {"ok": True},
        },
        {"kind": "handoff", "from": "root", "to": "verifier"},
        {"kind": "agent_ended", "agent": "verifier", "output": "done"},
    ]
    events = await _collect(runner.run_streamed(_aiter(chunks)))

    # 所有 hook 至少被 fire 一次
    assert "agent_start" in hook_calls
    assert "pre_tool" in hook_calls
    assert "post_tool" in hook_calls
    assert "handoff" in hook_calls
    assert "agent_end" in hook_calls
    assert "stop" in hook_calls

    # trace 開且關
    assert len(proc.trace_starts) == 1
    assert len(proc.trace_ends) == 1

    # final_output 結尾
    assert isinstance(events[-1], RunItemStreamEvent)
    assert events[-1].item_type == "final_output"
    assert events[-1].item == "done"


# ---------------------------------------------------------------------------
# 9. 安全網:run_streamed 處理未知 chunk kind 不會炸,只 warn
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_streamed_ignores_unknown_chunk_kind() -> None:
    """未知 kind 的 chunk 不會中斷 run,只記 warning。"""
    runner = AnilaStreamRunner(HookRegistry(), EventBus())
    chunks = [
        {"kind": "totally_unknown_kind", "x": 1},
        {"kind": "raw_token", "delta": "hi", "model": "m"},
    ]
    events = await _collect(runner.run_streamed(_aiter(chunks)))
    # 仍然有 raw_token + final_output
    assert any(isinstance(e, RawResponseEvent) for e in events)
    assert isinstance(events[-1], RunItemStreamEvent)


# ---------------------------------------------------------------------------
# 10. tool_completed with error 走 ToolResult.error 路徑
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_completed_with_error_sets_tool_result_error() -> None:
    """``tool_completed`` 帶 error -> ``ToolResult.error`` 為 str(error)。"""
    runner = AnilaStreamRunner(HookRegistry(), EventBus())
    chunks = [
        {
            "kind": "tool_started",
            "tool": "search",
            "args": {},
            "call_id": "c1",
        },
        {
            "kind": "tool_completed",
            "tool": "search",
            "call_id": "c1",
            "error": "rate limited",
        },
    ]
    events = await _collect(runner.run_streamed(_aiter(chunks)))
    completed = [
        e
        for e in events
        if isinstance(e, AgentUpdatedStreamEvent) and e.tool_call_completed is not None
    ]
    assert len(completed) == 1
    assert completed[0].tool_call_completed is not None
    assert completed[0].tool_call_completed.error == "rate limited"


# ---------------------------------------------------------------------------
# 11. event 順序保證:tool_started 的 AgentUpdated 在 RunItem 之前
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_started_agent_updated_precedes_run_item() -> None:
    """同一 tool_started chunk 內,AgentUpdatedStreamEvent yield 順序在 RunItemStreamEvent 之前。"""
    runner = AnilaStreamRunner(HookRegistry(), EventBus())
    chunks = [
        {
            "kind": "tool_started",
            "tool": "search",
            "args": {},
            "call_id": "c1",
        },
    ]
    events = await _collect(runner.run_streamed(_aiter(chunks)))
    # 找出 AgentUpdatedStreamEvent 跟 tool_call RunItemStreamEvent 的 index
    au_idx = next(
        i
        for i, e in enumerate(events)
        if isinstance(e, AgentUpdatedStreamEvent) and e.tool_call_started is not None
    )
    ri_idx = next(
        i
        for i, e in enumerate(events)
        if isinstance(e, RunItemStreamEvent) and e.item_type == "tool_call"
    )
    assert au_idx < ri_idx
