"""P0-9 tracing framework 的 unit test。

涵蓋:
- ``Trace`` / ``Span`` dataclass 預設值與序列化。
- ``Tracer.start_trace`` context manager 自動填 start / end time。
- ``Tracer.start_span`` 正常 / 例外 兩條 path 的 status 設定。
- ``JsonlTracingProcessor`` 寫入 JSONL 檔可被 line-by-line parse,且行數對齊事件數。
- ``ConsoleTracingProcessor`` 寫 stdout(以 capsys 驗證)。
- 多個 processor 同時註冊皆收到事件。
- 巢狀 span 的 ``parent_span_id`` 自動指向外層 span。
- trace 外呼叫 start_span 的 fallback 行為。
"""

from __future__ import annotations

import io
import json
from datetime import datetime
from pathlib import Path

import pytest

from anila_agent.tracing import (
    ConsoleTracingProcessor,
    JsonlTracingProcessor,
    Span,
    Trace,
    Tracer,
    TracingProcessor,
)

# ---------------------------------------------------------------------------
# Trace / Span dataclass 基本結構
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_trace_defaults() -> None:
    """`Trace` 預設值應自帶 UUID trace_id 與 utc now start_time。"""
    trace = Trace(name="demo")
    assert isinstance(trace.trace_id, str) and len(trace.trace_id) > 0
    assert trace.name == "demo"
    assert isinstance(trace.start_time, datetime)
    assert trace.end_time is None
    assert trace.metadata == {}


@pytest.mark.unit
def test_span_defaults() -> None:
    """`Span` 預設值:狀態為 in_progress、無 error、無 parent。"""
    span = Span(name="op")
    assert isinstance(span.span_id, str) and len(span.span_id) > 0
    assert span.parent_span_id is None
    assert span.status == "in_progress"
    assert span.error is None
    assert isinstance(span.start_time, datetime)
    assert span.end_time is None


@pytest.mark.unit
def test_trace_to_dict_isoformat() -> None:
    """trace 序列化:時間欄位應為 ISO 8601 字串。"""
    trace = Trace(name="x", metadata={"k": "v"})
    payload = trace.to_dict()
    assert payload["object"] == "trace"
    assert payload["name"] == "x"
    assert payload["end_time"] is None
    # start_time 為 ISO 字串,可被 fromisoformat 解回 datetime
    datetime.fromisoformat(payload["start_time"])


# ---------------------------------------------------------------------------
# Tracer.start_trace context manager
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_start_trace_sets_start_and_end_time() -> None:
    """進入 / 離開 trace context 應自動帶 start_time 與 end_time。"""
    tracer = Tracer()
    with tracer.start_trace("workflow") as trace:
        assert trace.start_time is not None
        assert trace.end_time is None
        assert tracer.current_trace is trace
    assert trace.end_time is not None
    assert trace.end_time >= trace.start_time
    assert tracer.current_trace is None  # 離開後清掉


# ---------------------------------------------------------------------------
# Tracer.start_span status 設定
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_span_ok_on_normal_exit() -> None:
    """正常離開 span context,status 應為 'ok'。"""
    tracer = Tracer()
    with tracer.start_trace("w"), tracer.start_span("step") as span:
        pass
    assert span.status == "ok"
    assert span.error is None
    assert span.end_time is not None


@pytest.mark.unit
def test_span_error_on_exception() -> None:
    """在 span context 內 raise 例外,status 應為 'error' 且記錄 error 訊息。"""
    tracer = Tracer()
    captured: Span | None = None
    with (
        pytest.raises(ValueError, match="boom"),
        tracer.start_trace("w"),
        tracer.start_span("step") as span,
    ):
        captured = span
        raise ValueError("boom")
    assert captured is not None
    assert captured.status == "error"
    assert captured.error is not None
    assert "ValueError" in captured.error
    assert "boom" in captured.error
    assert captured.end_time is not None


# ---------------------------------------------------------------------------
# JsonlTracingProcessor
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_jsonl_processor_writes_parseable_events(tmp_path: Path) -> None:
    """JsonlTracingProcessor 寫的每一行應為合法 JSON,行數對應事件數。"""
    jsonl_path = tmp_path / "trace.jsonl"
    proc = JsonlTracingProcessor(jsonl_path)
    tracer = Tracer()
    tracer.register_processor(proc)

    # 1 trace + 3 span -> 共 2 + 6 = 8 行
    with tracer.start_trace("agent.run"):
        with tracer.start_span("tool.call.read"):
            pass
        with tracer.start_span("tool.call.search"):
            pass
        with tracer.start_span("llm.completion"):
            pass

    proc.close()

    lines = jsonl_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 8  # trace.start + 3 * (span.start + span.end) + trace.end

    events = [json.loads(line) for line in lines]
    assert events[0]["event"] == "trace.start"
    assert events[-1]["event"] == "trace.end"
    span_events = [e for e in events if e["event"].startswith("span.")]
    assert len(span_events) == 6
    # 所有 span event 都掛同一個 trace_id
    trace_id = events[0]["trace_id"]
    for e in span_events:
        assert e["trace_id"] == trace_id


