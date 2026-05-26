"""Unit tests:P1-9 Token budget continuation。

涵蓋面
======

* `BudgetTracker.record` 累加 prompt / completion / used / remaining / ratio。
* 建構時 validation(max_tokens / soft_threshold_ratio 邊界)。
* `is_soft_exceeded` / `is_hard_exceeded` 兩個 threshold 各自的觸發點。
* `reset` 把累計欄位歸零、不影響 max / ratio 設定。
* `with_budget` decorator:
  - sync function 與 async coroutine function 兩種路徑都會 record。
  - 從 response.usage(object / dict 兩種 shape)取 tokens。
  - 沒有 usage 欄位時不 record 但不 crash。
  - soft / hard callback 各觸發一次、`raise_on_hard` 控 raise。
* `PTLRetry`:
  - PTL error → truncate → retry 直到成功。
  - PTL error 持續 max_retries+1 次後 re-raise 最後一個 error。
  - 非 PTL error 不重試,直接 propagate。
  - `run_async` 同樣行為,且能等 async truncate callback。
  - `max_retries=0` 等同「最多 1 attempt」,失敗即 raise。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

from anila_agent.core.token_budget import (
    DEFAULT_PTL_PATTERNS,
    BudgetExceeded,
    BudgetTracker,
    ContinuationStrategy,
    PTLRetry,
    with_budget,
)


# ---------------------------------------------------------------------------
# 共用 fixture / helper
# ---------------------------------------------------------------------------


@dataclass
class _FakeUsage:
    """模擬 OpenAI / Anthropic SDK 的 usage 物件 shape。"""

    prompt_tokens: int
    completion_tokens: int


@dataclass
class _FakeResponse:
    """模擬 LLM SDK 回傳 response 物件(有 .usage 子物件)。"""

    usage: _FakeUsage
    content: str = "ok"


# ---------------------------------------------------------------------------
# BudgetTracker — 基本累加 / view
# ---------------------------------------------------------------------------


def test_tracker_initial_state_is_clean() -> None:
    """新建 tracker 應該所有累計欄位為 0、remaining=max、ratio=0。"""
    tracker = BudgetTracker(max_tokens=10_000)
    assert tracker.used == 0
    assert tracker.prompt_tokens == 0
    assert tracker.completion_tokens == 0
    assert tracker.records == 0
    assert tracker.remaining == 10_000
    assert tracker.usage_ratio == 0.0
    assert not tracker.is_soft_exceeded()
    assert not tracker.is_hard_exceeded()


def test_tracker_record_accumulates() -> None:
    """連續 record 應該累加 prompt + completion,used = sum,records = count。"""
    tracker = BudgetTracker(max_tokens=10_000)
    tracker.record(prompt_tokens=100, completion_tokens=50)
    tracker.record(prompt_tokens=200, completion_tokens=100)
    assert tracker.prompt_tokens == 300
    assert tracker.completion_tokens == 150
    assert tracker.used == 450
    assert tracker.records == 2
    assert tracker.remaining == 10_000 - 450
    assert tracker.usage_ratio == pytest.approx(450 / 10_000)


def test_tracker_remaining_clamps_to_zero_when_over() -> None:
    """used > max 時 remaining 應該回 0,而不是負數(下游 UI 友善)。"""
    tracker = BudgetTracker(max_tokens=1000)
    tracker.record(prompt_tokens=900, completion_tokens=200)
    assert tracker.used == 1100
    assert tracker.remaining == 0
    # ratio 仍可超過 1.0,讓 caller 看得到「破多少」。
    assert tracker.usage_ratio > 1.0


def test_tracker_record_rejects_negative_tokens() -> None:
    """LLM provider 不該回負數;傳負數視為 caller bug,raise ValueError。"""
    tracker = BudgetTracker(max_tokens=10_000)
    with pytest.raises(ValueError):
        tracker.record(prompt_tokens=-1, completion_tokens=0)
    with pytest.raises(ValueError):
        tracker.record(prompt_tokens=0, completion_tokens=-1)


# ---------------------------------------------------------------------------
# BudgetTracker — constructor validation
# ---------------------------------------------------------------------------


def test_tracker_constructor_rejects_invalid_max_tokens() -> None:
    """max_tokens 必須 > 0。"""
    with pytest.raises(ValueError):
        BudgetTracker(max_tokens=0)
    with pytest.raises(ValueError):
        BudgetTracker(max_tokens=-100)


def test_tracker_constructor_rejects_invalid_soft_ratio() -> None:
    """soft_threshold_ratio 必須在 (0, 1] 區間內。"""
    with pytest.raises(ValueError):
        BudgetTracker(max_tokens=10_000, soft_threshold_ratio=0.0)
    with pytest.raises(ValueError):
        BudgetTracker(max_tokens=10_000, soft_threshold_ratio=1.5)
    with pytest.raises(ValueError):
        BudgetTracker(max_tokens=10_000, soft_threshold_ratio=-0.1)
    # 邊界 1.0 應該被接受(等於 hard 等於 soft,degenerate 但 valid)。
    BudgetTracker(max_tokens=10_000, soft_threshold_ratio=1.0)


# ---------------------------------------------------------------------------
# BudgetTracker — soft / hard threshold
# ---------------------------------------------------------------------------


def test_tracker_soft_threshold_triggers_at_ratio() -> None:
    """used 達 soft_threshold_ratio * max 時 is_soft_exceeded 翻 True。"""
    tracker = BudgetTracker(max_tokens=1000, soft_threshold_ratio=0.8)
    # 799 / 1000 = 0.799 < 0.8,還沒到
    tracker.record(prompt_tokens=400, completion_tokens=399)
    assert not tracker.is_soft_exceeded()
    # 加 1 變 800,正好觸發
    tracker.record(prompt_tokens=1, completion_tokens=0)
    assert tracker.is_soft_exceeded()
    assert not tracker.is_hard_exceeded()


def test_tracker_hard_threshold_triggers_at_max() -> None:
    """used >= max 視為 hard exceeded。"""
    tracker = BudgetTracker(max_tokens=1000)
    tracker.record(prompt_tokens=600, completion_tokens=399)  # 999
    assert not tracker.is_hard_exceeded()
    tracker.record(prompt_tokens=1, completion_tokens=0)  # 1000
    assert tracker.is_hard_exceeded()


def test_tracker_soft_threshold_absolute_value() -> None:
    """`soft_threshold()` 應該回 int 絕對值,向下取整。"""
    tracker = BudgetTracker(max_tokens=1000, soft_threshold_ratio=0.75)
    assert tracker.soft_threshold() == 750


# ---------------------------------------------------------------------------
# BudgetTracker — reset
# ---------------------------------------------------------------------------


def test_tracker_reset_clears_counters_but_keeps_config() -> None:
    """reset 後累計欄位歸零,但 max_tokens / soft_ratio 仍保留。"""
    tracker = BudgetTracker(max_tokens=1000, soft_threshold_ratio=0.7)
    tracker.record(prompt_tokens=500, completion_tokens=300)
    assert tracker.used == 800
    assert tracker.is_soft_exceeded()

    tracker.reset()
    assert tracker.used == 0
    assert tracker.prompt_tokens == 0
    assert tracker.completion_tokens == 0
    assert tracker.records == 0
    assert tracker.remaining == 1000
    assert not tracker.is_soft_exceeded()
    # config 沒掉
    assert tracker.max_tokens == 1000
    assert tracker.soft_threshold_ratio == 0.7


# ---------------------------------------------------------------------------
# BudgetExceeded
# ---------------------------------------------------------------------------


def test_budget_exceeded_carries_used_and_limit() -> None:
    """BudgetExceeded 應該帶 used / limit 兩個欄位給 caller 顯示。"""
    err = BudgetExceeded(used=1500, limit=1000)
    assert err.used == 1500
    assert err.limit == 1000
    assert "1500" in str(err) and "1000" in str(err)


def test_budget_exceeded_custom_message() -> None:
    """允許 caller 傳自訂 message 覆蓋預設文字。"""
    err = BudgetExceeded(used=100, limit=50, message="dialogue too long")
    assert str(err) == "dialogue too long"


# ---------------------------------------------------------------------------
# with_budget decorator — sync path
# ---------------------------------------------------------------------------


def test_with_budget_sync_records_from_usage_object() -> None:
    """Sync function 跑完後應自動從 response.usage 取 tokens 記到 tracker。"""
    tracker = BudgetTracker(max_tokens=10_000)

    @with_budget(tracker)
    def call_llm(prompt: str) -> _FakeResponse:
        return _FakeResponse(usage=_FakeUsage(prompt_tokens=120, completion_tokens=80))

    result = call_llm("hi")
    assert isinstance(result, _FakeResponse)
    assert result.content == "ok"
    assert tracker.prompt_tokens == 120
    assert tracker.completion_tokens == 80
    assert tracker.used == 200
    assert tracker.records == 1


def test_with_budget_sync_records_from_usage_dict() -> None:
    """Response 是 dict(legacy chat-completion SDK)時也要能取 usage。"""
    tracker = BudgetTracker(max_tokens=10_000)

    @with_budget(tracker)
    def call_llm() -> dict[str, Any]:
        return {
            "content": "yo",
            "usage": {"prompt_tokens": 50, "completion_tokens": 25},
        }

    call_llm()
    assert tracker.used == 75


def test_with_budget_sync_no_usage_field_skips_record_but_no_crash() -> None:
    """沒有 usage 欄位的 response 不該 crash,只是不 record。"""
    tracker = BudgetTracker(max_tokens=10_000)

    @with_budget(tracker)
    def call_llm() -> str:
        return "plain string, no usage"

    result = call_llm()
    assert result == "plain string, no usage"
    assert tracker.used == 0
    assert tracker.records == 0


def test_with_budget_sync_returns_response_unchanged() -> None:
    """Decorator 不該 wrap / mutate response,原樣回傳。"""
    tracker = BudgetTracker(max_tokens=10_000)
    sentinel = _FakeResponse(usage=_FakeUsage(prompt_tokens=10, completion_tokens=5))

    @with_budget(tracker)
    def call_llm() -> _FakeResponse:
        return sentinel

    assert call_llm() is sentinel


# ---------------------------------------------------------------------------
# with_budget — async path
# ---------------------------------------------------------------------------


def test_with_budget_async_records_from_usage_object() -> None:
    """Async coroutine function 也要 record。"""
    tracker = BudgetTracker(max_tokens=10_000)

    @with_budget(tracker)
    async def call_llm() -> _FakeResponse:
        return _FakeResponse(usage=_FakeUsage(prompt_tokens=300, completion_tokens=100))

    result = asyncio.run(call_llm())
    assert isinstance(result, _FakeResponse)
    assert tracker.used == 400
    assert tracker.records == 1


# ---------------------------------------------------------------------------
# with_budget — soft / hard callback
# ---------------------------------------------------------------------------


def test_with_budget_fires_soft_callback_once() -> None:
    """soft callback 應該在第一次跨過 soft threshold 時觸發,不重複觸發。"""
    tracker = BudgetTracker(max_tokens=1000, soft_threshold_ratio=0.5)
    soft_calls: list[int] = []

    strategy = ContinuationStrategy(
        on_soft=lambda t: soft_calls.append(t.used)
    )

    @with_budget(tracker, strategy)
    def call_llm(usage: _FakeUsage) -> _FakeResponse:
        return _FakeResponse(usage=usage)

    # 第一次 used=300,沒到 500 soft,不觸發
    call_llm(_FakeUsage(prompt_tokens=200, completion_tokens=100))
    assert soft_calls == []
    # 第二次 used=600,觸發一次
    call_llm(_FakeUsage(prompt_tokens=200, completion_tokens=100))
    assert soft_calls == [600]
    # 第三次 used=700,still soft exceeded 但「同一個 wrapper instance」不重複觸發
    call_llm(_FakeUsage(prompt_tokens=50, completion_tokens=50))
    assert soft_calls == [600]


def test_with_budget_fires_hard_callback_and_raises() -> None:
    """hard callback 先觸發,然後 raise BudgetExceeded。"""
    tracker = BudgetTracker(max_tokens=500, soft_threshold_ratio=0.5)
    hard_calls: list[int] = []
    soft_calls: list[int] = []

    strategy = ContinuationStrategy(
        on_soft=lambda t: soft_calls.append(t.used),
        on_hard=lambda t: hard_calls.append(t.used),
    )

    @with_budget(tracker, strategy)
    def call_llm(usage: _FakeUsage) -> _FakeResponse:
        return _FakeResponse(usage=usage)

    # 一輪就直接破 hard:used=600 > 500
    with pytest.raises(BudgetExceeded) as exc_info:
        call_llm(_FakeUsage(prompt_tokens=400, completion_tokens=200))

    assert exc_info.value.used == 600
    assert exc_info.value.limit == 500
    # soft 與 hard 都會 fire(因為 600 > 250 也 > 500)
    assert soft_calls == [600]
    assert hard_calls == [600]


def test_with_budget_raise_on_hard_false_does_not_raise() -> None:
    """`raise_on_hard=False` 時 callback 觸發但不 raise,讓 caller 自己決定。"""
    tracker = BudgetTracker(max_tokens=500)
    hard_calls: list[int] = []

    strategy = ContinuationStrategy(
        on_hard=lambda t: hard_calls.append(t.used),
        raise_on_hard=False,
    )

    @with_budget(tracker, strategy)
    def call_llm() -> _FakeResponse:
        return _FakeResponse(usage=_FakeUsage(prompt_tokens=400, completion_tokens=200))

    # 不該 raise
    result = call_llm()
    assert isinstance(result, _FakeResponse)
    assert hard_calls == [600]
    assert tracker.is_hard_exceeded()


def test_with_budget_no_strategy_defaults_to_raise_on_hard() -> None:
    """不傳 strategy 時 default ContinuationStrategy 應該 raise on hard。"""
    tracker = BudgetTracker(max_tokens=100)

    @with_budget(tracker)
    def call_llm() -> _FakeResponse:
        return _FakeResponse(usage=_FakeUsage(prompt_tokens=200, completion_tokens=0))

    with pytest.raises(BudgetExceeded):
        call_llm()


# ---------------------------------------------------------------------------
# PTLRetry — sync
# ---------------------------------------------------------------------------


def test_ptl_retry_truncates_and_retries_until_success() -> None:
    """前 2 次 raise PTL error,第 3 次成功 → truncate 應被呼叫 2 次。"""
    truncate_calls: list[str] = []

    def truncate(prompt: str, _err: BaseException) -> str:
        truncate_calls.append(prompt)
        # 模擬實際 compactor:把長度減半
        return prompt[: len(prompt) // 2]

    attempts: list[str] = []

    def call_llm(prompt: str) -> str:
        attempts.append(prompt)
        if len(prompt) > 10:
            raise RuntimeError("context length exceeded")
        return f"response for {prompt}"

    retry = PTLRetry(truncate=truncate, max_retries=3)
    result = retry.run_sync(call_llm, "x" * 40)
    # 40 → 20(retry 1)→ 10(retry 2),len 10 進入「不 >10」分支,成功
    # 40 chars → 20 chars → 10 chars (success)
    assert len(attempts) == 3
    assert truncate_calls == ["x" * 40, "x" * 20]
    assert result.startswith("response for ")


def test_ptl_retry_exhausts_retries_reraises_last_error() -> None:
    """連續 PTL error,用完 retry 額度就 re-raise 最後一個 error。"""

    def truncate(prompt: str, _err: BaseException) -> str:
        # truncate 不夠快:始終回相同長度,逼到耗盡 retries
        return prompt

    call_count = {"n": 0}

    def call_llm(_prompt: str) -> str:
        call_count["n"] += 1
        raise RuntimeError(f"prompt too long (call {call_count['n']})")

    retry = PTLRetry(truncate=truncate, max_retries=3)
    with pytest.raises(RuntimeError) as exc_info:
        retry.run_sync(call_llm, "x" * 1000)

    # 3 次 retry + 1 次原始 attempt = 4 次 attempt
    assert call_count["n"] == 4
    # 最後一次的 error 該被 re-raise
    assert "call 4" in str(exc_info.value)


def test_ptl_retry_does_not_swallow_non_ptl_errors() -> None:
    """非 PTL error 不該被 retry,該直接 propagate。"""
    truncate_calls: list[str] = []

    def truncate(prompt: str, _err: BaseException) -> str:
        truncate_calls.append(prompt)
        return prompt

    def call_llm(_prompt: str) -> str:
        raise ValueError("totally unrelated bug")

    retry = PTLRetry(truncate=truncate, max_retries=3)
    with pytest.raises(ValueError, match="totally unrelated"):
        retry.run_sync(call_llm, "anything")

    # truncate 不應被呼叫
    assert truncate_calls == []


def test_ptl_retry_max_retries_zero_means_one_attempt() -> None:
    """max_retries=0 → 只給 1 次 attempt,PTL 就立刻 raise,不 retry。"""

    def truncate(prompt: str, _err: BaseException) -> str:
        return prompt

    call_count = {"n": 0}

    def call_llm(_prompt: str) -> str:
        call_count["n"] += 1
        raise RuntimeError("context length exceeded")

    retry = PTLRetry(truncate=truncate, max_retries=0)
    with pytest.raises(RuntimeError):
        retry.run_sync(call_llm, "anything")
    assert call_count["n"] == 1


def test_ptl_retry_rejects_negative_max_retries() -> None:
    """max_retries 必須 >= 0,負數視為 caller bug。"""
    with pytest.raises(ValueError):
        PTLRetry(truncate=lambda p, e: p, max_retries=-1)


def test_ptl_retry_detects_common_ptl_phrasings() -> None:
    """default patterns 該認得 OpenAI / Anthropic 常見錯誤訊息。"""
    samples = [
        "Error code 400: This model's maximum context length is 8192 tokens.",
        "context_length_exceeded",
        "The prompt is too long for this model.",
        "Token limit hit.",
        "context window exceeded",
    ]
    for msg in samples:
        attempts = {"n": 0}

        def call_llm(_prompt: str, _msg: str = msg) -> str:
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RuntimeError(_msg)
            return "ok"

        retry = PTLRetry(truncate=lambda p, _e: p[:1], max_retries=2)
        result = retry.run_sync(call_llm, "yyy")
        assert result == "ok"
        assert attempts["n"] == 2, f"failed on phrasing: {msg!r}"


def test_ptl_retry_custom_patterns() -> None:
    """允許 caller 自訂 patterns(例如不認 default 但認自家錯誤碼)。"""

    def call_llm(_prompt: str) -> str:
        raise RuntimeError("E_PROMPT_OVERFLOW: too big")

    # default 不認,當作非 PTL 直接 propagate
    default_retry = PTLRetry(truncate=lambda p, _e: p, max_retries=2)
    with pytest.raises(RuntimeError):
        default_retry.run_sync(call_llm, "x")

    # 自訂 pattern 認得
    attempts = {"n": 0}

    def call_llm_recovers(_prompt: str) -> str:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("E_PROMPT_OVERFLOW: too big")
        return "ok"

    custom_retry = PTLRetry(
        truncate=lambda p, _e: p,
        max_retries=2,
        patterns=("E_PROMPT_OVERFLOW",),
    )
    assert custom_retry.run_sync(call_llm_recovers, "x") == "ok"


# ---------------------------------------------------------------------------
# PTLRetry — async
# ---------------------------------------------------------------------------


def test_ptl_retry_async_truncates_and_succeeds() -> None:
    """async 版本同樣會 truncate 後重試成功。"""

    async def call_llm(prompt: str) -> str:
        if len(prompt) > 5:
            raise RuntimeError("context length too long")
        return f"got:{prompt}"

    def truncate(prompt: str, _err: BaseException) -> str:
        return prompt[: max(len(prompt) // 2, 1)]

    retry = PTLRetry(truncate=truncate, max_retries=5)
    result = asyncio.run(retry.run_async(call_llm, "x" * 32))
    assert result.startswith("got:")
    assert len(result) - len("got:") <= 5


def test_ptl_retry_async_supports_async_truncate_callback() -> None:
    """truncate callback 可以是 async function(會被 await)。"""

    async def call_llm(prompt: str) -> str:
        if len(prompt) > 4:
            raise RuntimeError("prompt too long")
        return prompt

    async def truncate(prompt: str, _err: BaseException) -> str:
        # 模擬「呼叫遠端 compactor service」
        await asyncio.sleep(0)
        return prompt[: max(len(prompt) // 2, 1)]

    retry = PTLRetry(truncate=truncate, max_retries=5)
    result = asyncio.run(retry.run_async(call_llm, "x" * 16))
    assert len(result) <= 4


def test_ptl_retry_async_exhausts_and_raises() -> None:
    """async 版用完 retry 額度也 re-raise 最後 error。"""

    async def call_llm(_prompt: str) -> str:
        raise RuntimeError("context window exceeded")

    retry = PTLRetry(
        truncate=lambda p, _e: p,
        max_retries=2,
    )
    with pytest.raises(RuntimeError, match="context window"):
        asyncio.run(retry.run_async(call_llm, "x"))


# ---------------------------------------------------------------------------
# 公開常數 sanity check
# ---------------------------------------------------------------------------


def test_default_ptl_patterns_is_non_empty_tuple() -> None:
    """DEFAULT_PTL_PATTERNS 該是非空 tuple 且元素全 str。"""
    assert isinstance(DEFAULT_PTL_PATTERNS, tuple)
    assert len(DEFAULT_PTL_PATTERNS) > 0
    assert all(isinstance(p, str) and p for p in DEFAULT_PTL_PATTERNS)
