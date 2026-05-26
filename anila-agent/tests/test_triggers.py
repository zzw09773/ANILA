"""P1-4 Triggers 子系統 unit tests。

涵蓋:

* PeriodicTrigger 0.1 秒 interval fire 5 次後 stop。
* FileChangeTrigger 用 ``tmp_path`` 寫檔測 mtime + debounce 行為。
* DbChangeTrigger mock query_func,result 變 / 不變 fire 情況。
* TriggerManager start_all / stop_all lifecycle。
* callback 例外不會 break manager,其他 trigger 照常跑。
* tracing span (P0-9) 正確開閉 (用 capture processor 驗)。
* ctx.session.queue_message 注入後 SessionContext.message_queue 取得到。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from anila_agent.core.hook_context import HookContext, SessionContext
from anila_agent.tracing import Span, Tracer, TracingProcessor
from anila_agent.triggers import (
    DbChangeTrigger,
    FileChangeTrigger,
    PeriodicTrigger,
    Trigger,
    TriggerManager,
)

# ---------------------------------------------------------------------------
# 共用 fixture / helper
# ---------------------------------------------------------------------------


class _SpanCapture:
    """capture-style TracingProcessor — 收下所有 span,給測試 assert 用。"""

    def __init__(self) -> None:
        self.spans_started: list[Span] = []
        self.spans_ended: list[Span] = []

    def on_trace_start(self, trace: Any) -> None:
        return

    def on_trace_end(self, trace: Any) -> None:
        return

    def on_span_start(self, span: Span) -> None:
        self.spans_started.append(span)

    def on_span_end(self, span: Span) -> None:
        self.spans_ended.append(span)


@pytest.fixture
def session() -> SessionContext:
    """新鮮 SessionContext (含空 message_queue)。"""
    return SessionContext(session_id="test-session")


# ---------------------------------------------------------------------------
# SessionContext.queue_message / drain_messages
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_queue_message_then_drain(session: SessionContext) -> None:
    """queue_message 注入 -> drain_messages 拉得回來 (FIFO)。"""
    session.queue_message("first", role="system")
    session.queue_message("second", role="user", metadata={"src": "test"})

    msgs = session.drain_messages()
    assert len(msgs) == 2
    assert msgs[0] == {"role": "system", "content": "first", "metadata": {}}
    assert msgs[1] == {
        "role": "user",
        "content": "second",
        "metadata": {"src": "test"},
    }
    # 再 drain 一次應該回空 list (已拉光)。
    assert session.drain_messages() == []


@pytest.mark.unit
async def test_message_queue_independent_per_session() -> None:
    """每個 SessionContext 各自一個 queue,不共用。"""
    s1 = SessionContext(session_id="s1")
    s2 = SessionContext(session_id="s2")
    s1.queue_message("only-s1")
    assert s2.drain_messages() == []
    assert len(s1.drain_messages()) == 1


# ---------------------------------------------------------------------------
# PeriodicTrigger
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_periodic_trigger_rejects_non_positive_interval() -> None:
    """interval <= 0 應丟 ValueError。"""
    with pytest.raises(ValueError, match="positive"):
        PeriodicTrigger(0, lambda ctx: None)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="positive"):
        PeriodicTrigger(-1.5, lambda ctx: None)  # type: ignore[arg-type]


@pytest.mark.unit
async def test_periodic_trigger_fires_n_times_then_stops(
    session: SessionContext,
) -> None:
    """0.1s interval 期望短時間內收到 >= 5 次 fire,stop 後 task 收乾淨。"""
    fire_count = 0
    stop_event = asyncio.Event()

    async def cb(ctx: HookContext) -> None:
        nonlocal fire_count
        fire_count += 1
        if fire_count >= 5:
            stop_event.set()

    manager = TriggerManager(session=session)
    manager.register(PeriodicTrigger(0.05, cb))
    await manager.start_all()
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=2.0)
    finally:
        await manager.stop_all()

    assert fire_count >= 5
    assert not manager.is_running


# ---------------------------------------------------------------------------
# FileChangeTrigger
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_file_change_trigger_validates_arguments(tmp_path: Path) -> None:
    """空 paths / 非法 interval / 負 debounce 應丟 ValueError。"""

    async def cb(ctx: HookContext, paths: Any) -> None:
        return

    with pytest.raises(ValueError, match="at least one"):
        FileChangeTrigger([], cb)
    with pytest.raises(ValueError, match="poll_interval_seconds"):
        FileChangeTrigger([tmp_path / "x"], cb, poll_interval_seconds=0)
    with pytest.raises(ValueError, match="debounce_seconds"):
        FileChangeTrigger([tmp_path / "x"], cb, debounce_seconds=-1)


@pytest.mark.unit
async def test_file_change_trigger_detects_write(
    tmp_path: Path, session: SessionContext
) -> None:
    """改檔後 trigger 應 fire 一次,把 changed paths 帶給 callback。"""
    watch_file = tmp_path / "watch.txt"
    watch_file.write_text("v1")

    fired_paths: list[Path] = []
    fire_event = asyncio.Event()

    async def cb(ctx: HookContext, changed: Any) -> None:
        fired_paths.extend(changed)
        fire_event.set()

    trig = FileChangeTrigger(
        [watch_file],
        cb,
        poll_interval_seconds=0.05,
        debounce_seconds=0.1,
    )
    manager = TriggerManager(session=session)
    manager.register(trig)
    await manager.start_all()
    try:
        # 等 trigger run() 抓 baseline
        await asyncio.sleep(0.15)
        watch_file.write_text("v2")
        await asyncio.wait_for(fire_event.wait(), timeout=2.0)
    finally:
        await manager.stop_all()

    assert watch_file in fired_paths


@pytest.mark.unit
async def test_file_change_trigger_debounce_batches_writes(
    tmp_path: Path, session: SessionContext
) -> None:
    """debounce 期間內多次寫入應只 fire 一次 callback。"""
    watch_file = tmp_path / "watch.txt"
    watch_file.write_text("v1")

    fire_count = 0
    fire_event = asyncio.Event()

    async def cb(ctx: HookContext, changed: Any) -> None:
        nonlocal fire_count
        fire_count += 1
        fire_event.set()

    trig = FileChangeTrigger(
        [watch_file],
        cb,
        poll_interval_seconds=0.05,
        debounce_seconds=0.3,
    )
    manager = TriggerManager(session=session)
    manager.register(trig)
    await manager.start_all()
    try:
        await asyncio.sleep(0.1)  # baseline
        # debounce 視窗內連寫 3 次
        watch_file.write_text("v2")
        await asyncio.sleep(0.05)
        watch_file.write_text("v3")
        await asyncio.sleep(0.05)
        watch_file.write_text("v4")
        await asyncio.wait_for(fire_event.wait(), timeout=2.0)
        # 再多等一陣子,確認 debounce 結束後沒有再次 fire
        await asyncio.sleep(0.5)
    finally:
        await manager.stop_all()

    assert fire_count == 1


@pytest.mark.unit
async def test_file_change_trigger_handles_missing_file(
    tmp_path: Path, session: SessionContext
) -> None:
    """初始不存在的檔案,後來被建立應視為 change。"""
    target = tmp_path / "later.txt"
    fire_event = asyncio.Event()

    async def cb(ctx: HookContext, changed: Any) -> None:
        fire_event.set()

    trig = FileChangeTrigger(
        [target],
        cb,
        poll_interval_seconds=0.05,
        debounce_seconds=0.1,
    )
    manager = TriggerManager(session=session)
    manager.register(trig)
    await manager.start_all()
    try:
        await asyncio.sleep(0.1)
        target.write_text("hello")
        await asyncio.wait_for(fire_event.wait(), timeout=2.0)
    finally:
        await manager.stop_all()


# ---------------------------------------------------------------------------
# DbChangeTrigger
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_db_change_trigger_rejects_non_positive_interval() -> None:
    """interval <= 0 應丟 ValueError。"""

    async def q() -> Any:
        return 1

    async def cb(ctx: HookContext, prev: Any, curr: Any) -> None:
        return

    with pytest.raises(ValueError, match="positive"):
        DbChangeTrigger(q, cb, interval_seconds=0)


@pytest.mark.unit
async def test_db_change_trigger_fires_only_on_change(
    session: SessionContext,
) -> None:
    """query result 不變不 fire,變了才 fire 並帶 (prev, curr)。"""
    seq = iter(["a", "a", "a", "b", "b", "c"])
    fire_history: list[tuple[Any, Any]] = []
    fire_event = asyncio.Event()

    async def q() -> Any:
        return next(seq, "c")  # 用完之後一直回 "c"

    async def cb(ctx: HookContext, prev: Any, curr: Any) -> None:
        fire_history.append((prev, curr))
        if len(fire_history) >= 2:
            fire_event.set()

    trig = DbChangeTrigger(q, cb, interval_seconds=0.05)
    manager = TriggerManager(session=session)
    manager.register(trig)
    await manager.start_all()
    try:
        await asyncio.wait_for(fire_event.wait(), timeout=3.0)
    finally:
        await manager.stop_all()

    # 應該至少有兩次 transition (a->b, b->c);第一次 baseline 不算
    assert len(fire_history) >= 2
    assert fire_history[0] == ("a", "b")
    assert fire_history[1] == ("b", "c")


@pytest.mark.unit
async def test_db_change_trigger_fire_on_initial(session: SessionContext) -> None:
    """fire_on_initial=True 時第一個 query 即 fire 一次,previous 為 None。"""
    fire_history: list[tuple[Any, Any]] = []
    fire_event = asyncio.Event()

    async def q() -> Any:
        return "stable"

    async def cb(ctx: HookContext, prev: Any, curr: Any) -> None:
        fire_history.append((prev, curr))
        fire_event.set()

    trig = DbChangeTrigger(q, cb, interval_seconds=0.05, fire_on_initial=True)
    manager = TriggerManager(session=session)
    manager.register(trig)
    await manager.start_all()
    try:
        await asyncio.wait_for(fire_event.wait(), timeout=2.0)
    finally:
        await manager.stop_all()

    assert fire_history[0] == (None, "stable")


@pytest.mark.unit
async def test_db_change_trigger_swallows_query_exception(
    session: SessionContext,
) -> None:
    """query_func 例外應被 catch,不會 break trigger loop。"""
    call_count = 0
    fire_event = asyncio.Event()

    async def q() -> Any:
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            raise RuntimeError("db temporarily unavailable")
        return call_count  # 後續每次都不同 -> 會 fire

    async def cb(ctx: HookContext, prev: Any, curr: Any) -> None:
        fire_event.set()

    trig = DbChangeTrigger(q, cb, interval_seconds=0.05)
    manager = TriggerManager(session=session)
    manager.register(trig)
    await manager.start_all()
    try:
        await asyncio.wait_for(fire_event.wait(), timeout=2.0)
    finally:
        await manager.stop_all()

    # query_func 已被叫超過 2 次 (前兩次丟例外,後續才產生 fire)
    assert call_count >= 3


# ---------------------------------------------------------------------------
# TriggerManager lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_manager_register_after_start_raises(session: SessionContext) -> None:
    """start_all 後再 register 應丟 RuntimeError。"""

    async def cb(ctx: HookContext) -> None:
        return

    manager = TriggerManager(session=session)
    manager.register(PeriodicTrigger(0.1, cb))
    await manager.start_all()
    try:
        with pytest.raises(RuntimeError, match="register triggers"):
            manager.register(PeriodicTrigger(0.1, cb))
    finally:
        await manager.stop_all()


@pytest.mark.unit
async def test_manager_double_start_raises(session: SessionContext) -> None:
    """連續兩次 start_all 應丟 RuntimeError。"""

    async def cb(ctx: HookContext) -> None:
        return

    manager = TriggerManager(session=session)
    manager.register(PeriodicTrigger(0.1, cb))
    await manager.start_all()
    try:
        with pytest.raises(RuntimeError, match="already started"):
            await manager.start_all()
    finally:
        await manager.stop_all()


@pytest.mark.unit
async def test_manager_stop_all_is_idempotent(session: SessionContext) -> None:
    """重複呼叫 stop_all 應安全,不會丟例外。"""
    manager = TriggerManager(session=session)
    await manager.stop_all()  # 沒 start 直接 stop 安全
    await manager.stop_all()
    assert not manager.is_running


@pytest.mark.unit
async def test_manager_runs_multiple_triggers_concurrently(
    session: SessionContext,
) -> None:
    """同時跑兩個 trigger 都會 fire。"""
    counts = {"a": 0, "b": 0}
    both_fired = asyncio.Event()

    async def cb_a(ctx: HookContext) -> None:
        counts["a"] += 1
        if counts["a"] and counts["b"]:
            both_fired.set()

    async def cb_b(ctx: HookContext) -> None:
        counts["b"] += 1
        if counts["a"] and counts["b"]:
            both_fired.set()

    manager = TriggerManager(session=session)
    manager.register(PeriodicTrigger(0.05, cb_a, name="A"))
    manager.register(PeriodicTrigger(0.05, cb_b, name="B"))
    async with manager:
        await asyncio.wait_for(both_fired.wait(), timeout=2.0)

    assert counts["a"] >= 1
    assert counts["b"] >= 1


@pytest.mark.unit
async def test_manager_async_context_manager_starts_and_stops(
    session: SessionContext,
) -> None:
    """``async with manager`` 等於 start_all + stop_all。"""
    counter = 0
    fired = asyncio.Event()

    async def cb(ctx: HookContext) -> None:
        nonlocal counter
        counter += 1
        fired.set()

    manager = TriggerManager(session=session)
    manager.register(PeriodicTrigger(0.05, cb))
    async with manager:
        assert manager.is_running
        await asyncio.wait_for(fired.wait(), timeout=2.0)
    assert not manager.is_running
    assert counter >= 1


# ---------------------------------------------------------------------------
# 例外不影響 manager
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_callback_exception_does_not_break_manager(
    session: SessionContext,
) -> None:
    """壞 callback 丟例外,manager 與其他 trigger 仍正常運作。"""
    bad_fired = 0
    good_fired = 0
    enough = asyncio.Event()

    async def bad_cb(ctx: HookContext) -> None:
        nonlocal bad_fired
        bad_fired += 1
        raise RuntimeError("boom")

    async def good_cb(ctx: HookContext) -> None:
        nonlocal good_fired
        good_fired += 1
        if bad_fired >= 2 and good_fired >= 2:
            enough.set()

    manager = TriggerManager(session=session)
    manager.register(PeriodicTrigger(0.05, bad_cb, name="bad"))
    manager.register(PeriodicTrigger(0.05, good_cb, name="good"))
    async with manager:
        await asyncio.wait_for(enough.wait(), timeout=3.0)

    # 兩個 trigger 都至少 fire 兩次以上 (bad 的例外被吞掉,沒拖死)
    assert bad_fired >= 2
    assert good_fired >= 2


@pytest.mark.unit
async def test_trigger_run_exception_only_stops_itself(
    session: SessionContext,
) -> None:
    """trigger.run 本身丟例外,該 trigger 停止,其他 trigger 不受影響。"""

    class _BrokenTrigger(Trigger):
        callback = None

        @property
        def name(self) -> str:
            return "broken"

        async def run(self, manager: TriggerManager) -> None:
            await asyncio.sleep(0.01)
            raise RuntimeError("loop blew up")

    good_fired = asyncio.Event()

    async def good_cb(ctx: HookContext) -> None:
        good_fired.set()

    manager = TriggerManager(session=session)
    manager.register(_BrokenTrigger())
    manager.register(PeriodicTrigger(0.05, good_cb, name="good"))
    async with manager:
        await asyncio.wait_for(good_fired.wait(), timeout=2.0)


# ---------------------------------------------------------------------------
# Tracing 整合 (P0-9)
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_trigger_fire_opens_tracing_span(session: SessionContext) -> None:
    """每次 fire 應開 `trigger.fire.<name>` span 並設 trigger.name attribute。"""
    fired = asyncio.Event()

    async def cb(ctx: HookContext) -> None:
        fired.set()

    tracer = Tracer()
    capture = _SpanCapture()
    tracer.register_processor(capture)

    manager = TriggerManager(session=session, tracer=tracer)
    manager.register(PeriodicTrigger(0.05, cb, name="health"))
    with tracer.start_trace("agent.session"):
        async with manager:
            await asyncio.wait_for(fired.wait(), timeout=2.0)

    span_names = [s.name for s in capture.spans_started]
    assert "trigger.fire.health" in span_names
    span = next(s for s in capture.spans_ended if s.name == "trigger.fire.health")
    assert span.status == "ok"
    assert span.attributes.get("trigger.name") == "health"


@pytest.mark.unit
async def test_trigger_fire_span_closes_even_on_callback_exception(
    session: SessionContext,
) -> None:
    """callback 丟例外時,fire span 仍要正常關閉 (不漏 span)。

    `_invoke_callback` 把 callback 例外吞掉,因此 span 走 ok path —
    span 結尾要被 fire `on_span_end`,不能 leak。
    """
    seen = 0
    seen_event = asyncio.Event()

    async def cb(ctx: HookContext) -> None:
        nonlocal seen
        seen += 1
        if seen >= 2:
            seen_event.set()
        raise RuntimeError("crash inside callback")

    tracer = Tracer()
    capture = _SpanCapture()
    tracer.register_processor(capture)

    manager = TriggerManager(session=session, tracer=tracer)
    manager.register(PeriodicTrigger(0.05, cb, name="crashy"))
    with tracer.start_trace("agent.session"):
        async with manager:
            await asyncio.wait_for(seen_event.wait(), timeout=2.0)

    # 每次 fire 都應該 start + end 各一次
    starts = [s for s in capture.spans_started if s.name == "trigger.fire.crashy"]
    ends = [s for s in capture.spans_ended if s.name == "trigger.fire.crashy"]
    assert len(starts) >= 2
    assert len(ends) >= 2
    assert len(starts) == len(ends)


# ---------------------------------------------------------------------------
# ctx.session.queue_message 從 trigger 內注入
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_trigger_callback_can_inject_message_via_session(
    session: SessionContext,
) -> None:
    """trigger callback 內呼叫 ``ctx.session.queue_message`` 應出現在 session queue。"""
    fired = asyncio.Event()

    async def cb(ctx: HookContext) -> None:
        ctx.session.queue_message(
            "[trigger] hello from periodic",
            role="system",
            metadata={"source": "periodic"},
        )
        fired.set()

    manager = TriggerManager(session=session)
    manager.register(PeriodicTrigger(0.05, cb, name="injector"))
    async with manager:
        await asyncio.wait_for(fired.wait(), timeout=2.0)

    drained = session.drain_messages()
    assert len(drained) >= 1
    first = drained[0]
    assert first["role"] == "system"
    assert first["content"] == "[trigger] hello from periodic"
    assert first["metadata"] == {"source": "periodic"}


# ---------------------------------------------------------------------------
# TracingProcessor protocol guard
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_span_capture_is_tracing_processor_protocol() -> None:
    """_SpanCapture 應符合 TracingProcessor Protocol (runtime_checkable)。"""
    assert isinstance(_SpanCapture(), TracingProcessor)
