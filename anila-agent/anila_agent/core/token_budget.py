"""Token budget continuation — 對話 token 用量追蹤、軟/硬上限攔截、PTL retry。

本模組對應 enhancement roadmap §4.6 / P1-9 任務(quick win),把上游
``claude-code-src/src/query/tokenBudget.ts`` 的概念 port 成 Python,並擴成
一個獨立可重用的 budget 子系統:

* **BudgetTracker** — 純資料 + 累加,跑長對話時逐輪 record(prompt + completion)
  token,提供 used / remaining / usage_ratio 三個 view,以及 soft / hard
  threshold 判斷。
* **BudgetExceeded** — hard threshold 觸發時 raise,讓 caller(runner / agent
  迴圈)走「對話過長,請 reset」分支。
* **continuation_strategy** — 概念層 hook,soft_exceeded 給 caller 一個機會做
  compaction(P1-6 CompactingSession 的銜接點),本 task 不直接依賴 P1-6,只
  expose callback。
* **@with_budget(tracker, ...)** — async LLM call function 的 decorator,跑完
  之後從 response.usage 自動 record,並判斷是否該 fire soft / hard callback /
  raise。
* **PTLRetry** — PTL(prompt too long)retry helper:catch LLM 「context too
  long」class 的 error,呼叫 user-提供的 truncate callback,retry 最多 max 次,
  最後一輪仍失敗就把原 error re-raise 出去,**不**幫 caller 默默吃掉。

設計重點
========

1. **零外部相依**:只用 std lib,**不引** openai / anthropic SDK。
   `with_budget` 從 response 取 token 數的方式以 duck-typing 取:
   - 先讀 ``response.usage`` 物件(SDK 慣例,有 `prompt_tokens` / `completion_tokens`)
   - 不存在則讀 ``response["usage"]`` dict 形態
   - 都不存在則跳過 record(但 callback 還是會根據既有 used 觸發)

2. **同步 + 非同步雙模式**:`with_budget` 同時支援 sync function 與 async
   coroutine function,因為 runner 端 LLM call 在 ANILA 既有環境兩種都見得到
   (legacy sync chat client + 新的 async streaming)。

3. **不 mutate response**:decorator 只 *讀* response,不附加 / 修改任何欄位,
   避免下游 parser 因型別改變而炸。

4. **callback 可選**:soft / hard callback 都允許 None,沒給就只在 tracker 上
   留狀態,讓 caller 自行查 ``tracker.is_hard_exceeded()`` 後行動。

5. **threading**:本 module 不主動加 lock。BudgetTracker 預期在「單一對話迴圈」
   sequence 使用,並發 record 不在這 task scope 內(若未來要支援 multi-turn
   並行 stream,再加 ``threading.Lock``)。

跟 claude-code TS 原版的差異
============================

* TS 版的 ``continuationCount`` / ``diminishingReturns`` 是專做「自動把 user 戳
  一句『請繼續』」的子問題,本 module 不直接搬,保留 **threshold 抽象** 給上層
  runner 自己決定要 nudge 還是 stop。
* TS 版只記 ``globalTurnTokens`` 累積值;這裡採「prompt + completion delta」
  累加,語意上更貼近 OpenAI / Anthropic SDK 回傳的 usage shape,易接 LLM
  client。
"""

from __future__ import annotations

import asyncio
import functools
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar

# ---------------------------------------------------------------------------
# 例外
# ---------------------------------------------------------------------------


class BudgetExceeded(Exception):
    """Hard threshold 被打破時 raise。

    `used`:當下累計 token 數。
    `limit`:當初設定的 max_tokens(=hard threshold)。

    runner / caller 接到後通常會把 conversation 標為「需 reset」並回
    user-friendly message,而不是繼續硬丟給 LLM(必定超 context window)。
    """

    def __init__(self, used: int, limit: int, message: str | None = None) -> None:
        self.used = used
        self.limit = limit
        super().__init__(
            message
            or f"Token budget exceeded: used={used} >= limit={limit}"
        )


# ---------------------------------------------------------------------------
# Budget tracker
# ---------------------------------------------------------------------------


