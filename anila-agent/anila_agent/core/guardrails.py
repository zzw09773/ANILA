"""Guardrails framework — agent 層的 input / output 安全檢查機制。

本模組對應 enhancement roadmap P0-6:在 user input 進 agent 之前、與 agent 最終回應
之前各加一層可組合的檢查函式,用來擋掉長度爆掉、敏感詞、prompt injection、洩漏 secret、
hallucination 等問題。

設計參考(對齊 openai-agents `src/agents/guardrail.py`):

* `GuardrailResult` — 每次檢查回傳的結構,包含 tripwire 旗標與 output_info 細節。
* `GuardrailTripwireTriggered` — tripwire 觸發時 runner 應 raise 此例外以 abort current turn。
* `InputGuardrail` — 對「user input → agent」做檢查的 wrapper。
* `OutputGuardrail` — 對「agent → user」最終 output 做檢查的 wrapper。
* `@input_guardrail` / `@output_guardrail` decorator — 把任一 callable 包成上述 wrapper。

本檔故意不依賴 openai-agents 套件,只用 std lib + typing,以便:
1. 單獨單元測試(無需起 LLM)。
2. tool 層 (`anila_agent/tools/guardrails.py`) 可獨立沿用同一 base。
3. 後續 P0-7 Policy DSL 在這層之上再做組合。

P0-6 與後續 task 的分工:
- P0-6(本檔):提供「會被執行的單一檢查函式」抽象。
- P0-7:Policy DSL → 把多個 guardrail 用 allow / deny / require_approval 規則組起來。
- P0-8:在 runner 對接呼叫順序、收集 GuardrailResult 上報 telemetry。
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Generic, Protocol, TypeVar, overload, runtime_checkable

# 共用 context 型別 — 與 openai-agents 的 RunContextWrapper 對齊,但本層不強制,
# 讓使用端可以用 AnilaToolContext 或自家 ctx 結構直接餵進來。
TContext = TypeVar("TContext")


# ---------------------------------------------------------------------------
# 共用結構:GuardrailResult / GuardrailTripwireTriggered
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GuardrailResult:
    """單次 guardrail 檢查的結果。

    Attributes:
        tripwire_triggered: 是否觸發 tripwire(命中拒絕條件)。`True` 時 runner 必須中止
            當前 turn 並 raise `GuardrailTripwireTriggered`。
        output_info: 檢查過程的細節資料(例如違規長度、命中的敏感詞 list),供 log /
            telemetry / 上報 user 顯示。任意 dict 結構,呼叫端自行定義 schema。
    """

    tripwire_triggered: bool
    output_info: dict[str, Any] = field(default_factory=dict)


class GuardrailTripwireTriggered(Exception):
    """guardrail 觸發 tripwire 時 runner 應 raise 此例外。

    本例外攜帶:
    - `guardrail_name`:哪一條 guardrail 觸發(供 log / debug)。
    - `info`:對應 `GuardrailResult.output_info`,給 user 看的細節。
    - `triggered_at`:觸發時間(UTC ISO8601 字串),供事後 trace。

    例外訊息預設為:`Guardrail '<name>' tripwire triggered`,呼叫端可直接 str(e) 上報。
    """

    def __init__(
        self,
        guardrail_name: str,
        info: dict[str, Any] | None = None,
    ) -> None:
        """建立 tripwire 例外。

        Args:
            guardrail_name: 觸發的 guardrail 名稱(對應 decorator 的 `name` 或 fn.__name__)。
            info: `GuardrailResult.output_info` 的內容,可為 None。
        """
        self.guardrail_name = guardrail_name
        self.info: dict[str, Any] = dict(info) if info else {}
        self.triggered_at: str = datetime.now(timezone.utc).isoformat()
        super().__init__(
            f"Guardrail {guardrail_name!r} tripwire triggered at {self.triggered_at}"
        )


# ---------------------------------------------------------------------------
# Protocol — 給 type checker 與第三方擴充用
# ---------------------------------------------------------------------------


@runtime_checkable
class InputGuardrailProtocol(Protocol):
    """InputGuardrail 的結構協定。

    任何具備 `name` 屬性與 `check(ctx, user_input)` 方法的物件都可當 InputGuardrail 使用。
    """

    name: str

    def check(self, ctx: Any, user_input: str) -> GuardrailResult:
        """執行檢查並回傳結果。"""
        ...


@runtime_checkable
class OutputGuardrailProtocol(Protocol):
    """OutputGuardrail 的結構協定。"""

    name: str

    def check(self, ctx: Any, output: Any) -> GuardrailResult:
        """執行檢查並回傳結果。"""
        ...


# ---------------------------------------------------------------------------
# InputGuardrail / OutputGuardrail wrapper 類別
# ---------------------------------------------------------------------------


# 對齊 openai-agents:同時接受 sync 與 async callable。
_InputCheckFn = Callable[[Any, str], GuardrailResult | Awaitable[GuardrailResult]]
_OutputCheckFn = Callable[[Any, Any], GuardrailResult | Awaitable[GuardrailResult]]


@dataclass
class InputGuardrail(Generic[TContext]):
    """對「user input 進 agent 前」執行的檢查 wrapper。

    用途範例:
    - 長度限制(例如 user_input > 10_000 字就擋)。
    - 敏感詞 / 個資偵測。
    - prompt injection pattern 偵測(例如 "Ignore all previous instructions")。

    `check(ctx, user_input)` 會呼叫 `guardrail_function`,回傳 `GuardrailResult`。
    若 `tripwire_triggered=True`,呼叫端應 raise `GuardrailTripwireTriggered`。

    本 dataclass 故意保留 sync API(不 async),因為大部分 input 檢查都是純字串處理;
    需要 async(例如打外部 moderation API)時可改用 `run` 處理 awaitable。
    """

    guardrail_function: _InputCheckFn
    """實際執行檢查的 callable。簽名:`(ctx, user_input) -> GuardrailResult`。"""

    name: str = ""
    """guardrail 名稱,空字串時 fallback 到 fn.__name__。"""

    def __post_init__(self) -> None:
        # name 為空字串時 fallback 用 function 名稱,方便 log / trace。
        if not self.name:
            self.name = getattr(self.guardrail_function, "__name__", "input_guardrail")

    def check(self, ctx: TContext | Any, user_input: str) -> GuardrailResult:
        """同步介面:執行檢查並回傳結果。

        若 `guardrail_function` 是 async,本方法會丟 TypeError(請改用 `run`)。
        這個分離設計是為了讓多數同步檢查不需要進 event loop。
        """
        result = self.guardrail_function(ctx, user_input)
        if inspect.isawaitable(result):
            raise TypeError(
                f"Guardrail {self.name!r} is async; use `await guardrail.run(...)` instead."
            )
        return result

    async def run(self, ctx: TContext | Any, user_input: str) -> GuardrailResult:
        """非同步介面:同時相容 sync / async `guardrail_function`。"""
        result = self.guardrail_function(ctx, user_input)
        if inspect.isawaitable(result):
            return await result
        return result


@dataclass
class OutputGuardrail(Generic[TContext]):
    """對「agent 最終回應 user 前」執行的檢查 wrapper。

    用途範例:
    - 防止洩漏 secret(API key / PII 在 output 出現)。
    - hallucination 簡單偵測(例如 output 內含「I'm not sure but ...」即拒)。
    - 內容審查(NSFW / 違法內容)。

    `check(ctx, output)` 呼叫 `guardrail_function`,回傳 `GuardrailResult`。
    `output` 為 agent 最終產生的物件(通常是 str,但保留 Any 以支援結構化 output)。
    """

    guardrail_function: _OutputCheckFn
    """實際執行檢查的 callable。簽名:`(ctx, output) -> GuardrailResult`。"""

    name: str = ""
    """guardrail 名稱,空字串時 fallback 到 fn.__name__。"""

    def __post_init__(self) -> None:
        if not self.name:
            self.name = getattr(self.guardrail_function, "__name__", "output_guardrail")

    def check(self, ctx: TContext | Any, output: Any) -> GuardrailResult:
        """同步介面:執行檢查並回傳結果。"""
        result = self.guardrail_function(ctx, output)
        if inspect.isawaitable(result):
            raise TypeError(
                f"Guardrail {self.name!r} is async; use `await guardrail.run(...)` instead."
            )
        return result

    async def run(self, ctx: TContext | Any, output: Any) -> GuardrailResult:
        """非同步介面:同時相容 sync / async `guardrail_function`。"""
        result = self.guardrail_function(ctx, output)
        if inspect.isawaitable(result):
            return await result
        return result


# ---------------------------------------------------------------------------
# Decorators:@input_guardrail / @output_guardrail
# ---------------------------------------------------------------------------


@overload
def input_guardrail(func: _InputCheckFn) -> InputGuardrail[Any]: ...


@overload
def input_guardrail(
    *, name: str | None = None
) -> Callable[[_InputCheckFn], InputGuardrail[Any]]: ...


def input_guardrail(
    func: _InputCheckFn | None = None,
    *,
    name: str | None = None,
) -> InputGuardrail[Any] | Callable[[_InputCheckFn], InputGuardrail[Any]]:
    """把 callable 包成 `InputGuardrail` 的裝飾器。

    支援兩種寫法:

    無括號形式::

        @input_guardrail
        def too_long(ctx, user_input):
            if len(user_input) > 10000:
                return GuardrailResult(
                    tripwire_triggered=True,
                    output_info={"length": len(user_input)},
                )
            return GuardrailResult(tripwire_triggered=False)

    keyword 形式::

        @input_guardrail(name="length_check")
        def _check(ctx, user_input):
            ...

    Args:
        func: 直接傳入時走無括號形式。
        name: keyword 形式時設定 guardrail 名稱;預設為 fn.__name__。
    """

    def decorator(fn: _InputCheckFn) -> InputGuardrail[Any]:
        # name 留空字串時 InputGuardrail.__post_init__ 會 fallback 到 fn.__name__。
        return InputGuardrail(guardrail_function=fn, name=name or "")

    if func is not None:
        return decorator(func)
    return decorator


@overload
def output_guardrail(func: _OutputCheckFn) -> OutputGuardrail[Any]: ...


@overload
def output_guardrail(
    *, name: str | None = None
) -> Callable[[_OutputCheckFn], OutputGuardrail[Any]]: ...


def output_guardrail(
    func: _OutputCheckFn | None = None,
    *,
    name: str | None = None,
) -> OutputGuardrail[Any] | Callable[[_OutputCheckFn], OutputGuardrail[Any]]:
    """把 callable 包成 `OutputGuardrail` 的裝飾器。

    用法與 `@input_guardrail` 對稱。
    """

    def decorator(fn: _OutputCheckFn) -> OutputGuardrail[Any]:
        return OutputGuardrail(guardrail_function=fn, name=name or "")

    if func is not None:
        return decorator(func)
    return decorator


__all__ = [
    "GuardrailResult",
    "GuardrailTripwireTriggered",
    "InputGuardrail",
    "InputGuardrailProtocol",
    "OutputGuardrail",
    "OutputGuardrailProtocol",
    "input_guardrail",
    "output_guardrail",
]
