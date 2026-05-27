"""Generic retry framework — RetryPolicy / RetryDecision / RetryPolicies / with_retry。

對應 enhancement roadmap §4.10 / P2-2 任務。把上游 ``openai-agents-python``
``src/agents/retry.py`` 的 policy 組合概念 port 成一個獨立、零外部相依的子系
統,並對齊 anila-agent 既有兩條約束:

* 跟 P0-9 tracing 整合:每次 retry attempt 自動開一個 ``retry.attempt.N``
  child span,把 error / delay / reason 記到 span attributes。
* 跟 P1-9 ``core.token_budget.PTLRetry`` 對齊 interface:提供 adapter class
  讓既有 PTL truncate flow 也能塞進通用 retry loop。

設計重點
========

1. **零外部相依**:只用 std lib(``time`` / ``asyncio`` / ``functools`` /
   ``inspect``)+ anila 自己的 :mod:`anila_agent.tracing`。**不引** openai /
   anthropic / httpx SDK。
2. **error 模型走 duck-typing**:不綁死特定 SDK 的 exception class。policy
   靠讀 ``error.status_code`` / ``error.response.headers["Retry-After"]`` /
   ``error.__class__.__name__`` 之類「常見欄位」判斷;欄位 missing 就回
   ``should_retry=False``,避免吞掉 caller 不認得的 error。
3. **同步 + 非同步雙模式**:``with_retry`` 同時支援 sync function 與 async
   coroutine function — runner / agent 環境兩種都見得到。
4. **policy 組合**:``RetryPolicies.combined(*policies)`` 走「任一 policy 回
   should_retry=True 就 retry」(or 語意),不寫 and 版本,因為實務上 retry
   邏輯 99% 都是 or。caller 要 and 語意請自己寫 lambda policy。
5. **delay 由 policy 決定**:不在 decorator 寫死 exponential backoff,讓
   policy 自己算 ``delay_seconds``。內建 factory 已封好常見 backoff,caller
   只需挑一個。
6. **tracing 失敗不影響主流程**:若 tracer 為 None / span 開不出來,retry 照
   跑。
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar, runtime_checkable

from anila_agent.tracing.tracer import Tracer

# ---------------------------------------------------------------------------
# RetryDecision — 單次決策資料
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RetryDecision:
    """Policy ``should_retry`` 的回傳值;immutable,方便上游 cache / log。

    Attributes
    ----------
    should_retry:
        是否要重試;``False`` 直接 re-raise 原 error。
    delay_seconds:
        重試前要 sleep 幾秒;``0.0`` 表示立刻 retry。policy 自己算
        backoff,decorator 不替它做。
    reason:
        decision 的人類可讀理由,寫到 tracing span / log,方便 debug。
        空字串代表「沒理由」(通常出現在 ``should_retry=False`` 的場合)。
    """

    should_retry: bool
    delay_seconds: float = 0.0
    reason: str = ""

    @classmethod
    def no(cls, reason: str = "") -> RetryDecision:
        """sugar:``RetryDecision(should_retry=False, reason=...)``。"""
        return cls(should_retry=False, delay_seconds=0.0, reason=reason)

    @classmethod
    def yes(cls, delay_seconds: float = 0.0, reason: str = "") -> RetryDecision:
        """sugar:``RetryDecision(should_retry=True, ...)``。"""
        return cls(should_retry=True, delay_seconds=delay_seconds, reason=reason)


# ---------------------------------------------------------------------------
# RetryPolicy Protocol — caller 自寫 policy 也走這
# ---------------------------------------------------------------------------


@runtime_checkable
class RetryPolicy(Protocol):
    """Retry policy interface — duck-typed,符合 ``should_retry`` 簽名即可。

    Note:
        刻意走 Protocol 而非 ABC,避免強迫 caller 繼承;測試 / mock 也較簡單。
    """

    def should_retry(self, error: BaseException, attempt: int) -> RetryDecision:
        """根據 error 與當前 attempt 序號(從 1 起算)回傳 :class:`RetryDecision`。

        Args:
            error: 上次 attempt 觸發的 exception。
            attempt: 當前已完成的 attempt 次數(1 = 第一次失敗)。decorator
                會把這個數字傳進來,policy 可用它算 exponential backoff 與
                max_retries 上限。
        """
        ...


# ---------------------------------------------------------------------------
# 內部 helper — duck-type 讀 error 欄位
# ---------------------------------------------------------------------------


def _extract_status_code(error: BaseException) -> int | None:
    """從常見 HTTP error shape 取 status_code。

    支援:
    * ``error.status_code``(httpx / openai SDK 慣例)
    * ``error.response.status_code``(requests / httpx exception 包 response 物件)
    * ``error.code``(部分 SDK 用 code 而非 status_code)
    """
    for path in ("status_code", "code"):
        if hasattr(error, path):
            value = getattr(error, path)
            if isinstance(value, int):
                return value
    response = getattr(error, "response", None)
    if response is not None and hasattr(response, "status_code"):
        value = response.status_code
        if isinstance(value, int):
            return value
    return None


def _extract_retry_after(error: BaseException) -> float | None:
    """從 error 物件取 ``Retry-After`` header 秒數。

    支援:
    * ``error.retry_after``(部分 SDK 已 normalize 成屬性)
    * ``error.response.headers["Retry-After"]``(httpx / requests response)
    * ``error.headers["Retry-After"]``(error 物件直接帶 headers)

    Header value 解析:
    * 純數字 → float 秒數
    * 解不開(HTTP-date 格式或亂字)→ None,讓 caller 走別的 fallback

    Note:
        HTTP-date 格式(RFC 7231)沒在本 helper 處理,因為實務上絕大多數
        provider 回的是純秒數。要的話 caller 自寫 policy 覆寫。
    """
    direct = getattr(error, "retry_after", None)
    if isinstance(direct, (int, float)):
        return float(direct)

    def _read_headers(headers: Any) -> float | None:
        if headers is None:
            return None
        # dict-like 通常支援 .get 或 []
        value: Any = None
        try:
            value = headers.get("Retry-After")
        except AttributeError:
            try:
                value = headers["Retry-After"]
            except (KeyError, TypeError):
                return None
        if value is None:
            # 試 lowercase header 名(httpx 預設 case-insensitive 但保險)
            try:
                value = headers.get("retry-after")
            except AttributeError:
                value = None
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    response = getattr(error, "response", None)
    if response is not None:
        result = _read_headers(getattr(response, "headers", None))
        if result is not None:
            return result
    return _read_headers(getattr(error, "headers", None))


def _is_network_error(error: BaseException) -> bool:
    """duck-type 判斷是不是「連線層」error。

    判定法(任一條成立即 True):
    * Python built-in ``ConnectionError`` / ``TimeoutError`` 的 subclass
    * ``OSError`` 的 subclass(broken pipe / refused / DNS)
    * class name 帶 ``connect`` / ``timeout`` / ``network`` 子字串

    重點:不 import httpx / requests / openai,讓 module 保持零相依。
    """
    if isinstance(error, (ConnectionError, TimeoutError)):
        return True
    if isinstance(error, OSError):
        return True
    name = type(error).__name__.lower()
    return any(token in name for token in ("connect", "timeout", "network"))


def _exp_backoff(base_delay: float, attempt: int, max_delay: float = 60.0) -> float:
    """exponential backoff:``base_delay * 2^(attempt-1)``,但 cap 在 ``max_delay``。

    ``attempt=1`` → base_delay,``attempt=2`` → 2*base_delay,以此類推。
    不加 jitter — 測試易碎且本 module 範圍內未必需要;caller 想要自己包。
    """
    if attempt <= 0:
        return 0.0
    delay: float = base_delay * float(2 ** (attempt - 1))
    return min(delay, max_delay)


# ---------------------------------------------------------------------------
# 內建 policies(dataclass form;比 closure 好 debug / 好序列化)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _NetworkErrorPolicy:
    """重試 HTTP 5xx / connection / timeout 類 error,exponential backoff。

    ``error.status_code in 500..599`` 或 ``_is_network_error(error)`` 任一成立
    即視為「值得 retry」。4xx 一律不 retry(client 邏輯錯,retry 也是錯)。
    """

    max_retries: int = 3
    base_delay: float = 1.0
    max_delay: float = 60.0

    def should_retry(self, error: BaseException, attempt: int) -> RetryDecision:
        if attempt > self.max_retries:
            return RetryDecision.no(
                reason=f"network_error: exceeded max_retries={self.max_retries}"
            )
        status = _extract_status_code(error)
        if status is not None and 500 <= status < 600:
            return RetryDecision.yes(
                delay_seconds=_exp_backoff(self.base_delay, attempt, self.max_delay),
                reason=f"network_error: HTTP {status}",
            )
        if _is_network_error(error):
            return RetryDecision.yes(
                delay_seconds=_exp_backoff(self.base_delay, attempt, self.max_delay),
                reason=f"network_error: {type(error).__name__}",
            )
        # 4xx / 非連線層 error 不歸這 policy 管。
        return RetryDecision.no(reason="network_error: not a retriable error")


@dataclass(frozen=True)
class _RetryAfterPolicy:
    """讀 response header 的 ``Retry-After`` 決定 delay。

    沒 header 就回 no — 留給 combined policy 裡別的 policy 處理。
    """

    max_retries: int = 5

    def should_retry(self, error: BaseException, attempt: int) -> RetryDecision:
        if attempt > self.max_retries:
            return RetryDecision.no(
                reason=f"retry_after: exceeded max_retries={self.max_retries}"
            )
        delay = _extract_retry_after(error)
        if delay is None:
            return RetryDecision.no(reason="retry_after: no Retry-After header")
        return RetryDecision.yes(
            delay_seconds=delay,
            reason=f"retry_after: header={delay}s",
        )


@dataclass(frozen=True)
class _RateLimitPolicy:
    """處理 HTTP 429,exponential backoff;優先使用 Retry-After 若有。"""

    max_retries: int = 5
    base_delay: float = 2.0
    max_delay: float = 60.0

    def should_retry(self, error: BaseException, attempt: int) -> RetryDecision:
        if attempt > self.max_retries:
            return RetryDecision.no(
                reason=f"rate_limit: exceeded max_retries={self.max_retries}"
            )
        status = _extract_status_code(error)
        if status != 429:
            return RetryDecision.no(reason="rate_limit: not HTTP 429")
        # 優先用 Retry-After header(provider 明確告訴你等多久,聽他的)
        header_delay = _extract_retry_after(error)
        if header_delay is not None:
            return RetryDecision.yes(
                delay_seconds=header_delay,
                reason=f"rate_limit: HTTP 429, Retry-After={header_delay}s",
            )
        return RetryDecision.yes(
            delay_seconds=_exp_backoff(self.base_delay, attempt, self.max_delay),
            reason="rate_limit: HTTP 429, exp_backoff",
        )


@dataclass(frozen=True)
class _CombinedPolicy:
    """多 policy 組合 — 任一回 ``should_retry=True`` 就 retry,delay 取第一個 yes。

    順序很重要:把 Retry-After 類 policy 放前面,exp backoff 放後面,讓
    provider 明確指示優先生效。
    """

    policies: tuple[RetryPolicy, ...] = field(default_factory=tuple)

    def should_retry(self, error: BaseException, attempt: int) -> RetryDecision:
        last_no: RetryDecision = RetryDecision.no(reason="combined: no policy matched")
        for policy in self.policies:
            decision = policy.should_retry(error, attempt)
            if decision.should_retry:
                return decision
            last_no = decision
        return last_no


# ---------------------------------------------------------------------------
# RetryPolicies — 對外 factory 集合
# ---------------------------------------------------------------------------


class RetryPolicies:
    """Static factory 集合;對外公開的入口,所有內建 policy 從這建構。

    Example:
        ```python
        from anila_agent.providers import RetryPolicies, with_retry

        policy = RetryPolicies.combined(
            RetryPolicies.retry_after(),
            RetryPolicies.rate_limit(),
            RetryPolicies.network_error(),
        )

        @with_retry(policy)
        async def call_llm(prompt: str) -> Any:
            return await client.messages.create(...)
        ```
    """

    @staticmethod
    def network_error(
        max_retries: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 60.0,
    ) -> RetryPolicy:
        """HTTP 5xx + connection / timeout error,exponential backoff。"""
        return _NetworkErrorPolicy(
            max_retries=max_retries,
            base_delay=base_delay,
            max_delay=max_delay,
        )

    @staticmethod
    def retry_after(max_retries: int = 5) -> RetryPolicy:
        """讀 response 的 ``Retry-After`` header 決定 delay。"""
        return _RetryAfterPolicy(max_retries=max_retries)

    @staticmethod
    def rate_limit(
        max_retries: int = 5,
        base_delay: float = 2.0,
        max_delay: float = 60.0,
    ) -> RetryPolicy:
        """HTTP 429,exponential backoff;優先用 Retry-After header 若有。"""
        return _RateLimitPolicy(
            max_retries=max_retries,
            base_delay=base_delay,
            max_delay=max_delay,
        )

    @staticmethod
    def combined(*policies: RetryPolicy) -> RetryPolicy:
        """多 policy 組合(or 語意:任一 yes 就 retry,順序決定 delay 優先權)。"""
        return _CombinedPolicy(policies=tuple(policies))

    @staticmethod
    def ptl(
        truncate: Callable[[Any, BaseException], Any],
        max_retries: int = 3,
        patterns: tuple[str, ...] | None = None,
    ) -> PTLRetryPolicy:
        """PTL(prompt too long)policy — 與 core.token_budget.PTLRetry 對齊。

        回傳 :class:`PTLRetryPolicy`(同時是 RetryPolicy + truncate holder)。
        caller 在 with_retry decorator 上拿 ``policy.last_truncated`` 取縮過的
        prompt(因為 RetryPolicy interface 純判斷,truncate 走 side channel)。
        """
        return PTLRetryPolicy(
            truncate=truncate,
            max_retries=max_retries,
            patterns=patterns,
        )


# ---------------------------------------------------------------------------
# PTL adapter — 對齊 RetryPolicy interface
# ---------------------------------------------------------------------------


@dataclass
class PTLRetryPolicy:
    """把 :class:`anila_agent.core.token_budget.PTLRetry` 的概念套到
    :class:`RetryPolicy` interface。

    與 PTLRetry 的差別
    -----------------
    PTLRetry 是 *call wrapper*(``run_sync(call, prompt)`` 自己負責執行 + retry +
    truncate),語意上比較像 helper class。
    PTLRetryPolicy 是 *decision policy*,只回答「該不該 retry」與「等多久」,
    truncate 結果寫進 ``last_truncated`` 供 caller(通常是 ``with_retry``
    上層 wrapper)取用。

    這樣 PTL 的縮 prompt 行為才能塞進通用 ``with_retry(policy)`` flow,跟
    network_error / rate_limit 等其他 policy 同 layer。

    Attributes
    ----------
    truncate:
        ``(current_prompt, error) -> new_prompt``;decision 為 yes 時被呼叫,
        新 prompt 寫進 ``last_truncated``。
    max_retries:
        最多 retry 次數(預設 3,跟 PTLRetry 對齊)。
    patterns:
        判定為 PTL error 的 substring tuple,預設用
        :data:`anila_agent.core.token_budget.DEFAULT_PTL_PATTERNS`。
    last_truncated:
        最近一次 truncate 後的 prompt;``with_retry`` 不會主動讀,留給 caller
        在 retry callable 內自行透過 closure 取(見模組 docstring example)。
    """

    truncate: Callable[[Any, BaseException], Any]
    max_retries: int = 3
    patterns: tuple[str, ...] | None = None
    last_truncated: Any = None

    def should_retry(self, error: BaseException, attempt: int) -> RetryDecision:
        # 動態 import 避免 module 載入順序問題(core/token_budget 反過來不依賴 providers)。
        from anila_agent.core.token_budget import DEFAULT_PTL_PATTERNS, _is_ptl_error

        patterns = self.patterns or DEFAULT_PTL_PATTERNS
        if not _is_ptl_error(error, patterns):
            return RetryDecision.no(reason="ptl: not a prompt-too-long error")
        if attempt > self.max_retries:
            return RetryDecision.no(
                reason=f"ptl: exceeded max_retries={self.max_retries}"
            )
        # 嘗試 truncate;失敗就 propagate(caller 不該吞掉 truncate exception)
        try:
            self.last_truncated = self.truncate(self.last_truncated, error)
        except Exception as exc:  # truncate 失敗讓 caller 看到原因,不 swallow
            raise exc
        return RetryDecision.yes(
            delay_seconds=0.0,
            reason="ptl: truncated, retrying",
        )


# ---------------------------------------------------------------------------
# with_retry decorator
# ---------------------------------------------------------------------------


F = TypeVar("F", bound=Callable[..., Any])
AsyncF = TypeVar("AsyncF", bound=Callable[..., Awaitable[Any]])


def with_retry(
    policy: RetryPolicy,
    *,
    tracer: Tracer | None = None,
    sleep: Callable[[float], None] | None = None,
    async_sleep: Callable[[float], Awaitable[None]] | None = None,
) -> Callable[[F], F]:
    """Decorator:套 RetryPolicy 自動 retry。同時支援 sync 與 async function。

    每次 retry 走以下 flow:
    1. 呼叫 ``func(*args, **kwargs)`` → 抓 exception。
    2. ``policy.should_retry(error, attempt)`` → 拿 :class:`RetryDecision`。
    3. 若 ``should_retry=False`` → re-raise。
    4. 若 ``should_retry=True`` → 開 ``retry.attempt.N`` child span、sleep
       ``delay_seconds``、attempt += 1、回 step 1。

    Args:
        policy: RetryPolicy 實例(內建 factory 或 caller 自寫)。
        tracer: 用來開 retry child span 的 Tracer;``None`` 表示不寫 tracing
            (沒人在意 / 測試環境)。預設 None。
        sleep: sync sleep 函式;測試時可 inject fake 避免真睡。預設
            ``time.sleep``。
        async_sleep: async sleep 函式;測試時可 inject fake。預設
            ``asyncio.sleep``。

    Returns:
        decorator function。

    Note:
        decorator 不 catch ``BaseException`` 的非 Exception subclass(
        ``KeyboardInterrupt`` / ``SystemExit``),這兩個讓 caller / Python
        runtime 自處理。
    """
    _sleep = sleep if sleep is not None else time.sleep
    _async_sleep = async_sleep if async_sleep is not None else asyncio.sleep

    def decorator(func: F) -> F:
        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                attempt = 0
                while True:
                    try:
                        return await func(*args, **kwargs)
                    except Exception as exc:
                        attempt += 1
                        decision = policy.should_retry(exc, attempt)
                        _emit_retry_span(tracer, attempt, exc, decision)
                        if not decision.should_retry:
                            raise
                        if decision.delay_seconds > 0:
                            await _async_sleep(decision.delay_seconds)

            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            attempt = 0
            while True:
                try:
                    return func(*args, **kwargs)
                except Exception as exc:
                    attempt += 1
                    decision = policy.should_retry(exc, attempt)
                    _emit_retry_span(tracer, attempt, exc, decision)
                    if not decision.should_retry:
                        raise
                    if decision.delay_seconds > 0:
                        _sleep(decision.delay_seconds)

        return sync_wrapper  # type: ignore[return-value]

    return decorator


def _emit_retry_span(
    tracer: Tracer | None,
    attempt: int,
    error: BaseException,
    decision: RetryDecision,
) -> None:
    """開一個 short-lived span 記錄這次 retry attempt 的決策。

    tracing 是 best-effort:tracer=None / span 開不出來都不影響主流程。
    span name 用 ``retry.attempt.N`` 方便 grep。
    """
    if tracer is None:
        return
    try:
        with tracer.start_span(
            f"retry.attempt.{attempt}",
            attributes={
                "retry.attempt": attempt,
                "retry.should_retry": decision.should_retry,
                "retry.delay_seconds": decision.delay_seconds,
                "retry.reason": decision.reason,
                "retry.error_type": type(error).__name__,
                "retry.error_message": str(error)[:500],
            },
        ):
            # context manager 進入/離開就已經把 start/end 事件丟給 processor,
            # span body 不需要做事。
            pass
    except Exception:  # tracing 失敗不該影響主流程,silently drop
        return


__all__ = [
    "PTLRetryPolicy",
    "RetryDecision",
    "RetryPolicies",
    "RetryPolicy",
    "with_retry",
]