@dataclass
class BudgetTracker:
    """逐輪累加 prompt + completion tokens,並提供 soft / hard 兩道 threshold。

    Attributes
    ----------
    max_tokens:
        Hard threshold;`used >= max_tokens` 視為 hard exceeded,該 raise。
        必須 > 0,否則建構時就 ValueError(避免 silently 跳過保護)。
    soft_threshold_ratio:
        Soft threshold 佔 max_tokens 的比例;預設 0.8 表示用量達 80% 觸發
        soft exceeded(警告 / 提示 compaction,但繼續跑)。
        必須在 (0, 1] 區間內,否則 ValueError。
    """

    max_tokens: int
    soft_threshold_ratio: float = 0.8

    # 不對外公開的累加欄位 — 用 dataclass field default,測試可 reset 後再寫。
    _prompt_tokens: int = 0
    _completion_tokens: int = 0
    _records: int = 0

    def __post_init__(self) -> None:
        if self.max_tokens <= 0:
            raise ValueError(
                f"max_tokens must be > 0, got {self.max_tokens}"
            )
        if not 0.0 < self.soft_threshold_ratio <= 1.0:
            raise ValueError(
                "soft_threshold_ratio must be in (0, 1], "
                f"got {self.soft_threshold_ratio}"
            )

    # ------------------------------------------------------------------ record

    def record(self, prompt_tokens: int, completion_tokens: int) -> None:
        """記錄一次 LLM call 的 token 用量。

        負數一律 reject(LLM provider 不該回負數,出現代表 caller 傳錯)。
        """
        if prompt_tokens < 0 or completion_tokens < 0:
            raise ValueError(
                "token counts must be >= 0, got "
                f"prompt_tokens={prompt_tokens}, "
                f"completion_tokens={completion_tokens}"
            )
        self._prompt_tokens += prompt_tokens
        self._completion_tokens += completion_tokens
        self._records += 1

    # -------------------------------------------------------------- views/info

    @property
    def used(self) -> int:
        """已用 tokens(prompt + completion 累計)。"""
        return self._prompt_tokens + self._completion_tokens

    @property
    def prompt_tokens(self) -> int:
        """累計 prompt(input)tokens。"""
        return self._prompt_tokens

    @property
    def completion_tokens(self) -> int:
        """累計 completion(output)tokens。"""
        return self._completion_tokens

    @property
    def remaining(self) -> int:
        """剩餘 tokens;若已超過 max,回 0(不回負數,簡化下游邏輯)。"""
        return max(self.max_tokens - self.used, 0)

    @property
    def usage_ratio(self) -> float:
        """`used / max_tokens` 比例;可超過 1.0(代表已破 hard threshold)。"""
        return self.used / self.max_tokens

    @property
    def records(self) -> int:
        """已 record 的輪數。"""
        return self._records

    # ----------------------------------------------------------- threshold api

    def soft_threshold(self) -> int:
        """Soft threshold 對應的 tokens 絕對值(向下取整)。"""
        return int(self.max_tokens * self.soft_threshold_ratio)

    def is_soft_exceeded(self) -> bool:
        """`used >= soft_threshold` 視為 soft exceeded。"""
        return self.used >= self.soft_threshold()

    def is_hard_exceeded(self) -> bool:
        """`used >= max_tokens` 視為 hard exceeded;caller 該 raise / stop。"""
        return self.used >= self.max_tokens

    # ----------------------------------------------------------------- mutate

    def reset(self) -> None:
        """歸零所有累計欄位,但保留 max / ratio 設定。"""
        self._prompt_tokens = 0
        self._completion_tokens = 0
        self._records = 0


# ---------------------------------------------------------------------------
# Continuation strategy(soft / hard callback container)
# ---------------------------------------------------------------------------


SoftCallback = Callable[[BudgetTracker], None]
"""Soft threshold 達到時呼叫;接 tracker 自身。常見用途:trigger compaction。"""

HardCallback = Callable[[BudgetTracker], None]
"""Hard threshold 達到時呼叫(在 raise BudgetExceeded 之前)。用於記 log / 通知 user。"""


@dataclass
class ContinuationStrategy:
    """soft / hard threshold 觸發行為的 declarative 容器。

    給 `with_budget` decorator 用:
    * `on_soft`:第一次 `is_soft_exceeded()` 翻 True 時呼叫一次。
    * `on_hard`:`is_hard_exceeded()` 翻 True 時呼叫一次,接著 raise
      `BudgetExceeded`(除非 `raise_on_hard=False`)。
    * `raise_on_hard`:是否在 hard 觸發後 raise。預設 True;設 False 可
      讓 caller 拿 callback 自處理(例如改打 compaction 然後 retry)。
    """

    on_soft: SoftCallback | None = None
    on_hard: HardCallback | None = None
    raise_on_hard: bool = True


# ---------------------------------------------------------------------------
# Decorator
# ---------------------------------------------------------------------------


F = TypeVar("F", bound=Callable[..., Any])


