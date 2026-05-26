"""anila-agent tracing sub-package。

提供 trace / span lifecycle 與 processor 抽象,用於觀測 agent run 的內部
operation 流向(LLM 呼叫、tool 執行、handoff 等)。設計時對齊 OpenTelemetry
semantic conventions,但不引入 OTel 依賴(只用標準函式庫)。

公開介面:
    Trace: 一次完整 agent run 的根區塊 dataclass。
    Span: trace 內的單一 operation 區段 dataclass。
    SpanStatus: ``Literal["ok", "error", "in_progress"]``。
    TracingProcessor: trace / span 生命週期事件處理 Protocol。
    JsonlTracingProcessor: 將事件寫入 JSONL 檔的 processor。
    ConsoleTracingProcessor: 將事件寫到 stdout 的 processor(開發用)。
    Tracer: tracer 主入口 — 提供 ``start_trace`` / ``start_span`` context manager。

整合點:本 sub-package 為 standalone infrastructure,尚未與 lifecycle hook
(P0-1)接通。後續 P1 階段可在 hook callback 內呼叫 ``tracer.start_span(...)``
把 tool / handoff 事件導入 trace。
"""

from anila_agent.tracing.processor import (
    ConsoleTracingProcessor,
    JsonlTracingProcessor,
    TracingProcessor,
)
from anila_agent.tracing.tracer import Tracer
from anila_agent.tracing.types import Span, SpanStatus, Trace

__all__ = [
    "ConsoleTracingProcessor",
    "JsonlTracingProcessor",
    "Span",
    "SpanStatus",
    "Trace",
    "Tracer",
    "TracingProcessor",
]
