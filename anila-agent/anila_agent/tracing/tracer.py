"""`Tracer` — tracing 系統的主入口。

提供 ``start_trace`` / ``start_span`` 兩個 context manager,並支援同時註冊
多個 :class:`TracingProcessor`。span 的巢狀關係由 internal stack 自動維護
(每次 ``start_span`` 會以當前最內層 span 為 parent),使用者無需手動傳
``parent_span_id``。

設計考量:
- 採 thread-local stack 維護 trace / span 巢狀層,避免不同 thread 互相干擾。
- processor 觸發失敗(例如 disk full)不可影響主流程 — 一律 catch 內部例外。
- 不引入 OTel 或其他 tracing 套件依賴;純標準函式庫(`threading` / `contextlib`)。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

from .processor import TracingProcessor
from .types import Span, Trace

logger = logging.getLogger(__name__)


class _ThreadLocalStack(threading.local):
    """thread-local 維護當前 trace 與 span stack。

    每個 thread 各自一份;trace 與 span 的巢狀關係皆以 stack 結構表示
    (進場 push、出場 pop)。
    """

    def __init__(self) -> None:
        """thread 首次存取時初始化兩個 stack 為空 list。"""
        self.traces: list[Trace] = []
        self.spans: list[Span] = []


class Tracer:
    """tracing 系統主入口 — 管理 trace / span 生命週期並廣播事件給 processors。

    Example:
        ```python
        tracer = Tracer()
        tracer.register_processor(JsonlTracingProcessor("/tmp/trace.jsonl"))

        with tracer.start_trace("agent.run") as trace:
            with tracer.start_span("llm.completion", attributes={"llm.model": "gpt-4o"}) as span:
                span.attributes["llm.tokens.prompt"] = 123
            # span 自動結束(status=ok),trace 也是
        ```

    注意:
    - ``start_span`` 不顯式接受 ``parent``;parent 取自當前 thread 內最內層 span
      (若無則為 trace 的頂層 — ``parent_span_id=None``)。如需手動跨層 parent,
      請直接建構 :class:`Span` 並呼叫 :meth:`fire_span_event` 系列方法(本版未提供
      該逃生口,後續視需求再開)。
    - 當前 thread 無 active trace 時呼叫 ``start_span`` 仍會建立 span,但其
      ``trace_id`` 將為空字串並印 warning — 設計上建議一律先包在 ``start_trace``
      內。
    """

    def __init__(self) -> None:
        """初始化空 processor list 與 thread-local stack。"""
        self._processors: list[TracingProcessor] = []
        self._local = _ThreadLocalStack()
        self._lock = threading.Lock()  # 保護 _processors 註冊清單

    # ------------------------------------------------------------------
    # processor 註冊
    # ------------------------------------------------------------------
    def register_processor(self, processor: TracingProcessor) -> None:
        """新增一個 processor;後續所有 trace / span 事件都會廣播給它。"""
        with self._lock:
            self._processors.append(processor)

    def clear_processors(self) -> None:
        """清空所有已註冊 processor(測試或關閉服務時用)。"""
        with self._lock:
            self._processors.clear()

    @property
    def processors(self) -> tuple[TracingProcessor, ...]:
        """以不可變 tuple 形式回傳當前註冊清單(避免外部誤動)。"""
        with self._lock:
            return tuple(self._processors)

    # ------------------------------------------------------------------
    # 當前 trace / span 查詢(供整合層使用)
    # ------------------------------------------------------------------
    @property
    def current_trace(self) -> Trace | None:
        """回傳當前 thread 最內層 trace;若無則 None。"""
        return self._local.traces[-1] if self._local.traces else None

    @property
    def current_span(self) -> Span | None:
        """回傳當前 thread 最內層 span;若無則 None。"""
        return self._local.spans[-1] if self._local.spans else None

    # ------------------------------------------------------------------
    # context manager 入口
    # ------------------------------------------------------------------
    @contextmanager
    def start_trace(
        self,
        name: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> Iterator[Trace]:
        """開啟一段 trace 區塊。

        進入時建立 :class:`Trace`、設定 ``start_time``、廣播 ``on_trace_start``;
        離開時設 ``end_time`` 並廣播 ``on_trace_end``。

        Args:
            name: workflow 名稱。
            metadata: 自訂中繼資料(例如 group_id、user_id)。

        Yields:
            Trace: 已建立的 trace 物件,user 可在 with block 內修改其
            ``metadata``。
        """
        trace = Trace(
            name=name,
            start_time=datetime.now(timezone.utc),
            metadata=dict(metadata) if metadata else {},
        )
        self._local.traces.append(trace)
        self._fire_trace_start(trace)
        try:
            yield trace
        finally:
            trace.end_time = datetime.now(timezone.utc)
            self._fire_trace_end(trace)
            # 防呆:確保 pop 的真的是同一個 trace(理論上應該是)。
            if self._local.traces and self._local.traces[-1] is trace:
                self._local.traces.pop()
            else:
                logger.warning(
                    "tracer stack out of sync on trace exit: %s", trace.trace_id
                )

    @contextmanager
    def start_span(
        self,
        name: str,
        *,
        attributes: dict[str, Any] | None = None,
    ) -> Iterator[Span]:
        """開啟一段 span 區塊。

        parent 自動取為當前 thread 最內層 span(若無則為 trace 頂層,即
        ``parent_span_id=None``)。進入時廣播 ``on_span_start``,離開時依
        block 是否丟例外設 ``status="ok"`` 或 ``"error"`` 後廣播
        ``on_span_end``。

        Args:
            name: span 名稱(建議用點分隔命名空間,如 ``"tool.call.search"``)。
            attributes: 自訂屬性(建議對齊 OTel semantic conventions)。

        Yields:
            Span: 已建立的 span 物件,user 可在 with block 內補 attributes。
        """
        trace = self.current_trace
        parent = self.current_span
        if trace is None:
            # 不阻擋使用 — 但 trace_id 將為空字串,並記 warning 提示。
            logger.warning(
                "start_span('%s') called outside any active trace; trace_id will be empty",
                name,
            )
        span = Span(
            trace_id=trace.trace_id if trace is not None else "",
            parent_span_id=parent.span_id if parent is not None else None,
            name=name,
            start_time=datetime.now(timezone.utc),
            attributes=dict(attributes) if attributes else {},
            status="in_progress",
        )
        self._local.spans.append(span)
        self._fire_span_start(span)
        try:
            yield span
        except Exception as exc:
            span.status = "error"
            span.error = f"{type(exc).__name__}: {exc}"
            raise
        else:
            # 若 user 在 block 內主動把 status 設成 error 就尊重該設定;
            # 否則預設視為 ok。
            if span.status == "in_progress":
                span.status = "ok"
        finally:
            span.end_time = datetime.now(timezone.utc)
            self._fire_span_end(span)
            if self._local.spans and self._local.spans[-1] is span:
                self._local.spans.pop()
            else:
                logger.warning(
                    "tracer stack out of sync on span exit: %s", span.span_id
                )

    # ------------------------------------------------------------------
    # 內部事件廣播(processor 例外吞掉,以免破壞主流程)
    # ------------------------------------------------------------------
    def _fire_trace_start(self, trace: Trace) -> None:
        for proc in self.processors:
            try:
                proc.on_trace_start(trace)
            except Exception:
                logger.exception("processor on_trace_start raised; swallowed")

    def _fire_trace_end(self, trace: Trace) -> None:
        for proc in self.processors:
            try:
                proc.on_trace_end(trace)
            except Exception:
                logger.exception("processor on_trace_end raised; swallowed")

    def _fire_span_start(self, span: Span) -> None:
        for proc in self.processors:
            try:
                proc.on_span_start(span)
            except Exception:
                logger.exception("processor on_span_start raised; swallowed")

    def _fire_span_end(self, span: Span) -> None:
        for proc in self.processors:
            try:
                proc.on_span_end(span)
            except Exception:
                logger.exception("processor on_span_end raised; swallowed")