def _extract_usage(response: Any) -> tuple[int, int] | None:
    """從 response 物件取 (prompt_tokens, completion_tokens)。

    支援三種形態:
    * `response.usage.prompt_tokens` / `.completion_tokens`(OpenAI / Anthropic SDK 物件)
    * `response.usage` 是 dict
    * `response` 本身是 dict,有 ``"usage"`` key

    任一形態解析失敗(欄位 missing / 型別不對)就回 None,decorator 不 record
    但 callback / threshold 判斷照走(用既有 used)。
    """
    usage = None
    if hasattr(response, "usage"):
        usage = response.usage
    elif isinstance(response, dict) and "usage" in response:
        usage = response["usage"]
    else:
        return None

    def _get(field: str) -> int | None:
        # 物件形態
        if hasattr(usage, field):
            value = getattr(usage, field)
            if isinstance(value, int):
                return value
        # dict 形態
        if isinstance(usage, dict) and field in usage:
            value = usage[field]
            if isinstance(value, int):
                return value
        return None

    prompt = _get("prompt_tokens")
    completion = _get("completion_tokens")
    if prompt is None or completion is None:
        return None
    return prompt, completion


def _apply_thresholds(
    tracker: BudgetTracker,
    strategy: ContinuationStrategy,
    soft_already_fired: dict[str, bool],
    hard_already_fired: dict[str, bool],
) -> None:
    """共用 threshold 判斷邏輯(sync / async 兩條路都用)。

    用 dict 包 bool 是因為 closure 改 bool literal 在 inner function 中要 nonlocal,
    包 dict 後直接 mutate 屬性即可,跨 sync/async wrapper 共用更乾淨。
    """
    if tracker.is_soft_exceeded() and not soft_already_fired["fired"]:
        soft_already_fired["fired"] = True
        if strategy.on_soft is not None:
            strategy.on_soft(tracker)

    if tracker.is_hard_exceeded() and not hard_already_fired["fired"]:
        hard_already_fired["fired"] = True
        if strategy.on_hard is not None:
            strategy.on_hard(tracker)
        if strategy.raise_on_hard:
            raise BudgetExceeded(used=tracker.used, limit=tracker.max_tokens)


def with_budget(
    tracker: BudgetTracker,
    strategy: ContinuationStrategy | None = None,
) -> Callable[[F], F]:
    """Decorator:把 sync / async LLM call function 包成自動記 token + 檢 threshold。

    用法
    ----
    ```python
    tracker = BudgetTracker(max_tokens=10_000)
    strategy = ContinuationStrategy(on_soft=lambda t: compactor.run())

    @with_budget(tracker, strategy)
    async def call_llm(prompt: str) -> Any:
        return await client.messages.create(...)
    ```

    LLM call 跑完後 decorator 會:
    1. `_extract_usage(response)` 取 prompt / completion tokens,有就 record。
    2. 若 `is_soft_exceeded()` 首次翻真 → 呼叫 `strategy.on_soft`(若有)。
    3. 若 `is_hard_exceeded()` 首次翻真 → 呼叫 `strategy.on_hard`(若有)、
       再依 `raise_on_hard` 決定 raise `BudgetExceeded`。

    回傳值:**原樣**回傳 LLM call 的 response(decorator 不 wrap / mutate)。

    Notes
    -----
    * 同一 tracker 被多個 wrapper 共用時,soft / hard fired 旗標是「per
      wrapper instance」,不是 per tracker。這設計簡化常見情境(同一個
      runner 對應一個 wrapper);要 cross-call 一次性語意請走 caller-side
      狀態。
    """
    final_strategy = strategy or ContinuationStrategy()

    def decorator(func: F) -> F:
        soft_fired = {"fired": False}
        hard_fired = {"fired": False}

        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                response = await func(*args, **kwargs)
                usage = _extract_usage(response)
                if usage is not None:
                    tracker.record(usage[0], usage[1])
                _apply_thresholds(
                    tracker, final_strategy, soft_fired, hard_fired
                )
                return response

            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            response = func(*args, **kwargs)
            usage = _extract_usage(response)
            if usage is not None:
                tracker.record(usage[0], usage[1])
            _apply_thresholds(tracker, final_strategy, soft_fired, hard_fired)
            return response

        return sync_wrapper  # type: ignore[return-value]

    return decorator


# ---------------------------------------------------------------------------
# PTL retry(prompt too long → truncate → retry)
# ---------------------------------------------------------------------------


PromptT = TypeVar("PromptT")
"""Prompt 型別 — 任意 caller 自選(str / list[message] / dict 都行)。"""

TruncateCallback = Callable[[PromptT, BaseException], PromptT]
"""Truncate callback 簽名:接 (當前 prompt, 觸發的 PTL error) → 縮短後的 prompt。

通常 caller 接 P1-6 CompactingSession.compact() 或自己的 truncate logic。
"""


