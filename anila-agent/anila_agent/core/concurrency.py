"""runTools concurrency partition — read-only 平行 / destructive 串行。

本模組對應 enhancement roadmap **P1-2**:把 LLM 一輪 emit 的一批 tool call 依
metadata(`concurrency_safe` / `is_read_only` / `is_destructive`)拆成兩組:

* **平行組(concurrency_safe=True)**:用 ``asyncio.gather`` 同時跑,讀類 tool
  立刻 fan-out。
* **串行組(concurrency_safe=False)**:逐個 ``await``,寫類 tool 避免 race。

設計參考 claude-code-src ``src/services/tools/toolOrchestration.ts`` 的
``runTools`` + ``partitionToolCalls``(deep-dive §4.3),但 ANILA 版本做兩處
取捨:

1. 上游 TS 用 generator 串流 partial output;Python 版採 collect-then-return 一次回
   完整 ``ToolResult`` list — 符合 openai-agents 既有 ``Runner`` 收 tool 結果的
   契約。
2. 上游 TS 維持 LLM 順序逐批切換 read-only / write 區塊;ANILA 版做 **stable
   partition** — 先全部平行讀完再做寫入,語義上更接近「先把所有 read-only 結果
   finalize、再讓 write 看到 read 後狀態」。
   若呼叫端需要嚴格 LLM 順序語義(read-write 交錯),可在 LLM prompt 端要求
   tool call 串行排列,或未來新增 ``preserve_call_order`` flag(目前不需要)。

invariant:
* partition 為 **stable** — 兩組 list 各自保留 input ``tool_calls`` 的相對順序。
* ``run_tool_calls`` 回傳的 ``ToolResult`` list 順序 **嚴格對齊** input 順序
  (不是執行完成順序)。
* 任何 tool 內 raise 例外都會被吃下並轉成 ``ToolResult.error``,確保 batch 內
  其他 tool 不被連坐失敗。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from anila_agent.tools.base import get_metadata
from anila_agent.tools.registry import ToolRegistry
from anila_agent.tracing import Span, Tracer

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ToolCall / ToolResult dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolCall:
    """LLM 在一輪 ``assistant_message`` 內 emit 的單一 tool call。

    對齊 Anthropic / OpenAI tool_use 區塊:LLM 給 tool ``name`` + 結構化 ``args``
    + 該次 call 的 ``call_id``(下游 tool_result message 必須回填同一 id)。

    Attributes:
        name: tool 名稱(必須能在 :class:`ToolRegistry` 中查到)。
        args: tool 的結構化參數(已從 JSON 解析)。
        call_id: 此次 call 的識別碼。用來把 ``ToolResult`` 對應回原始 call,
            並作為 tracing span 的 attribute。
    """

    name: str
    args: dict[str, Any] = field(default_factory=dict)
    call_id: str = ""


@dataclass
class ToolResult:
    """單一 tool call 執行結果。

    成功:``output`` 填 tool 回傳值、``error`` 為 None。
    失敗:``output`` 為 None、``error`` 填例外訊息(已含 type name)。

    Attributes:
        call_id: 對應 :class:`ToolCall.call_id`,讓呼叫端把結果回填給 LLM。
        output: tool 成功時的回傳值;失敗時為 ``None``。
        error: tool 失敗時的錯誤訊息(``"<TypeName>: <msg>"``);成功時為 ``None``。
        duration_ms: 此 tool call 從 invoke 到 return 的耗時(毫秒)。
    """

    call_id: str
    output: Any = None
    error: str | None = None
    duration_ms: float = 0.0


# ---------------------------------------------------------------------------
# ToolInvoker — 把 ToolCall 跑成 ToolResult 的抽象
# ---------------------------------------------------------------------------

# 呼叫端注入的 invoker:吃 (tool_call, registry) 回傳 awaitable output。
# 提供 default invoker(query registry tool 的 ``on_invoke_tool`` 或直接呼叫 fn)。
ToolInvoker = Callable[[ToolCall, ToolRegistry], Awaitable[Any]]


async def _default_invoker(tool_call: ToolCall, registry: ToolRegistry) -> Any:
    """預設 invoker — 用 registry 查 tool 再呼叫其 callable。

    優先順序:
    1. 若 tool 物件有 ``on_invoke_tool`` (openai-agents FunctionTool 介面),呼叫之。
    2. 退而求其次:把 tool 物件直接視為 callable。
    3. 若 callable 是 async,await 之;若為 sync,在 default executor 內跑。

    args 以 keyword 展開傳入 tool。
    """
    if tool_call.name not in registry.tools:
        raise KeyError(f"Tool not registered: {tool_call.name!r}")
    tool = registry.tools[tool_call.name]

    # openai-agents FunctionTool 對外契約:on_invoke_tool(ctx, json_args) → str。
    # 但我們這層走 dict 介面比較單純;若呼叫端要走 openai-agents 路徑,請自行
    # 注入 invoker。default 走「直接 callable」最簡。
    target: Callable[..., Any]
    if hasattr(tool, "on_invoke_tool"):
        # 走 openai-agents 路徑時請改用呼叫端注入的 invoker。
        raise TypeError(
            f"Tool {tool_call.name!r} is an openai-agents FunctionTool; "
            "default invoker does not support it. Pass a custom invoker via run_tool_calls(...)."
        )
    if callable(tool):
        target = tool
    else:
        raise TypeError(f"Tool {tool_call.name!r} is neither callable nor FunctionTool")

    if inspect.iscoroutinefunction(target):
        return await target(**tool_call.args)
    # 同步函式 — 丟到 default executor 不阻塞 event loop。
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: target(**tool_call.args))


# ---------------------------------------------------------------------------
# Partition
# ---------------------------------------------------------------------------


def partition_tool_calls(
    tool_calls: list[ToolCall],
    registry: ToolRegistry,
) -> tuple[list[ToolCall], list[ToolCall]]:
    """依 tool metadata 把 tool_calls 拆成 (parallel_safe, sequential_only)。

    判斷規則:
    * tool 在 registry 內且 metadata 的 ``concurrency_safe=True`` → 平行組。
    * 其餘(包含 ``is_destructive=True`` / ``concurrency_safe=False`` /
      tool 不在 registry)→ 串行組(保守處理)。

    invariant:**stable partition** — 兩組 list 各自保留 input 的相對順序,
    呼叫端可在 collect 結果後依 ``call_id`` 對齊原順序。

    Args:
        tool_calls: LLM 在一輪 emit 的 tool call list。
        registry: 用來查 tool metadata 的 registry。

    Returns:
        ``(parallel_safe, sequential_only)`` — 兩個 list,聯集等於 ``tool_calls``,
        各自保留原相對順序。
    """
    parallel: list[ToolCall] = []
    sequential: list[ToolCall] = []
    for call in tool_calls:
        if _is_parallel_safe(call, registry):
            parallel.append(call)
        else:
            sequential.append(call)
    return parallel, sequential


def _is_parallel_safe(call: ToolCall, registry: ToolRegistry) -> bool:
    """單一 tool call 是否可平行。

    不在 registry 內的 tool 一律當「不可平行」處理(保守),避免讓 race 漏掉。
    """
    if call.name not in registry.tools:
        return False
    meta = get_metadata(registry.tools[call.name])
    # concurrency_safe 為 None 時 ToolMetadata.__post_init__ 已依 is_read_only 推導,
    # 這裡可以直接信任這個欄位。
    return bool(meta.concurrency_safe)


# ---------------------------------------------------------------------------
# 主入口:run_tool_calls
# ---------------------------------------------------------------------------


async def run_tool_calls(
    tool_calls: list[ToolCall],
    registry: ToolRegistry,
    ctx: Any = None,  # 保留參數位:後續可接 AnilaToolContext / RunContext。
    *,
    invoker: ToolInvoker | None = None,
    tracer: Tracer | None = None,
    max_concurrency: int | None = None,
) -> list[ToolResult]:
    """跑一整批 tool call,read-only 平行 / destructive 串行。

    執行順序:
    1. 先呼叫 :func:`partition_tool_calls` 拆兩組(stable)。
    2. 平行組:用 ``asyncio.gather`` 同時跑,可選用 semaphore 限制併發。
    3. 串行組:逐個 ``await``。
    4. 回傳時把兩組結果依 input ``tool_calls`` 順序對齊(不是執行完成順序)。

    tracing(若有傳 ``tracer``):
    * 整批包一個 parent span ``"tool_batch"``,attribute 含
      ``tool_batch.size`` / ``tool_batch.parallel_count`` / ``tool_batch.sequential_count``。
    * 每個 tool call 一個 child span ``"tool.<name>"``,parent 為 batch span,
      attribute 含 ``tool.name`` / ``tool.call_id`` / ``tool.duration_ms`` /
      ``tool.parallel``(True/False)、失敗時 ``status=error`` 並填 ``error``。

    Args:
        tool_calls: LLM 在一輪 emit 的 tool call list。
        registry: tool registry,提供 metadata 查詢與 tool 物件查找。
        ctx: 預留 — 後續可注入 :class:`anila_agent.core.AnilaToolContext`
            或自訂 RunContext。當前 default invoker 不使用此參數。
        invoker: 自訂 tool 執行函式;``None`` 時用 :func:`_default_invoker`。
            訊號:當你的 registry 內是 openai-agents ``FunctionTool``,務必注入
            自己的 invoker(default 不支援)。
        tracer: 若提供則自動產生 batch + child span。``None`` 時純跑邏輯。
        max_concurrency: 平行組最大同時 in-flight 數量;``None`` 不限。

    Returns:
        ``list[ToolResult]`` 順序對齊 input ``tool_calls``。每個 result 帶
        ``call_id`` / ``output``(成功) / ``error``(失敗) / ``duration_ms``。
    """
    if not tool_calls:
        return []

    actual_invoker = invoker or _default_invoker
    parallel, sequential = partition_tool_calls(tool_calls, registry)

    # 結果 buffer:以 call_id 為 key,最後再依 input 順序組回 list。
    results: dict[str, ToolResult] = {}

    if tracer is not None:
        with tracer.start_span(
            "tool_batch",
            attributes={
                "tool_batch.size": len(tool_calls),
                "tool_batch.parallel_count": len(parallel),
                "tool_batch.sequential_count": len(sequential),
            },
        ) as batch_span:
            await _execute_batch(
                parallel=parallel,
                sequential=sequential,
                results=results,
                invoker=actual_invoker,
                registry=registry,
                tracer=tracer,
                batch_span=batch_span,
                max_concurrency=max_concurrency,
            )
    else:
        await _execute_batch(
            parallel=parallel,
            sequential=sequential,
            results=results,
            invoker=actual_invoker,
            registry=registry,
            tracer=None,
            batch_span=None,
            max_concurrency=max_concurrency,
        )

    # 順序對齊 input:用 input 順序找 result。
    aligned: list[ToolResult] = []
    for call in tool_calls:
        if call.call_id in results:
            aligned.append(results[call.call_id])
        else:
            # 防呆 — 理論上不會發生,但若 invoker 漏吐結果就補一個 error。
            aligned.append(
                ToolResult(
                    call_id=call.call_id,
                    error="internal: no result recorded for call_id",
                )
            )
    return aligned


async def _execute_batch(
    *,
    parallel: list[ToolCall],
    sequential: list[ToolCall],
    results: dict[str, ToolResult],
    invoker: ToolInvoker,
    registry: ToolRegistry,
    tracer: Tracer | None,
    batch_span: Span | None,
    max_concurrency: int | None,
) -> None:
    """執行 partition 後的兩組:先平行(read-only)再串行(destructive)。

    為何 **平行先**:read-only 不會改外部狀態,先 finalize 後再開始 write,確保
    write 看到的是 stable 的 read 結果(避免 read 中途被 write race)。

    Args:
        parallel: 平行組 tool calls。
        sequential: 串行組 tool calls。
        results: 結果累積 buffer(以 call_id 為 key);本函式 in-place 寫入。
        invoker: tool 執行函式。
        registry: tool registry(供 invoker 與 tracing attribute 使用)。
        tracer: tracing 物件;``None`` 時不開 child span。
        batch_span: 已開啟的 batch span(用來取 trace_id / span_id 作為 child
            的 parent);僅當 ``tracer is not None`` 時為非 None。
        max_concurrency: 平行組最大同時 in-flight 數量。
    """
    # ---- 平行組:read-only / concurrency_safe 並發跑 ---------------------
    if parallel:
        semaphore: asyncio.Semaphore | None = (
            asyncio.Semaphore(max_concurrency) if max_concurrency else None
        )

        async def _gated(call: ToolCall) -> ToolResult:
            if semaphore is not None:
                async with semaphore:
                    return await _invoke_one(
                        call, invoker, registry, tracer, batch_span, parallel=True
                    )
            return await _invoke_one(
                call, invoker, registry, tracer, batch_span, parallel=True
            )

        # 用 gather:任何 tool 內已被 _invoke_one catch 例外轉成 ToolResult,
        # 不會讓 gather 整個 fail-fast。
        parallel_results = await asyncio.gather(*[_gated(c) for c in parallel])
        for r in parallel_results:
            results[r.call_id] = r

    # ---- 串行組:destructive / concurrency_safe=False 逐個跑 -------------
    for call in sequential:
        r = await _invoke_one(
            call, invoker, registry, tracer, batch_span, parallel=False
        )
        results[r.call_id] = r


async def _invoke_one(
    call: ToolCall,
    invoker: ToolInvoker,
    registry: ToolRegistry,
    tracer: Tracer | None,
    batch_span: Span | None,
    *,
    parallel: bool,
) -> ToolResult:
    """跑單一 tool call,計時、catch 例外、發 tracing 事件。

    重要:tracing 走「手動建構 Span + 直接走 processor 廣播」路徑,**不用**
    ``tracer.start_span`` context manager — 因為 ``asyncio.gather`` 內多個 task
    共用同一 thread 的 thread-local span stack,context manager 會錯誤 nest。
    手動建 span 並指定 ``parent_span_id = batch_span.span_id`` 才能保證 parent
    關係正確。

    Args:
        call: 要跑的 tool call。
        invoker: tool 執行函式。
        registry: tool registry(供 attribute 標記 tool 分類用)。
        tracer: tracing 物件;``None`` 時不發 span event。
        batch_span: parent batch span(若有 tracer)。
        parallel: 是否為平行組,記到 span attribute 供 debug。

    Returns:
        :class:`ToolResult`:成功時 ``output`` 填,失敗時 ``error`` 填,皆帶
        ``duration_ms``。
    """
    span: Span | None = None
    if tracer is not None and batch_span is not None:
        span = Span(
            trace_id=batch_span.trace_id,
            parent_span_id=batch_span.span_id,
            name=f"tool.{call.name}",
            start_time=datetime.now(timezone.utc),
            attributes={
                "tool.name": call.name,
                "tool.call_id": call.call_id,
                "tool.parallel": parallel,
            },
            status="in_progress",
        )
        # 用 tracer.processors 直接廣播 — 避開 thread-local stack。
        _fire_span_start(tracer, span)

    start_ns = time.perf_counter_ns()
    try:
        output = await invoker(call, registry)
    except Exception as exc:  # 一律 catch,轉成 ToolResult.error
        duration_ms = (time.perf_counter_ns() - start_ns) / 1_000_000
        error_msg = f"{type(exc).__name__}: {exc}"
        if span is not None and tracer is not None:
            span.status = "error"
            span.error = error_msg
            span.attributes["tool.duration_ms"] = duration_ms
            span.end_time = datetime.now(timezone.utc)
            _fire_span_end(tracer, span)
        return ToolResult(
            call_id=call.call_id,
            output=None,
            error=error_msg,
            duration_ms=duration_ms,
        )

    duration_ms = (time.perf_counter_ns() - start_ns) / 1_000_000
    if span is not None and tracer is not None:
        span.status = "ok"
        span.attributes["tool.duration_ms"] = duration_ms
        span.end_time = datetime.now(timezone.utc)
        _fire_span_end(tracer, span)
    return ToolResult(
        call_id=call.call_id,
        output=output,
        error=None,
        duration_ms=duration_ms,
    )


def _fire_span_start(tracer: Tracer, span: Span) -> None:
    """直接走 tracer 註冊的 processors 發 ``on_span_start``,不入 stack。

    與 :meth:`Tracer._fire_span_start` 同義,但獨立呼叫以避免 mutate thread-local
    stack(asyncio task 共用 thread 時 stack 會錯亂)。任何 processor 例外吃下。
    """
    for proc in tracer.processors:
        try:
            proc.on_span_start(span)
        except Exception:
            logger.exception("processor on_span_start raised; swallowed")


def _fire_span_end(tracer: Tracer, span: Span) -> None:
    """直接走 tracer 註冊的 processors 發 ``on_span_end``,不入 stack。"""
    for proc in tracer.processors:
        try:
            proc.on_span_end(span)
        except Exception:
            logger.exception("processor on_span_end raised; swallowed")
