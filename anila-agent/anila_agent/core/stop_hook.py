"""P1-14 Stop hook prevent-continuation。

本模組對應 enhancement roadmap **§4.10** — 把 claude-code-src
``src/query/stopHooks.ts`` 的 ``preventContinuation`` super power
搬進 anila-agent。

語義
====

* Agent 跑完一輪 turn,正準備把 final output 回給 user 之前,先 fire **stop hook chain**。
* 任一 stop hook 回 ``StopHookDecision(prevent_continuation=True)`` →
  runner 視為「該輪沒完成」,把 ``inject_message`` (若有) 塞回去 LLM,繼續下一輪。
* 全部 stop hook 都回 allow (預設 ``prevent_continuation=False``) → runner 正常結束,
  yield ``final_output``。

這與 P0-1 既有的 ``HookEvent.STOP`` (純 observation,只收 ``StopInput`` payload)是
**不同層次的機制**:

* ``HookEvent.STOP`` 走 ``fire(...)`` 聚合 ``HookOutput`` — 用於記錄、收 context。
* ``StopHook`` 走 ``fire_stop_hooks(...)`` — 用於改變主 loop 是否結束的決定。

兩者並存,順序為:`HookEvent.STOP` 先 fire (observation),再 `fire_stop_hooks`
(decision)。runner 應在 `fire_stop_hooks` 回 prevent 時跳過 yield final_output。

設計重點
========

* **Chain 早停**:任一 stop hook 回 prevent → 立刻 return,後續 hook 不執行
  (對齊 ``fire(...)`` 在 ``block`` / ``abort`` 時的 early-exit 行為)。
* **inject_message 可選**:hook 可附帶一條 system message,給 LLM 重 prompt 用
  (例如 ``"請補上 citation"``);若不附,runner 用 ``reason`` 當 fallback。
* **內建三個範例 hook**:`MaxIterationsStopHook` / `OutputLengthStopHook` /
  `KeywordStopHook` — 各自代表「迴圈防護」、「output 太短」、「output 缺 keyword」三類
  常見場景。
"""

from __future__ import annotations

import inspect
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Sequence
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# StopHookDecision dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StopHookDecision:
    """Stop hook 對「是否繼續跑下一輪」的判斷結果。

    Attributes:
        prevent_continuation: True 代表「不准結束」,runner 應繼續跑下一輪 LLM。
            False (預設) 代表 allow,runner 可以 yield final_output。
        reason: 人類可讀的原因字串,給 logging / debug 用。
        inject_message: 若 ``prevent_continuation=True``,runner 可以把這條 message
            當 system reminder 塞回去 LLM (例如 ``"請完成 X"``);若為 None,
            runner 退回去用 ``reason`` 當訊息文字。
    """

    prevent_continuation: bool = False
    reason: str | None = None
    inject_message: str | None = None

    @classmethod
    def allow(cls) -> "StopHookDecision":
        """便利 constructor — 「同意結束」。等同於不帶參數的 default。"""
        return cls()

    @classmethod
    def prevent(
        cls, reason: str, *, inject_message: str | None = None
    ) -> "StopHookDecision":
        """便利 constructor — 「阻止結束」,要求繼續下一輪。"""
        return cls(
            prevent_continuation=True,
            reason=reason,
            inject_message=inject_message,
        )


# ---------------------------------------------------------------------------
# StopHook ABC
# ---------------------------------------------------------------------------


class StopHook(ABC):
    """Stop hook 抽象基底 — subclass 實作 ``check(ctx, agent_output)`` 回 `StopHookDecision`。

    支援 sync 與 async ``check``:`fire_stop_hooks` 用 ``inspect.isawaitable``
    判斷後 await。設計上希望 hook 邏輯保持輕量 (純 Python 判斷),若需要 I/O
    (打 HTTP / 跑 subprocess),建議用 async 並把 timeout 自行管好,避免拖累
    整個 stream loop。

    Note:
        ``ctx`` 型別刻意保留為 ``Any`` — 呼叫端 (runner / coordinator) 自行決定
        要把什麼 metadata 塞進去。實務上會是 ``StopHookContext`` 之類的 dict /
        dataclass,帶 ``iteration`` / ``messages`` / ``session_id`` 等資訊。
    """

    @abstractmethod
    def check(
        self, ctx: Any, agent_output: Any
    ) -> "StopHookDecision | Awaitable[StopHookDecision]":
        """檢查當前 agent_output,回 `StopHookDecision`。

        Args:
            ctx: 由呼叫端提供的執行 context (iteration 計數、session metadata 等)。
            agent_output: 本輪 agent 產生的 output (通常是 str,但也可能是 dict / dataclass)。

        Returns:
            `StopHookDecision`:`prevent_continuation=True` 阻止結束;否則 allow。
        """


