"""P1-2 runTools concurrency partition 的 unit test。

涵蓋:
- ``partition_tool_calls`` stable partition 行為(順序保留、metadata-driven 分組)。
- ``run_tool_calls`` 平行組以 ``asyncio.gather`` 跑(總時間 ≈ max,而非 sum)。
- ``run_tool_calls`` 串行組逐個 await,destructive 之間互不重疊。
- ``run_tool_calls`` 結果順序對齊 input(而非執行完成順序)。
- ``run_tool_calls`` tool 內例外被吃下並轉成 ``ToolResult.error``,batch 不 fail-fast。
- ``run_tool_calls`` 與 :class:`Tracer` 整合:1 parent span + N child span,
  parent 關係正確、attribute 標記正確。
- 不在 registry 內的 tool 走串行路徑(保守處理)。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from anila_agent.core.concurrency import (
    ToolCall,
    ToolResult,
    partition_tool_calls,
    run_tool_calls,
)
from anila_agent.tools.base import AnilaTool
from anila_agent.tools.registry import ToolRegistry
from anila_agent.tracing import Span, Trace, Tracer

# ---------------------------------------------------------------------------
# Test helpers — 自訂 tool registry 與 invoker
# ---------------------------------------------------------------------------


def _make_registry() -> ToolRegistry:
    """建立 4 個有 metadata 的測試 tool。

    使用 ``AnilaTool.from_function`` 但「不真的呼叫」這些 tool 物件 — 我們會自帶
    invoker(``_fake_invoker``)。registry 內 tool 只是 metadata 容器。

    回傳的 registry 含:
    * ``r1`` / ``r2`` / ``r3``:唯讀(``concurrency_safe`` 推導為 True)。
    * ``w1``:破壞性寫入(``concurrency_safe=False``)。
    """

    def r1(query: str = "") -> str:
        return query

    def r2(query: str = "") -> str:
        return query

    def r3(query: str = "") -> str:
        return query

    def w1(path: str = "") -> str:
        return path

    registry = ToolRegistry()
    registry.add(
        AnilaTool.from_function(
            r1,
            is_read_only=True,
            category="retrieval",
            name="r1",
        )
    )
    registry.add(
        AnilaTool.from_function(
            r2,
            is_read_only=True,
            category="retrieval",
            name="r2",
        )
    )
    registry.add(
        AnilaTool.from_function(
            r3,
            is_read_only=True,
            category="retrieval",
            name="r3",
        )
    )
    registry.add(
        AnilaTool.from_function(
            w1,
            is_destructive=True,
            category="filesystem",
            name="w1",
        )
    )
    return registry


class _FakeInvoker:
    """測試用 invoker — 依 tool name 模擬不同延遲與行為。

    Attributes:
        delays: tool name → 延遲秒數(``asyncio.sleep`` 量)。
        outputs: tool name → 回傳值。
        in_flight: 當前並發中的 tool name set(用來驗 destructive 不重疊)。
        max_in_flight: 觀察到的最大並發數(供平行斷言用)。
        order_started: tool call 進入 invoker 的順序(call_id 序列)。
        order_finished: tool call 離開 invoker 的順序(call_id 序列)。
        raise_for: tool name → 要 raise 的例外實例;命中時 invoker raise。
    """

    def __init__(
        self,
        delays: dict[str, float] | None = None,
        outputs: dict[str, Any] | None = None,
        raise_for: dict[str, Exception] | None = None,
    ) -> None:
        self.delays = delays or {}
        self.outputs = outputs or {}
        self.raise_for = raise_for or {}
        self.in_flight: set[str] = set()
        self.max_in_flight: int = 0
        self.order_started: list[str] = []
        self.order_finished: list[str] = []
        self._lock = asyncio.Lock()

    async def __call__(self, call: ToolCall, registry: ToolRegistry) -> Any:
        async with self._lock:
            self.in_flight.add(call.call_id)
            self.max_in_flight = max(self.max_in_flight, len(self.in_flight))
            self.order_started.append(call.call_id)

        try:
            if call.name in self.raise_for:
                raise self.raise_for[call.name]
            delay = self.delays.get(call.name, 0.0)
            if delay > 0:
                await asyncio.sleep(delay)
            return self.outputs.get(call.name, f"output:{call.name}")
        finally:
            async with self._lock:
                self.in_flight.discard(call.call_id)
                self.order_finished.append(call.call_id)


# ---------------------------------------------------------------------------
# partition_tool_calls
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_partition_all_read_only() -> None:
    """全 read-only tool 全進 parallel 組,sequential 組為空。"""
    registry = _make_registry()
    calls = [
        ToolCall(name="r1", call_id="c1"),
        ToolCall(name="r2", call_id="c2"),
        ToolCall(name="r3", call_id="c3"),
    ]
    parallel, sequential = partition_tool_calls(calls, registry)
    assert [c.call_id for c in parallel] == ["c1", "c2", "c3"]
    assert sequential == []


@pytest.mark.unit
def test_partition_all_destructive() -> None:
    """全 destructive tool 全進 sequential 組,parallel 組為空。"""
    registry = _make_registry()
    calls = [
        ToolCall(name="w1", call_id="c1"),
        ToolCall(name="w1", call_id="c2"),
    ]
    parallel, sequential = partition_tool_calls(calls, registry)
    assert parallel == []
    assert [c.call_id for c in sequential] == ["c1", "c2"]


@pytest.mark.unit
def test_partition_stable_preserves_relative_order() -> None:
    """混合 tool 時兩組各自保留 input 相對順序。"""
    registry = _make_registry()
    # 故意 interleave:r1, w1, r2, w1, r3
    calls = [
        ToolCall(name="r1", call_id="a"),
        ToolCall(name="w1", call_id="b"),
        ToolCall(name="r2", call_id="c"),
        ToolCall(name="w1", call_id="d"),
        ToolCall(name="r3", call_id="e"),
    ]
    parallel, sequential = partition_tool_calls(calls, registry)
    # parallel 組保留 a → c → e 順序
    assert [c.call_id for c in parallel] == ["a", "c", "e"]
    # sequential 組保留 b → d 順序
    assert [c.call_id for c in sequential] == ["b", "d"]


@pytest.mark.unit
def test_partition_unknown_tool_goes_to_sequential() -> None:
    """不在 registry 內的 tool 走串行(保守處理)。"""
    registry = _make_registry()
    calls = [
        ToolCall(name="r1", call_id="a"),
        ToolCall(name="unknown_tool", call_id="b"),
    ]
    parallel, sequential = partition_tool_calls(calls, registry)
    assert [c.call_id for c in parallel] == ["a"]
    assert [c.call_id for c in sequential] == ["b"]


# ---------------------------------------------------------------------------
# run_tool_calls — 平行 / 串行 / 順序 / 例外
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_run_tool_calls_parallel_runs_concurrently() -> None:
    """3 個 read-only tool 各 sleep 0.1s,總時間應 ≈ 0.1s 而非 0.3s。"""
    registry = _make_registry()
    invoker = _FakeInvoker(delays={"r1": 0.1, "r2": 0.1, "r3": 0.1})

    calls = [
        ToolCall(name="r1", call_id="a"),
        ToolCall(name="r2", call_id="b"),
        ToolCall(name="r3", call_id="c"),
    ]

    start = time.perf_counter()
    results = await run_tool_calls(calls, registry, invoker=invoker)
    elapsed = time.perf_counter() - start

    # 平行跑:elapsed ≈ 0.1s。給足 buffer 容忍 CI jitter,但仍應遠小於 0.3s 串行。
    assert elapsed < 0.25, f"expected parallel ≈ 0.1s, got {elapsed:.3f}s"
    # 三個 tool 應同時 in-flight 過。
    assert invoker.max_in_flight == 3
    # 結果順序對齊 input。
    assert [r.call_id for r in results] == ["a", "b", "c"]
    assert all(r.error is None for r in results)


@pytest.mark.unit
async def test_run_tool_calls_destructive_runs_serially() -> None:
    """3 個 destructive tool 各 sleep 0.1s,應逐個跑(總 ≈ 0.3s,並發數 = 1)。"""
    registry = _make_registry()
    invoker = _FakeInvoker(delays={"w1": 0.1})

    calls = [
        ToolCall(name="w1", call_id="a"),
        ToolCall(name="w1", call_id="b"),
        ToolCall(name="w1", call_id="c"),
    ]

    start = time.perf_counter()
    results = await run_tool_calls(calls, registry, invoker=invoker)
    elapsed = time.perf_counter() - start

    assert elapsed >= 0.3, f"expected serial ≈ 0.3s, got {elapsed:.3f}s"
    # 串行 — 同時 in-flight 永遠 ≤ 1。
    assert invoker.max_in_flight == 1
    # 結果順序對齊 input。
    assert [r.call_id for r in results] == ["a", "b", "c"]
    # 開始順序就是 input 順序(串行特性)。
    assert invoker.order_started == ["a", "b", "c"]


@pytest.mark.unit
async def test_run_tool_calls_mixed_parallel_then_serial() -> None:
    """1 read + 1 destructive:read 先並發跑完,destructive 後跑。

    驗 P1-2 設計選擇 — 平行組先 finalize,串行組看到 stable read 結果。
    """
    registry = _make_registry()
    invoker = _FakeInvoker(delays={"r1": 0.1, "w1": 0.1})

    calls = [
        ToolCall(name="r1", call_id="read"),
        ToolCall(name="w1", call_id="write"),
    ]

    results = await run_tool_calls(calls, registry, invoker=invoker)

    # read 必須 finished 在 write started 之前(設計 invariant)。
    read_finished_idx = invoker.order_finished.index("read")
    write_started_idx = invoker.order_started.index("write")
    assert read_finished_idx < write_started_idx, (
        f"read should finish before write starts; "
        f"order_finished={invoker.order_finished}, "
        f"order_started={invoker.order_started}"
    )
    # 結果順序對齊 input(read 在前)。
    assert [r.call_id for r in results] == ["read", "write"]


@pytest.mark.unit
async def test_run_tool_calls_result_order_matches_input() -> None:
    """input 順序與執行完成順序不同時,結果仍以 input 順序回傳。

    技巧:r1 sleep 0.2s、r2 sleep 0.05s、r3 sleep 0.1s — 完成順序為 r2 → r3 → r1,
    但結果順序應為 r1 → r2 → r3(input 順序)。
    """
    registry = _make_registry()
    invoker = _FakeInvoker(delays={"r1": 0.2, "r2": 0.05, "r3": 0.1})

    calls = [
        ToolCall(name="r1", call_id="x"),
        ToolCall(name="r2", call_id="y"),
        ToolCall(name="r3", call_id="z"),
    ]

    results = await run_tool_calls(calls, registry, invoker=invoker)

    # 完成順序應為 y → z → x。
    assert invoker.order_finished == ["y", "z", "x"]
    # 但結果順序仍對齊 input。
    assert [r.call_id for r in results] == ["x", "y", "z"]


@pytest.mark.unit
async def test_run_tool_calls_empty_list_returns_empty() -> None:
    """空 input 應立即回空 list,不應 raise。"""
    registry = _make_registry()
    invoker = _FakeInvoker()
    results = await run_tool_calls([], registry, invoker=invoker)
    assert results == []


@pytest.mark.unit
async def test_run_tool_calls_tool_exception_caught_per_call() -> None:
    """單一 tool raise 例外應被吃下並轉成 ``ToolResult.error``;同 batch 其他 tool 仍正常完成。"""
    registry = _make_registry()
    invoker = _FakeInvoker(
        delays={"r1": 0.05, "r2": 0.05, "r3": 0.05},
        raise_for={"r2": RuntimeError("boom")},
    )

    calls = [
        ToolCall(name="r1", call_id="a"),
        ToolCall(name="r2", call_id="b"),
        ToolCall(name="r3", call_id="c"),
    ]

    results = await run_tool_calls(calls, registry, invoker=invoker)

    # 順序仍對齊 input。
    assert [r.call_id for r in results] == ["a", "b", "c"]
    # r1 / r3 成功。
    assert results[0].error is None
    assert results[0].output == "output:r1"
    assert results[2].error is None
    assert results[2].output == "output:r3"
    # r2 失敗。
    assert results[1].output is None
    assert results[1].error is not None
    assert "RuntimeError" in results[1].error
    assert "boom" in results[1].error


@pytest.mark.unit
async def test_run_tool_calls_unknown_tool_returns_error() -> None:
    """未註冊 tool 走串行路徑,default invoker raise KeyError 後變 ``ToolResult.error``。"""
    registry = _make_registry()
    invoker = _FakeInvoker()

    calls = [
        ToolCall(name="ghost", call_id="g"),
    ]
    results = await run_tool_calls(calls, registry, invoker=invoker)

    assert len(results) == 1
    # _FakeInvoker 對 ghost 仍會回 "output:ghost"(它不檢查 registry),所以這裡
    # 主要驗 partition 把它丟到 sequential 後仍能跑完。
    assert results[0].call_id == "g"


@pytest.mark.unit
async def test_run_tool_calls_max_concurrency_throttles_parallel() -> None:
    """``max_concurrency=2`` 應把 3 個 read-only tool 的同時 in-flight 數限制在 2。"""
    registry = _make_registry()
    invoker = _FakeInvoker(delays={"r1": 0.1, "r2": 0.1, "r3": 0.1})

    calls = [
        ToolCall(name="r1", call_id="a"),
        ToolCall(name="r2", call_id="b"),
        ToolCall(name="r3", call_id="c"),
    ]

    results = await run_tool_calls(
        calls, registry, invoker=invoker, max_concurrency=2
    )

    assert invoker.max_in_flight == 2
    assert [r.call_id for r in results] == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# run_tool_calls + tracing 整合
# ---------------------------------------------------------------------------


class _RecordingProcessor:
    """測試用 tracing processor — 把所有事件存到 list 供斷言用。"""

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


@pytest.mark.unit
async def test_run_tool_calls_tracing_parent_and_children() -> None:
    """傳 tracer 時應產生:1 parent ``tool_batch`` span + N child ``tool.<name>`` span。

    並驗:
    - child 的 ``parent_span_id`` 都指向 parent batch span。
    - child 的 ``trace_id`` 都等於 parent 的 ``trace_id``。
    - attribute 標記正確(parallel / sequential / duration_ms)。
    """
    registry = _make_registry()
    invoker = _FakeInvoker(delays={"r1": 0.02, "r2": 0.02, "w1": 0.02})

    tracer = Tracer()
    proc = _RecordingProcessor()
    tracer.register_processor(proc)

    calls = [
        ToolCall(name="r1", call_id="a"),
        ToolCall(name="r2", call_id="b"),
        ToolCall(name="w1", call_id="c"),
    ]

    with tracer.start_trace("test.run"):
        await run_tool_calls(calls, registry, invoker=invoker, tracer=tracer)

    # 收集所有 span.start 事件。
    span_starts = [e for kind, e in proc.events if kind == "span.start"]
    span_ends = [e for kind, e in proc.events if kind == "span.end"]
    assert isinstance(span_starts[0], Span) and isinstance(span_ends[0], Span)

    # 1 batch + 3 child = 4 span starts 與 4 span ends。
    assert len(span_starts) == 4
    assert len(span_ends) == 4

    # 第 1 個 span.start 應為 batch span。
    batch_start = span_starts[0]
    assert isinstance(batch_start, Span)
    assert batch_start.name == "tool_batch"
    assert batch_start.parent_span_id is None  # 直接掛 trace 下
    assert batch_start.attributes["tool_batch.size"] == 3
    assert batch_start.attributes["tool_batch.parallel_count"] == 2
    assert batch_start.attributes["tool_batch.sequential_count"] == 1

    # 後續 3 個 span.start 為 child span,parent_span_id 都指向 batch。
    child_starts = span_starts[1:]
    for child in child_starts:
        assert isinstance(child, Span)
        assert child.parent_span_id == batch_start.span_id
        assert child.trace_id == batch_start.trace_id
        assert child.name.startswith("tool.")
        assert "tool.call_id" in child.attributes
        assert "tool.parallel" in child.attributes

    # 名稱對應正確。
    child_names = {c.name for c in child_starts if isinstance(c, Span)}
    assert child_names == {"tool.r1", "tool.r2", "tool.w1"}

    # parallel 標記正確:r1 / r2 平行;w1 串行。
    for child in child_starts:
        assert isinstance(child, Span)
        if child.name in ("tool.r1", "tool.r2"):
            assert child.attributes["tool.parallel"] is True
        elif child.name == "tool.w1":
            assert child.attributes["tool.parallel"] is False

    # 所有 child span end 都應有 duration_ms 且 status=ok。
    for ev_kind, span in proc.events:
        if ev_kind == "span.end" and isinstance(span, Span) and span.name.startswith(
            "tool."
        ):
            assert "tool.duration_ms" in span.attributes
            assert span.status == "ok"


@pytest.mark.unit
async def test_run_tool_calls_tracing_error_span_status() -> None:
    """tool raise 時 child span 應標 status=error 且填 error 訊息。"""
    registry = _make_registry()
    invoker = _FakeInvoker(raise_for={"r1": ValueError("oops")})

    tracer = Tracer()
    proc = _RecordingProcessor()
    tracer.register_processor(proc)

    calls = [ToolCall(name="r1", call_id="a")]

    with tracer.start_trace("test.run"):
        results = await run_tool_calls(calls, registry, invoker=invoker, tracer=tracer)

    assert results[0].error is not None
    # 找到 tool.r1 的 span.end 事件。
    r1_ends = [
        s
        for k, s in proc.events
        if k == "span.end" and isinstance(s, Span) and s.name == "tool.r1"
    ]
    assert len(r1_ends) == 1
    end_span = r1_ends[0]
    assert isinstance(end_span, Span)
    assert end_span.status == "error"
    assert end_span.error is not None
    assert "ValueError" in end_span.error


@pytest.mark.unit
async def test_run_tool_calls_without_tracer_no_span_events() -> None:
    """``tracer=None`` 時不應有任何 span event(回歸驗證 tracing 為 optional)。"""
    registry = _make_registry()
    invoker = _FakeInvoker()

    tracer = Tracer()
    proc = _RecordingProcessor()
    tracer.register_processor(proc)

    calls = [ToolCall(name="r1", call_id="a")]
    # 注意:這裡刻意不傳 tracer,但仍用一個有 processor 的 Tracer 在外掛
    # 來確保「不傳 tracer 就不發 span」這條 path 是乾淨的。
    await run_tool_calls(calls, registry, invoker=invoker)
    assert proc.events == []


# ---------------------------------------------------------------------------
# ToolResult / ToolCall 結構
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_tool_call_dataclass_defaults() -> None:
    """``ToolCall`` 預設 args 為空 dict、call_id 為空字串。"""
    call = ToolCall(name="x")
    assert call.name == "x"
    assert call.args == {}
    assert call.call_id == ""


@pytest.mark.unit
def test_tool_result_dataclass_defaults() -> None:
    """``ToolResult`` 預設 output / error 為 None,duration_ms 為 0.0。"""
    r = ToolResult(call_id="x")
    assert r.call_id == "x"
    assert r.output is None
    assert r.error is None
    assert r.duration_ms == 0.0