@pytest.mark.unit
def test_jsonl_processor_close_is_idempotent(tmp_path: Path) -> None:
    """重複 close 不應丟例外。"""
    proc = JsonlTracingProcessor(tmp_path / "x.jsonl")
    proc.close()
    proc.close()  # 第二次也安全


# ---------------------------------------------------------------------------
# ConsoleTracingProcessor
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_console_processor_writes_to_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    """ConsoleTracingProcessor 預設應寫至 stdout,且事件可被 capsys 攔下。"""
    tracer = Tracer()
    tracer.register_processor(ConsoleTracingProcessor())

    with tracer.start_trace("agent.run"), tracer.start_span("tool.call.read"):
        pass

    captured = capsys.readouterr()
    lines = captured.out.strip().splitlines()
    # 1 trace + 1 span -> 2 + 2 = 4 行
    assert len(lines) == 4
    events = [json.loads(line) for line in lines]
    assert events[0]["event"] == "trace.start"
    assert events[1]["event"] == "span.start"
    assert events[2]["event"] == "span.end"
    assert events[3]["event"] == "trace.end"


@pytest.mark.unit
def test_console_processor_custom_stream() -> None:
    """ConsoleTracingProcessor 可接受自訂 stream(例如 StringIO)。"""
    buf = io.StringIO()
    proc = ConsoleTracingProcessor(stream=buf)
    tracer = Tracer()
    tracer.register_processor(proc)

    with tracer.start_trace("w"):
        pass

    lines = buf.getvalue().strip().splitlines()
    assert len(lines) == 2  # trace.start + trace.end


# ---------------------------------------------------------------------------
# 多 processor 與巢狀 span
# ---------------------------------------------------------------------------


class _RecordingProcessor:
    """測試用 processor — 把所有事件存到 list 內,順序、型別、payload 皆可驗。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def on_trace_start(self, trace: Trace) -> None:
        self.events.append(("trace.start", trace.trace_id))

    def on_trace_end(self, trace: Trace) -> None:
        self.events.append(("trace.end", trace.trace_id))

    def on_span_start(self, span: Span) -> None:
        self.events.append(("span.start", span.span_id))

    def on_span_end(self, span: Span) -> None:
        self.events.append(("span.end", span.span_id))


@pytest.mark.unit
def test_multiple_processors_all_receive_events() -> None:
    """同時註冊多個 processor 皆應收到同一份事件序列。"""
    tracer = Tracer()
    p1 = _RecordingProcessor()
    p2 = _RecordingProcessor()
    tracer.register_processor(p1)
    tracer.register_processor(p2)

    with tracer.start_trace("w"), tracer.start_span("s"):
        pass

    assert p1.events == p2.events
    assert [e[0] for e in p1.events] == [
        "trace.start",
        "span.start",
        "span.end",
        "trace.end",
    ]


@pytest.mark.unit
def test_recording_processor_is_tracing_processor_protocol() -> None:
    """`_RecordingProcessor` 隱式符合 ``TracingProcessor`` Protocol(duck typing)。"""
    p = _RecordingProcessor()
    # @runtime_checkable Protocol 可用 isinstance 確認
    assert isinstance(p, TracingProcessor)


@pytest.mark.unit
def test_nested_span_parent_id_set_correctly() -> None:
    """巢狀 span 應自動以外層 span 為 parent。"""
    tracer = Tracer()
    with tracer.start_trace("w") as trace, tracer.start_span("outer") as outer:
        assert outer.parent_span_id is None
        assert outer.trace_id == trace.trace_id
        with tracer.start_span("inner") as inner:
            assert inner.parent_span_id == outer.span_id
            assert inner.trace_id == trace.trace_id


@pytest.mark.unit
def test_span_without_active_trace_logs_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """無 active trace 時呼叫 start_span 仍可運作,但 trace_id 為空字串並印 warning。"""
    import logging

    tracer = Tracer()
    with (
        caplog.at_level(logging.WARNING, logger="anila_agent.tracing.tracer"),
        tracer.start_span("orphan") as span,
    ):
        assert span.trace_id == ""
    assert any("orphan" in rec.message for rec in caplog.records)


@pytest.mark.unit
def test_tracer_clear_processors() -> None:
    """``clear_processors`` 應移除所有已註冊 processor。"""
    tracer = Tracer()
    p = _RecordingProcessor()
    tracer.register_processor(p)
    assert len(tracer.processors) == 1
    tracer.clear_processors()
    assert tracer.processors == ()

    with tracer.start_trace("w"):
        pass
    # 已清空,p 不應再收到事件
    assert p.events == []