# Callable 形式的 stop hook — 寫測試 / lambda 時直接傳 function,不必 subclass。
StopHookCallable = Callable[
    [Any, Any], "StopHookDecision | Awaitable[StopHookDecision]"
]

# Chain entry 可以是 ABC instance 或 callable,讓 register API 兩種都吃。
StopHookEntry = StopHook | StopHookCallable


# ---------------------------------------------------------------------------
# 內建 stop hook 範例
# ---------------------------------------------------------------------------


class MaxIterationsStopHook(StopHook):
    """防止無限迴圈 — 同一個 agent run 跑超過 ``max_iterations`` 輪就強制 allow。

    語義:這是「上限保險」,不是「下限要求」;當 iteration > max 時 hook 回
    ``prevent_continuation=False`` (allow 結束),確保 runner 不會無限延長。
    當 iteration <= max 時 hook **不主動 prevent**,只是讓其他 stop hook 自行
    決定 — 換句話說它扮演「強制終止」的角色,而不是「要求繼續」。

    ctx 約定:呼叫端在 ctx 上提供 ``iteration: int`` 屬性 (或 dict key)。
    若兩者都沒有,當作 0 處理。
    """

    def __init__(self, max_iterations: int = 10) -> None:
        if max_iterations < 1:
            raise ValueError(
                f"max_iterations must be >= 1, got {max_iterations}"
            )
        self._max = max_iterations

    @property
    def max_iterations(self) -> int:
        return self._max

    def check(self, ctx: Any, agent_output: Any) -> StopHookDecision:
        iteration = _extract_iteration(ctx)
        if iteration >= self._max:
            # 已達上限 — 強制允許結束 (allow),即使其他 hook 想 prevent
            # 也只能在自己的 check 中表達;chain early-exit 不會發生在此處。
            return StopHookDecision.allow()
        # 未達上限 — 本 hook 不發表意見 (allow),讓其他 hook 決定。
        return StopHookDecision.allow()


class OutputLengthStopHook(StopHook):
    """要求 final output 至少有 ``min_chars`` 字元;太短就 prevent。

    Args:
        min_chars: output 必須達到的最小字元數。預設 50。
        inject_message: 若 prevent,塞回 LLM 的 system reminder;預設提示完成輸出。

    注意:此 hook 只看 ``str(agent_output)`` 的長度,不對 dict / dataclass 做特別處理。
    若 ``agent_output`` 為 None,視為 0 字元。
    """

    def __init__(
        self,
        min_chars: int = 50,
        *,
        inject_message: str | None = None,
    ) -> None:
        if min_chars < 0:
            raise ValueError(f"min_chars must be >= 0, got {min_chars}")
        self._min = min_chars
        self._inject = (
            inject_message
            or f"請補完輸出,內容應達到至少 {min_chars} 字元再結束。"
        )

    @property
    def min_chars(self) -> int:
        return self._min

    def check(self, ctx: Any, agent_output: Any) -> StopHookDecision:
        text = "" if agent_output is None else str(agent_output)
        if len(text) < self._min:
            return StopHookDecision.prevent(
                reason=(
                    f"output length {len(text)} < min_chars {self._min}"
                ),
                inject_message=self._inject,
            )
        return StopHookDecision.allow()


