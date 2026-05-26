"""Tracing processor:抽象介面 + 兩個具體實作。

`TracingProcessor` Protocol 定義 trace / span 生命週期事件回呼;
:class:`JsonlTracingProcessor` 與 :class:`ConsoleTracingProcessor` 為附帶的
兩個具體實作,分別寫 JSONL 檔與 stdout(後者開發用)。

設計上參考 openai-agents 的 `TracingProcessor` 介面,但簡化為 Protocol(對應
Python 結構化型別 / duck typing),讓使用者不必繼承也可實作。所有方法皆非
async — 呼叫者(:class:`Tracer`)在同步路徑上 fire event,processor 自負其責
不可長時間阻塞。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import IO, Protocol, runtime_checkable

from .types import Span, Trace


@runtime_checkable
class TracingProcessor(Protocol):
    """trace / span 生命週期事件處理介面。

    所有方法皆為同步呼叫,implementer 不應在其中阻塞(IO 過久應另起 thread /
    非同步 queue)。任何例外都應該內部吃掉,以免破壞 agent 主流程。

    Methods:
        on_trace_start: 一個 trace 進入 context 時觸發。
        on_trace_end: trace 結束(無論成功或失敗)時觸發。
        on_span_start: span 進入 context 時觸發。
        on_span_end: span 結束時觸發。
    """

    def on_trace_start(self, trace: Trace) -> None: ...

    def on_trace_end(self, trace: Trace) -> None: ...

    def on_span_start(self, span: Span) -> None: ...

    def on_span_end(self, span: Span) -> None: ...


class JsonlTracingProcessor:
    """寫 JSONL(JSON Lines)的 processor。

    每個生命週期事件(trace_start / trace_end / span_start / span_end)會在輸出
    檔內各佔一行 JSON object,event 型別由 ``"event"`` 欄位區分。檔案以 append
    模式打開,並在 :meth:`close` 被呼叫時關閉。

    Example:
        ```python
        proc = JsonlTracingProcessor("/tmp/trace.jsonl")
        tracer.register_processor(proc)
        ...
        proc.close()
        ```

    Attributes:
        path: 輸出 JSONL 檔案路徑。
    """

    def __init__(self, path: str | Path) -> None:
        """開檔(append 模式,UTF-8)。"""
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fp: IO[str] = self.path.open("a", encoding="utf-8")

    def _write(self, event: str, payload: dict[str, object]) -> None:
        """寫一行 JSONL — ``{"event": ..., ...payload}``。"""
        if self._fp.closed:
            return
        record = {"event": event, **payload}
        self._fp.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        self._fp.flush()

    def on_trace_start(self, trace: Trace) -> None:
        self._write("trace.start", trace.to_dict())

    def on_trace_end(self, trace: Trace) -> None:
        self._write("trace.end", trace.to_dict())

    def on_span_start(self, span: Span) -> None:
        self._write("span.start", span.to_dict())

    def on_span_end(self, span: Span) -> None:
        self._write("span.end", span.to_dict())

    def close(self) -> None:
        """關閉檔案 handle(重複呼叫安全)。"""
        if not self._fp.closed:
            self._fp.close()


class ConsoleTracingProcessor:
    """寫 stdout 的 processor(開發用)。

    主要供開發階段觀察 trace 流向。每個事件以單行 JSON 輸出至指定 stream
    (預設 :data:`sys.stdout`),方便在 terminal 上肉眼掃描或以 ``jq`` 過濾。
    """

    def __init__(self, stream: IO[str] | None = None) -> None:
        """指定輸出 stream(預設為 stdout)。"""
        self._stream: IO[str] = stream if stream is not None else sys.stdout

    def _write(self, event: str, payload: dict[str, object]) -> None:
        record = {"event": event, **payload}
        # 注意:這裡刻意使用 stream.write 而非 print,以便測試時透過
        # capsys 或 monkeypatch 控制輸出目的地。
        self._stream.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        self._stream.flush()

    def on_trace_start(self, trace: Trace) -> None:
        self._write("trace.start", trace.to_dict())

    def on_trace_end(self, trace: Trace) -> None:
        self._write("trace.end", trace.to_dict())

    def on_span_start(self, span: Span) -> None:
        self._write("span.start", span.to_dict())

    def on_span_end(self, span: Span) -> None:
        self._write("span.end", span.to_dict())