def _is_ptl_error(
    error: BaseException, patterns: tuple[str, ...]
) -> bool:
    """判斷一個 exception 是不是 PTL(prompt too long)。

    判定走「error 訊息子字串 match」之一即可。預設 patterns 涵蓋常見 LLM
    provider 回的字眼,caller 可在建構 PTLRetry 時自訂。

    用子字串 + lower-case 比對,避免綁死特定 SDK 的 exception class(各家
    provider 不一,且測試環境多用 generic Exception 模擬)。
    """
    msg = str(error).lower()
    return any(p.lower() in msg for p in patterns)


DEFAULT_PTL_PATTERNS: tuple[str, ...] = (
    "context length",
    "context_length",
    "context window",
    "prompt is too long",
    "prompt too long",
    "maximum context",
    "max_tokens",
    "token limit",
    "too many tokens",
)


@dataclass
class PTLRetry:
    """Prompt-too-long retry helper。

    用 `run_sync()` / `run_async()` 把一次 LLM call 包起來;若 call 噴 PTL
    類 error,就把目前 prompt 丟給 `truncate` callback 縮一下,再 retry。

    最多 retry `max_retries` 次(總共最多 ``max_retries + 1`` 次 attempt)。
    全部 attempt 失敗就把**最後一次**的 error re-raise(不吃掉)。

    如果 truncate callback 自身丟出例外,**不** swallow,直接 propagate。

    參數
    ----
    truncate:
        `Callable[[prompt, error], new_prompt]`。負責縮短 prompt;return
        新 prompt 給下一輪 attempt 用。
    max_retries:
        除了「第一次 attempt」外,最多再 retry 幾次。預設 3。
    patterns:
        判定為 PTL 的 error 訊息 substring tuple。預設 `DEFAULT_PTL_PATTERNS`。
    """

    truncate: TruncateCallback[Any]
    max_retries: int = 3
    patterns: tuple[str, ...] = DEFAULT_PTL_PATTERNS

    def __post_init__(self) -> None:
        if self.max_retries < 0:
            raise ValueError(
                f"max_retries must be >= 0, got {self.max_retries}"
            )

    # -------------------------------------------------------- sync entry-point

    def run_sync(
        self,
        call: Callable[[PromptT], Any],
        prompt: PromptT,
    ) -> Any:
        """跑同步 LLM call,捕 PTL,truncate 後 retry。

        `call(prompt)` 應同步回 LLM response。
        """
        current = prompt
        last_error: BaseException | None = None
        for attempt in range(self.max_retries + 1):
            try:
                return call(current)
            except BaseException as exc:  # noqa: BLE001  PTL 通常是 LLM SDK 自家 exc
                if not _is_ptl_error(exc, self.patterns):
                    raise
                last_error = exc
                if attempt >= self.max_retries:
                    # 用完 retry 額度,把最後一次的 error 還回去。
                    raise
                # 還有 retry 額度,truncate 後續打。
                current = self.truncate(current, exc)
        # Unreachable — 上面 loop 必然 return 或 raise;為了型別檢查 fallback。
        assert last_error is not None
        raise last_error

    # ------------------------------------------------------- async entry-point

    async def run_async(
        self,
        call: Callable[[PromptT], Awaitable[Any]],
        prompt: PromptT,
    ) -> Any:
        """跑非同步 LLM call,捕 PTL,truncate 後 retry。

        `call(prompt)` 應是 async coroutine function。
        """
        current = prompt
        last_error: BaseException | None = None
        for attempt in range(self.max_retries + 1):
            try:
                return await call(current)
            except BaseException as exc:  # noqa: BLE001
                if not _is_ptl_error(exc, self.patterns):
                    raise
                last_error = exc
                if attempt >= self.max_retries:
                    raise
                # truncate 本身允許 sync function;若 caller 給 async,await 之。
                truncated: Any = self.truncate(current, exc)
                if inspect.isawaitable(truncated):
                    truncated = await truncated
                current = truncated
        assert last_error is not None
        raise last_error


# ---------------------------------------------------------------------------
# 顯式對外 API list
# ---------------------------------------------------------------------------


__all__ = [
    "DEFAULT_PTL_PATTERNS",
    "BudgetExceeded",
    "BudgetTracker",
    "ContinuationStrategy",
    "HardCallback",
    "PTLRetry",
    "SoftCallback",
    "TruncateCallback",
    "with_budget",
]


# asyncio import — keep at end so module import overhead remains light when
# asyncio 不被實際呼叫;但 inspect.iscoroutinefunction / await syntax 需要 runtime
# 有 asyncio。保留 explicit import 讓靜態檢查工具看得到依賴。
_ = asyncio