class KeywordStopHook(StopHook):
    """要求 output 必須包含特定 keyword;缺任一就 prevent。

    Args:
        required: 必須全數出現的 keyword list。空 list 視為「不要求」,永遠 allow。
        case_sensitive: 是否區分大小寫,預設 False (case-insensitive)。
        inject_message: 若 prevent,塞回 LLM 的 system reminder;預設列出缺漏的 keyword。
    """

    def __init__(
        self,
        required: Sequence[str],
        *,
        case_sensitive: bool = False,
        inject_message: str | None = None,
    ) -> None:
        self._required: tuple[str, ...] = tuple(required)
        self._case_sensitive = case_sensitive
        self._inject_template = inject_message

    @property
    def required(self) -> tuple[str, ...]:
        return self._required

    def check(self, ctx: Any, agent_output: Any) -> StopHookDecision:
        if not self._required:
            return StopHookDecision.allow()
        text = "" if agent_output is None else str(agent_output)
        haystack = text if self._case_sensitive else text.lower()
        missing: list[str] = []
        for keyword in self._required:
            needle = keyword if self._case_sensitive else keyword.lower()
            if needle not in haystack:
                missing.append(keyword)
        if not missing:
            return StopHookDecision.allow()
        inject = self._inject_template or (
            f"輸出缺少必要關鍵字 {missing},請補上後再結束。"
        )
        return StopHookDecision.prevent(
            reason=f"missing required keywords: {missing}",
            inject_message=inject,
        )


# ---------------------------------------------------------------------------
# Chain runner — 給 RunHooks.fire_stop_hooks 使用
# ---------------------------------------------------------------------------


async def fire_stop_hook_chain(
    hooks: Sequence[StopHookEntry],
    ctx: Any,
    agent_output: Any,
) -> StopHookDecision:
    """跑整個 stop hook chain,任一 prevent 就 early exit。

    Args:
        hooks: 註冊順序的 stop hook list (`StopHook` instance 或 callable)。
        ctx: 帶給每個 hook 的執行 context。
        agent_output: 本輪 agent 的 output。

    Returns:
        若任一 hook 回 ``prevent_continuation=True``,return 該 decision (chain
        early exit);全部過則 return ``StopHookDecision.allow()``。
    """
    for entry in hooks:
        decision = await _invoke_stop_hook(entry, ctx, agent_output)
        if decision.prevent_continuation:
            return decision
    return StopHookDecision.allow()


async def _invoke_stop_hook(
    entry: StopHookEntry, ctx: Any, agent_output: Any
) -> StopHookDecision:
    """執行單一 stop hook(ABC instance 或 callable),回 ``StopHookDecision``。

    若 callback 回 awaitable 會 await;若回的不是 ``StopHookDecision`` 會 raise
    ``TypeError`` — 避免 silent failure。
    """
    if isinstance(entry, StopHook):
        result: Any = entry.check(ctx, agent_output)
    else:
        result = entry(ctx, agent_output)
    if inspect.isawaitable(result):
        result = await result
    if not isinstance(result, StopHookDecision):
        raise TypeError(
            f"stop hook {getattr(entry, '__qualname__', entry)} returned "
            f"{type(result).__name__}, expected StopHookDecision"
        )
    return result


# ---------------------------------------------------------------------------
# Utils
# ---------------------------------------------------------------------------


def _extract_iteration(ctx: Any) -> int:
    """從 ctx 取 ``iteration``;支援 attribute / dict / 缺失 三種情境。

    順序:
        1. ``ctx.iteration`` (attribute)
        2. ``ctx["iteration"]`` (dict / Mapping)
        3. 0 (fallback)
    """
    if ctx is None:
        return 0
    val = getattr(ctx, "iteration", None)
    if val is not None:
        try:
            return int(val)
        except (TypeError, ValueError):
            return 0
    if isinstance(ctx, dict):
        raw = ctx.get("iteration", 0)
        try:
            return int(raw)
        except (TypeError, ValueError):
            return 0
    return 0


__all__ = [
    "KeywordStopHook",
    "MaxIterationsStopHook",
    "OutputLengthStopHook",
    "StopHook",
    "StopHookCallable",
    "StopHookDecision",
    "StopHookEntry",
    "fire_stop_hook_chain",
]
