"""Tool-layer guardrails — 對「tool 被 call 前/後」的檢查機制。

本模組對應 enhancement roadmap P0-6 的 tool 部分,用來在 tool 真正執行前驗證 args、
或在 tool 回傳結果給 LLM 之前驗證 result。

設計參考(對齊 openai-agents `src/agents/tool_guardrails.py`):

* `ToolGuardrailBehavior` enum — 三種處置方式:`ALLOW` / `BLOCK` / `REPLACE_CONTENT`。
* `ToolGuardrailResult` — 檢查結果,包含 behavior、output_info、與 replacement_content。
* `ToolInputGuardrail` — 對 tool args 做檢查。
* `ToolOutputGuardrail` — 對 tool 回傳 result 做檢查。
* `@tool_input_guardrail` / `@tool_output_guardrail` decorator。

三種行為的語意:

- `ALLOW`(預設)— 通過,繼續 tool 執行 / 回傳原 output。
- `BLOCK` — 視同 tripwire,raise `GuardrailTripwireTriggered`,當前 turn 中止。
- `REPLACE_CONTENT` — 不執行 tool / 不回原 output,改把 `replacement_content`
  字串送回 LLM(例如 sanitized 版本、"redacted" 訊息)。

`GuardrailTripwireTriggered` 沿用 `anila_agent.core.guardrails`,讓 agent 層
與 tool 層共用同一個 abort 機制。
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Generic, Protocol, TypeVar, overload, runtime_checkable

from anila_agent.core.guardrails import GuardrailTripwireTriggered

TContext = TypeVar("TContext")


# ---------------------------------------------------------------------------
# Behavior enum 與 result 結構
# ---------------------------------------------------------------------------


class ToolGuardrailBehavior(str, Enum):
    """tool guardrail 處置方式。

    * `ALLOW` — 預設,通過繼續執行。
    * `BLOCK` — 拒絕並 raise `GuardrailTripwireTriggered`(tripwire 機制)。
    * `REPLACE_CONTENT` — 不執行 tool(input 階段)或捨棄原 output(output 階段),
      改用 `replacement_content` 字串送回 LLM。
    """

    ALLOW = "allow"
    BLOCK = "block"
    REPLACE_CONTENT = "replace_content"


@dataclass(frozen=True)
class ToolGuardrailResult:
    """tool guardrail 檢查結果。

    Attributes:
        behavior: 處置方式。`BLOCK` 時 `tripwire_triggered` 等同 True;`REPLACE_CONTENT`
            時必須提供 `replacement_content`。
        output_info: 檢查細節(例如命中的違規欄位、長度等),供 log / telemetry。
        replacement_content: `REPLACE_CONTENT` 時送回 LLM 的字串內容。其它 behavior 時忽略。
    """

    behavior: ToolGuardrailBehavior = ToolGuardrailBehavior.ALLOW
    output_info: dict[str, Any] = field(default_factory=dict)
    replacement_content: str | None = None

    @property
    def tripwire_triggered(self) -> bool:
        """`BLOCK` 行為視同 tripwire 觸發,讓 runner 統一以例外處置。"""
        return self.behavior is ToolGuardrailBehavior.BLOCK

    # ---- 便利 factory ----------------------------------------------------

    @classmethod
    def allow(cls, output_info: dict[str, Any] | None = None) -> ToolGuardrailResult:
        """通過。等同直接 `return ToolGuardrailResult()`,但語意更清楚。"""
        return cls(
            behavior=ToolGuardrailBehavior.ALLOW,
            output_info=dict(output_info) if output_info else {},
        )

    @classmethod
    def block(cls, output_info: dict[str, Any] | None = None) -> ToolGuardrailResult:
        """拒絕並觸發 tripwire。呼叫端收到後應 raise `GuardrailTripwireTriggered`。"""
        return cls(
            behavior=ToolGuardrailBehavior.BLOCK,
            output_info=dict(output_info) if output_info else {},
        )

    @classmethod
    def replace_content(
        cls,
        replacement: str,
        output_info: dict[str, Any] | None = None,
    ) -> ToolGuardrailResult:
        """用 `replacement` 字串取代原始 input 執行 / 原 output 回傳。"""
        return cls(
            behavior=ToolGuardrailBehavior.REPLACE_CONTENT,
            output_info=dict(output_info) if output_info else {},
            replacement_content=replacement,
        )


# ---------------------------------------------------------------------------
# Protocol — 給 type checker 與第三方擴充用
# ---------------------------------------------------------------------------


@runtime_checkable
class ToolInputGuardrailProtocol(Protocol):
    """ToolInputGuardrail 的結構協定。"""

    name: str

    def check(
        self,
        ctx: Any,
        tool_name: str,
        args: dict[str, Any],
    ) -> ToolGuardrailResult:
        """對即將執行的 tool 與 args 做檢查。"""
        ...


@runtime_checkable
class ToolOutputGuardrailProtocol(Protocol):
    """ToolOutputGuardrail 的結構協定。"""

    name: str

    def check(
        self,
        ctx: Any,
        tool_name: str,
        result: Any,
    ) -> ToolGuardrailResult:
        """對 tool 回傳結果做檢查。"""
        ...


# ---------------------------------------------------------------------------
# Wrapper dataclass
# ---------------------------------------------------------------------------


_ToolInputCheckFn = Callable[
    [Any, str, dict[str, Any]],
    ToolGuardrailResult | Awaitable[ToolGuardrailResult],
]
_ToolOutputCheckFn = Callable[
    [Any, str, Any],
    ToolGuardrailResult | Awaitable[ToolGuardrailResult],
]


@dataclass
class ToolInputGuardrail(Generic[TContext]):
    """對「tool 被 call 前」執行的檢查 wrapper。

    用途範例:
    - filesystem tool 在 write 前檢查 path 是否在 workspace 內。
    - HTTP tool 在發 request 前檢查 url scheme 是否允許。
    - 任何 destructive tool 都先過 user approval check。

    `check(ctx, tool_name, args)` 回傳 `ToolGuardrailResult`,runner 依 behavior 處理:
    - ALLOW → 繼續 tool 執行。
    - BLOCK → raise `GuardrailTripwireTriggered`。
    - REPLACE_CONTENT → 不執行 tool,把 `replacement_content` 當成 tool result 回給 LLM。
    """

    guardrail_function: _ToolInputCheckFn
    """檢查函式。簽名:`(ctx, tool_name, args) -> ToolGuardrailResult`。"""

    name: str = ""
    """guardrail 名稱,空字串時 fallback 到 fn.__name__。"""

    def __post_init__(self) -> None:
        if not self.name:
            self.name = getattr(
                self.guardrail_function, "__name__", "tool_input_guardrail"
            )

    def check(
        self,
        ctx: TContext | Any,
        tool_name: str,
        args: dict[str, Any],
    ) -> ToolGuardrailResult:
        """同步介面:執行檢查並回傳結果。

        若 `guardrail_function` 為 async,丟 TypeError(請改用 `run`)。
        """
        result = self.guardrail_function(ctx, tool_name, args)
        if inspect.isawaitable(result):
            raise TypeError(
                f"Tool guardrail {self.name!r} is async; use `await guardrail.run(...)` instead."
            )
        return result

    async def run(
        self,
        ctx: TContext | Any,
        tool_name: str,
        args: dict[str, Any],
    ) -> ToolGuardrailResult:
        """非同步介面:同時相容 sync / async `guardrail_function`。"""
        result = self.guardrail_function(ctx, tool_name, args)
        if inspect.isawaitable(result):
            return await result
        return result

    def enforce(
        self,
        ctx: TContext | Any,
        tool_name: str,
        args: dict[str, Any],
    ) -> ToolGuardrailResult:
        """執行檢查;若 behavior 為 `BLOCK` 則直接 raise tripwire。

        runner 預期路徑:呼叫 `enforce`,拿到的 result 必定不是 BLOCK。
        ALLOW / REPLACE_CONTENT 兩種狀態交由 runner 後續處理。
        """
        result = self.check(ctx, tool_name, args)
        if result.behavior is ToolGuardrailBehavior.BLOCK:
            raise GuardrailTripwireTriggered(
                guardrail_name=self.name,
                info={
                    "tool_name": tool_name,
                    "stage": "input",
                    **result.output_info,
                },
            )
        return result


@dataclass
class ToolOutputGuardrail(Generic[TContext]):
    """對「tool 回傳 → LLM」前執行的檢查 wrapper。

    用途範例:
    - filesystem read 後檢查內容是否含 secret(命中就 REPLACE_CONTENT redact)。
    - HTTP tool response 過 size limit 就截斷。
    - destructive 操作回傳的 stdout 過敏感詞就 redact。
    """

    guardrail_function: _ToolOutputCheckFn
    """檢查函式。簽名:`(ctx, tool_name, result) -> ToolGuardrailResult`。"""

    name: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            self.name = getattr(
                self.guardrail_function, "__name__", "tool_output_guardrail"
            )

    def check(
        self,
        ctx: TContext | Any,
        tool_name: str,
        result: Any,
    ) -> ToolGuardrailResult:
        """同步介面:執行檢查並回傳結果。"""
        outcome = self.guardrail_function(ctx, tool_name, result)
        if inspect.isawaitable(outcome):
            raise TypeError(
                f"Tool guardrail {self.name!r} is async; use `await guardrail.run(...)` instead."
            )
        return outcome

    async def run(
        self,
        ctx: TContext | Any,
        tool_name: str,
        result: Any,
    ) -> ToolGuardrailResult:
        """非同步介面:同時相容 sync / async `guardrail_function`。"""
        outcome = self.guardrail_function(ctx, tool_name, result)
        if inspect.isawaitable(outcome):
            return await outcome
        return outcome

    def enforce(
        self,
        ctx: TContext | Any,
        tool_name: str,
        result: Any,
    ) -> ToolGuardrailResult:
        """執行檢查;若 behavior 為 `BLOCK` 則直接 raise tripwire。"""
        outcome = self.check(ctx, tool_name, result)
        if outcome.behavior is ToolGuardrailBehavior.BLOCK:
            raise GuardrailTripwireTriggered(
                guardrail_name=self.name,
                info={
                    "tool_name": tool_name,
                    "stage": "output",
                    **outcome.output_info,
                },
            )
        return outcome


# ---------------------------------------------------------------------------
# Decorators
# ---------------------------------------------------------------------------


@overload
def tool_input_guardrail(func: _ToolInputCheckFn) -> ToolInputGuardrail[Any]: ...


@overload
def tool_input_guardrail(
    *, name: str | None = None
) -> Callable[[_ToolInputCheckFn], ToolInputGuardrail[Any]]: ...


def tool_input_guardrail(
    func: _ToolInputCheckFn | None = None,
    *,
    name: str | None = None,
) -> ToolInputGuardrail[Any] | Callable[[_ToolInputCheckFn], ToolInputGuardrail[Any]]:
    """把 callable 包成 `ToolInputGuardrail` 的裝飾器。

    用法::

        @tool_input_guardrail(name="workspace_only")
        def _check(ctx, tool_name, args):
            if not str(args.get("path", "")).startswith("/workspace"):
                return ToolGuardrailResult.block({"path": args.get("path")})
            return ToolGuardrailResult.allow()
    """

    def decorator(fn: _ToolInputCheckFn) -> ToolInputGuardrail[Any]:
        return ToolInputGuardrail(guardrail_function=fn, name=name or "")

    if func is not None:
        return decorator(func)
    return decorator


@overload
def tool_output_guardrail(func: _ToolOutputCheckFn) -> ToolOutputGuardrail[Any]: ...


@overload
def tool_output_guardrail(
    *, name: str | None = None
) -> Callable[[_ToolOutputCheckFn], ToolOutputGuardrail[Any]]: ...


def tool_output_guardrail(
    func: _ToolOutputCheckFn | None = None,
    *,
    name: str | None = None,
) -> ToolOutputGuardrail[Any] | Callable[[_ToolOutputCheckFn], ToolOutputGuardrail[Any]]:
    """把 callable 包成 `ToolOutputGuardrail` 的裝飾器。用法與 `tool_input_guardrail` 對稱。"""

    def decorator(fn: _ToolOutputCheckFn) -> ToolOutputGuardrail[Any]:
        return ToolOutputGuardrail(guardrail_function=fn, name=name or "")

    if func is not None:
        return decorator(func)
    return decorator


__all__ = [
    "ToolGuardrailBehavior",
    "ToolGuardrailResult",
    "ToolInputGuardrail",
    "ToolInputGuardrailProtocol",
    "ToolOutputGuardrail",
    "ToolOutputGuardrailProtocol",
    "tool_input_guardrail",
    "tool_output_guardrail",
]
