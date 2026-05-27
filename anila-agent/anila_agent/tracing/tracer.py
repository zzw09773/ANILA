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

    P2-9 額外加上 ``current_query_id`` — sub-agent dispatch / SessionContext
    開啟 ``start_query()`` 時設,所有後續開的 span 會自動繼承 query_id 欄位。
    """

    def __init__(self) -> None:
        """thread 首次存取時初始化兩個 stack 為空 list。"""
        self.traces: list[Trace] = []
        self.spans: list[Span] = []
        self.current_query_id: str | None = None


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
        """初始化空 processor list 與 thread-local stack。

        P2-9 額外維護 ``_traces_by_chain`` index — 以 ``chain_id`` 為 key,
        記錄所有 已開啟的 trace,供 :meth:`find_traces_by_chain` 查詢。
        """
        self._processors: list[TracingProcessor] = []
        self._local = _ThreadLocalStack()
        self._lock = threading.Lock()  # 保護 _processors 註冊清單
        # P2-9 chain index:chain_id -> list[Trace]。由 _lock 一併保護,以防
        # 多 thread 同時 fork sub-agent 造成 dict 競爭。
        self._traces_by_chain: dict[str, list[Trace]] = {}

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
    # P2-9 query_id thread-local 操作(供 SessionContext.start_query 用)
    # ------------------------------------------------------------------
    @property
    def current_query_id(self) -> str | None:
        """回傳當前 thread 的 active query_id;若無則 None。"""
        return self._local.current_query_id

    def set_current_query_id(self, query_id: str | None) -> None:
        """設定當前 thread 的 active query_id(供 SessionContext.start_query 用)。

        傳 ``None`` 表示清除。後續所有 :meth:`start_span` 都會把這個 id
        寫到 span 的 ``query_id`` 欄位。
        """
        self._local.current_query_id = query_id

    # ------------------------------------------------------------------
    # P2-9 chain index 查詢
    # ------------------------------------------------------------------
    def find_traces_by_chain(self, chain_id: str) -> list[Trace]:
        """找出同 chain_id 的所有 trace,回傳淺拷貝 list(避免外部 mutate)。

        用於分析「這個 user 一輪 query 觸發了多少 LLM call / sub-agent」。
        若 chain_id 從未被註冊過,回傳空 list。

        Args:
            chain_id: 要查的 chain 識別碼。

        Returns:
            list[Trace]: 同 chain 內的所有 trace(以 start_time 升冪排序的
            插入順序),若無則 ``[]``。
        """
        with self._lock:
            return list(self._traces_by_chain.get(chain_id, ()))

    # ------------------------------------------------------------------
    # context manager 入口
    # ------------------------------------------------------------------
    @contextmanager
    def start_trace(
        self,
        name: str,
        *,
        metadata: dict[str, Any] | None = None,
        chain_id: str | None = None,
        parent_trace_id: str | None = None,
        depth: int | None = None,
    ) -> Iterator[Trace]:
        """開啟一段 trace 區塊。

        進入時建立 :class:`Trace`、設定 ``start_time``、廣播 ``on_trace_start``;
        離開時設 ``end_time`` 並廣播 ``on_trace_end``。

        P2-9 multi-agent chain 行為:

        * 若呼叫端**已有 active trace**(代表 sub-agent dispatch),且未顯式
          指定 ``chain_id`` / ``parent_trace_id`` / ``depth``,新 trace 會
          自動繼承 outer trace 的 ``chain_id``、把 outer trace 的 ``trace_id``
          設成 ``parent_trace_id``,並把 ``depth`` 設為 ``outer.depth + 1``。
        * 若呼叫端**無 active trace**(代表 root agent run),且未顯式指定
          ``chain_id``,新 trace 會以自己的 ``trace_id`` 當作 chain 起點,
          這樣 ``find_traces_by_chain(trace.trace_id)`` 可以撈到整 chain。
        * 顯式指定任一欄位則優先採用,讓上層整合(P0-8 forkSubagent)可
          自行決定 chain 結構。

        Args:
            name: workflow 名稱。
            metadata: 自訂中繼資料(例如 group_id、user_id)。
            chain_id: 同一 user request 觸發的所有 trace 共用識別碼;
                ``None`` 表示自動繼承或自我起 chain(見上)。
            parent_trace_id: 上一層 agent 的 trace id;``None`` 表示自動。
            depth: chain 內深度;``None`` 表示自動推。

        Yields:
            Trace: 已建立的 trace 物件,user 可在 with block 內修改其
            ``metadata``。
        """
        outer = self.current_trace
        # 自動繼承 chain_id / parent_trace_id / depth(若呼叫端未指定)。
        effective_chain_id = chain_id
        effective_parent_trace_id = parent_trace_id
        effective_depth = depth
        if effective_parent_trace_id is None and outer is not None:
            effective_parent_trace_id = outer.trace_id
        if effective_depth is None:
            effective_depth = (outer.depth + 1) if outer is not None else 0
        if effective_chain_id is None:
            effective_chain_id = outer.chain_id if outer is not None else None

        trace = Trace(
            name=name,
            start_time=datetime.now(timezone.utc),
            metadata=dict(metadata) if metadata else {},
            chain_id=effective_chain_id,
            parent_trace_id=effective_parent_trace_id,
            depth=effective_depth,
        )
        # root 且未指定 chain_id 時,以自己的 trace_id 起 chain,方便
        # find_traces_by_chain(root.trace_id) 撈整條。
        if trace.chain_id is None:
            trace.chain_id = trace.trace_id

        self._local.traces.append(trace)
        # 加進 chain index(_lock 保護)。
        with self._lock:
            self._traces_by_chain.setdefault(trace.chain_id, []).append(trace)
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
            # P2-9:chain_id 從 trace 繼承;query_id 從 thread-local 抓。
            chain_id=trace.chain_id if trace is not None else None,
            query_id=self._local.current_query_id,
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
