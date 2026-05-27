"""Unit tests:P2-2 RetryPolicy framework。

涵蓋
====

* :class:`RetryDecision` 建構與 ``yes`` / ``no`` sugar。
* ``RetryPolicies.network_error``:
  - HTTP 5xx → retry,exponential backoff delay 隨 attempt 增加。
  - HTTP 4xx → 不 retry。
  - ConnectionError / TimeoutError / OSError → retry。
  - 超過 ``max_retries`` → 不 retry(reason 帶 max_retries 數字)。
* ``RetryPolicies.retry_after``:
  - 從 ``error.response.headers['Retry-After']`` 讀(httpx 形態)。
  - 從 ``error.headers`` 讀(error 直接帶 headers)。
  - 從 ``error.retry_after`` 屬性讀(SDK 已 normalize)。
  - 無 header → 不 retry。
* ``RetryPolicies.rate_limit``:
  - HTTP 429 + Retry-After → 用 header delay。
  - HTTP 429 無 header → exponential backoff。
  - 非 429 → 不 retry。
* ``RetryPolicies.combined``:
  - 任一 policy 回 yes 就 retry,delay 取第一個 yes。
  - 全部 no 就 no(reason 取最後一個 no)。
* :func:`with_retry` decorator:
  - sync function:retry 成功後 return,policy 拒絕後 re-raise。
  - async function:同上。
  - 用 inject 的 fake sleep 確認真的 sleep 對的秒數。
  - tracing span 結構:每次 retry 開一個 ``retry.attempt.N`` span。
* :class:`PTLRetryPolicy` adapter:
  - PTL error → yes,並呼叫 truncate;非 PTL → no。
  - 超過 max_retries → no。
  - 對齊 RetryPolicy Protocol(可塞 combined / with_retry)。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from anila_agent.providers import (
    PTLRetryPolicy,
    RetryDecision,
    RetryPolicies,
    RetryPolicy,
    with_retry,
)
from anila_agent.tracing import JsonlTracingProcessor, Tracer

# ---------------------------------------------------------------------------
# 共用 fake error helpers
# ---------------------------------------------------------------------------


@dataclass
class _FakeHeaders:
    """模擬 dict-like response.headers,支援 ``.get(key)`` 與 ``[key]``。"""

    data: dict[str, Any]

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self.data[key]


@dataclass
class _FakeResponse:
    """模擬 httpx / requests response 物件。"""

    status_code: int
    headers: _FakeHeaders


class _FakeHttpError(Exception):
    """模擬 httpx.HTTPStatusError shape:有 ``status_code`` + ``response``。"""

    def __init__(
        self,
        status_code: int,
        retry_after: float | None = None,
        message: str = "fake http error",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        headers: dict[str, Any] = {}
        if retry_after is not None:
            headers["Retry-After"] = retry_after
        self.response = _FakeResponse(
            status_code=status_code,
            headers=_FakeHeaders(headers),
        )


class _FakeConnectionError(ConnectionError):
    """繼承 ConnectionError;_is_network_error 應認得。"""


# ---------------------------------------------------------------------------
# RetryDecision
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_retry_decision_yes_sugar() -> None:
    """``RetryDecision.yes()`` 應回 should_retry=True,可帶 delay / reason。"""
    decision = RetryDecision.yes(delay_seconds=1.5, reason="test")
    assert decision.should_retry is True
    assert decision.delay_seconds == 1.5
    assert decision.reason == "test"


@pytest.mark.unit
def test_retry_decision_no_sugar() -> None:
    """``RetryDecision.no()`` 應 should_retry=False、delay=0。"""
    decision = RetryDecision.no(reason="nope")
    assert decision.should_retry is False
    assert decision.delay_seconds == 0.0
    assert decision.reason == "nope"


@pytest.mark.unit
def test_retry_decision_is_frozen() -> None:
    """RetryDecision 是 frozen dataclass,不可 mutate。"""
    from dataclasses import FrozenInstanceError

    decision = RetryDecision.yes()
    with pytest.raises(FrozenInstanceError):
        decision.should_retry = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# network_error policy
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_network_error_policy_retries_5xx() -> None:
    """HTTP 5xx 系列 error 應 retry,delay 走 exponential backoff。"""
    policy = RetryPolicies.network_error(max_retries=3, base_delay=1.0)
    error = _FakeHttpError(status_code=503)

    d1 = policy.should_retry(error, attempt=1)
    assert d1.should_retry is True
    assert d1.delay_seconds == 1.0  # base_delay * 2^0

    d2 = policy.should_retry(error, attempt=2)
    assert d2.should_retry is True
    assert d2.delay_seconds == 2.0  # base_delay * 2^1

    d3 = policy.should_retry(error, attempt=3)
    assert d3.delay_seconds == 4.0  # base_delay * 2^2


@pytest.mark.unit
def test_network_error_policy_does_not_retry_4xx() -> None:
    """HTTP 4xx 不該 retry(client error)。"""
    policy = RetryPolicies.network_error()
    error = _FakeHttpError(status_code=400)
    decision = policy.should_retry(error, attempt=1)
    assert decision.should_retry is False
    assert "not a retriable" in decision.reason


@pytest.mark.unit
def test_network_error_policy_retries_connection_error() -> None:
    """ConnectionError subclass 走 ``_is_network_error`` 判定,該 retry。"""
    policy = RetryPolicies.network_error(max_retries=2, base_delay=0.5)
    error = _FakeConnectionError("connection refused")
    decision = policy.should_retry(error, attempt=1)
    assert decision.should_retry is True
    assert decision.delay_seconds == 0.5
    assert "_FakeConnectionError" in decision.reason or "Connection" in decision.reason


@pytest.mark.unit
def test_network_error_policy_retries_timeout_error() -> None:
    """built-in TimeoutError 也算 network error。"""
    policy = RetryPolicies.network_error()
    error = TimeoutError("slow")
    decision = policy.should_retry(error, attempt=1)
    assert decision.should_retry is True


@pytest.mark.unit
def test_network_error_policy_respects_max_retries() -> None:
    """attempt 超過 max_retries 後一律 no。"""
    policy = RetryPolicies.network_error(max_retries=2)
    error = _FakeHttpError(status_code=503)
    assert policy.should_retry(error, attempt=2).should_retry is True
    decision = policy.should_retry(error, attempt=3)
    assert decision.should_retry is False
    assert "max_retries=2" in decision.reason


@pytest.mark.unit
def test_network_error_policy_caps_max_delay() -> None:
    """exponential backoff 不超過 max_delay。"""
    policy = RetryPolicies.network_error(
        max_retries=10, base_delay=1.0, max_delay=5.0
    )
    error = _FakeHttpError(status_code=500)
    # attempt=10 算出來會是 512s,被 cap 在 5.0
    decision = policy.should_retry(error, attempt=10)
    assert decision.delay_seconds == 5.0


# ---------------------------------------------------------------------------
# retry_after policy
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_retry_after_reads_response_headers() -> None:
    """``error.response.headers['Retry-After']`` 應被讀到。"""
    policy = RetryPolicies.retry_after()
    error = _FakeHttpError(status_code=429, retry_after=3)
    decision = policy.should_retry(error, attempt=1)
    assert decision.should_retry is True
    assert decision.delay_seconds == 3.0


@pytest.mark.unit
def test_retry_after_reads_direct_attribute() -> None:
    """error.retry_after 屬性也該被認。"""
    policy = RetryPolicies.retry_after()

    class _ErrWithRetryAfter(Exception):
        retry_after = 7.5

    decision = policy.should_retry(_ErrWithRetryAfter("x"), attempt=1)
    assert decision.should_retry is True
    assert decision.delay_seconds == 7.5


@pytest.mark.unit
def test_retry_after_reads_direct_headers() -> None:
    """error.headers(沒走 response 包裝)也該被讀到。"""
    policy = RetryPolicies.retry_after()

    class _ErrWithHeaders(Exception):
        headers = _FakeHeaders({"Retry-After": 4})

    decision = policy.should_retry(_ErrWithHeaders("x"), attempt=1)
    assert decision.should_retry is True
    assert decision.delay_seconds == 4.0


@pytest.mark.unit
def test_retry_after_no_header_means_no_retry() -> None:
    """無 header 就 no(留給 combined 裡其他 policy 處理)。"""
    policy = RetryPolicies.retry_after()
    error = _FakeHttpError(status_code=500)  # 無 Retry-After
    decision = policy.should_retry(error, attempt=1)
    assert decision.should_retry is False
    assert "no Retry-After" in decision.reason


@pytest.mark.unit
def test_retry_after_respects_max_retries() -> None:
    """attempt 超過 max_retries 一律 no(即便 header 有寫)。"""
    policy = RetryPolicies.retry_after(max_retries=2)
    error = _FakeHttpError(status_code=429, retry_after=1)
    assert policy.should_retry(error, attempt=2).should_retry is True
    assert policy.should_retry(error, attempt=3).should_retry is False


# ---------------------------------------------------------------------------
# rate_limit policy
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_rate_limit_uses_retry_after_header_when_present() -> None:
    """HTTP 429 + Retry-After header → 用 header delay,不算 backoff。"""
    policy = RetryPolicies.rate_limit(base_delay=2.0)
    error = _FakeHttpError(status_code=429, retry_after=10)
    decision = policy.should_retry(error, attempt=1)
    assert decision.should_retry is True
    assert decision.delay_seconds == 10.0
    assert "Retry-After" in decision.reason


@pytest.mark.unit
def test_rate_limit_falls_back_to_exp_backoff() -> None:
    """HTTP 429 沒 header → exponential backoff。"""
    policy = RetryPolicies.rate_limit(base_delay=2.0, max_delay=60.0)
    error = _FakeHttpError(status_code=429)
    d1 = policy.should_retry(error, attempt=1)
    d2 = policy.should_retry(error, attempt=2)
    assert d1.delay_seconds == 2.0
    assert d2.delay_seconds == 4.0


@pytest.mark.unit
def test_rate_limit_does_not_retry_non_429() -> None:
    """non-429 不歸 rate_limit policy 管。"""
    policy = RetryPolicies.rate_limit()
    error = _FakeHttpError(status_code=500)
    decision = policy.should_retry(error, attempt=1)
    assert decision.should_retry is False
    assert "not HTTP 429" in decision.reason


# ---------------------------------------------------------------------------
# combined policy
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_combined_any_policy_yes_triggers_retry() -> None:
    """任一 policy 回 yes 就 retry。"""
    policy = RetryPolicies.combined(
        RetryPolicies.rate_limit(),
        RetryPolicies.network_error(),
    )
    error = _FakeHttpError(status_code=500)  # rate_limit no, network_error yes
    decision = policy.should_retry(error, attempt=1)
    assert decision.should_retry is True
    assert "network_error" in decision.reason


@pytest.mark.unit
def test_combined_returns_first_yes_delay() -> None:
    """順序很重要 — 第一個回 yes 的 policy 的 delay 勝出。"""
    error = _FakeHttpError(status_code=429, retry_after=20)

    policy_a = RetryPolicies.combined(
        RetryPolicies.retry_after(),  # 會 yes,delay=20
        RetryPolicies.rate_limit(),  # 也會 yes,但 delay 是 exp backoff(2)
    )
    decision_a = policy_a.should_retry(error, attempt=1)
    assert decision_a.delay_seconds == 20.0

    policy_b = RetryPolicies.combined(
        RetryPolicies.rate_limit(),  # 會優先用 Retry-After header(yes, delay=20)
        RetryPolicies.retry_after(),
    )
    decision_b = policy_b.should_retry(error, attempt=1)
    assert decision_b.delay_seconds == 20.0


@pytest.mark.unit
def test_combined_all_no_returns_no() -> None:
    """全部 policy 都 no → combined 也 no。"""
    policy = RetryPolicies.combined(
        RetryPolicies.rate_limit(),
        RetryPolicies.retry_after(),
    )
    error = _FakeHttpError(status_code=400)  # 不是 429,沒 header
    decision = policy.should_retry(error, attempt=1)
    assert decision.should_retry is False


@pytest.mark.unit
def test_combined_empty_policies_returns_no() -> None:
    """空 policy list 視為「沒設」→ no。"""
    policy = RetryPolicies.combined()
    error = Exception("x")
    decision = policy.should_retry(error, attempt=1)
    assert decision.should_retry is False


# ---------------------------------------------------------------------------
# with_retry — sync
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_with_retry_sync_succeeds_after_one_failure() -> None:
    """第一次失敗、第二次成功;decorator 應透明傳回成功值。"""
    policy = RetryPolicies.network_error(max_retries=3, base_delay=0.01)
    slept: list[float] = []
    state = {"calls": 0}

    @with_retry(policy, sleep=slept.append)
    def call() -> str:
        state["calls"] += 1
        if state["calls"] == 1:
            raise _FakeHttpError(status_code=500)
        return "ok"

    assert call() == "ok"
    assert state["calls"] == 2
    assert slept == [0.01]  # 一次 sleep,值 = base_delay * 2^0


@pytest.mark.unit
def test_with_retry_sync_reraises_when_policy_says_no() -> None:
    """policy 回 no(例如 4xx)應直接 re-raise。"""
    policy = RetryPolicies.network_error()

    @with_retry(policy, sleep=lambda _: None)
    def call() -> str:
        raise _FakeHttpError(status_code=400)

    with pytest.raises(_FakeHttpError):
        call()


@pytest.mark.unit
def test_with_retry_sync_reraises_after_max_retries() -> None:
    """連續失敗超過 max_retries 後應 re-raise 最後一個 error。"""
    policy = RetryPolicies.network_error(max_retries=2, base_delay=0.0)
    state = {"calls": 0}

    @with_retry(policy, sleep=lambda _: None)
    def call() -> str:
        state["calls"] += 1
        raise _FakeHttpError(status_code=500)

    with pytest.raises(_FakeHttpError):
        call()
    # max_retries=2 → policy 在 attempt=3 回 no;total calls = 3
    assert state["calls"] == 3


# ---------------------------------------------------------------------------
# with_retry — async
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_with_retry_async_succeeds_after_failure() -> None:
    """async function 的 retry path 應同樣行為。"""
    policy = RetryPolicies.network_error(max_retries=3, base_delay=0.01)
    slept: list[float] = []

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)

    state = {"calls": 0}

    @with_retry(policy, async_sleep=fake_sleep)
    async def call() -> str:
        state["calls"] += 1
        if state["calls"] < 3:
            raise _FakeHttpError(status_code=502)
        return "ok-async"

    result = await call()
    assert result == "ok-async"
    assert state["calls"] == 3
    assert slept == [0.01, 0.02]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_with_retry_async_reraises() -> None:
    """async path 拒絕 retry 後 raise 一致。"""
    policy = RetryPolicies.rate_limit()  # 對 4xx 一律 no

    async def fake_sleep(_: float) -> None:
        return None

    @with_retry(policy, async_sleep=fake_sleep)
    async def call() -> None:
        raise _FakeHttpError(status_code=400)

    with pytest.raises(_FakeHttpError):
        await call()


@pytest.mark.unit
def test_with_retry_preserves_function_metadata() -> None:
    """``functools.wraps`` 應保留 ``__name__`` / ``__doc__``。"""
    policy = RetryPolicies.network_error()

    @with_retry(policy)
    def my_func() -> int:
        """do thing"""
        return 1

    assert my_func.__name__ == "my_func"
    assert my_func.__doc__ == "do thing"


# ---------------------------------------------------------------------------
# tracing integration
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_with_retry_emits_tracing_spans(tmp_path: Any) -> None:
    """每次 retry 都該開一個 ``retry.attempt.N`` span。"""
    tracer = Tracer()
    jsonl_path = tmp_path / "trace.jsonl"
    tracer.register_processor(JsonlTracingProcessor(jsonl_path))

    policy = RetryPolicies.network_error(max_retries=3, base_delay=0.0)
    state = {"calls": 0}

    @with_retry(policy, tracer=tracer, sleep=lambda _: None)
    def call() -> str:
        state["calls"] += 1
        if state["calls"] < 3:
            raise _FakeHttpError(status_code=503)
        return "ok"

    with tracer.start_trace("test.retry") as _:
        result = call()
        assert result == "ok"

    # 讀 JSONL 撈 span 名字
    import json
    lines = [
        json.loads(line)
        for line in jsonl_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    span_names = [
        ev["name"]
        for ev in lines
        if ev.get("event") == "span.end"
    ]
    # 應該看到 attempt.1, attempt.2 兩個 span(第 3 次 call 成功,沒進 except)
    assert "retry.attempt.1" in span_names
    assert "retry.attempt.2" in span_names
    assert "retry.attempt.3" not in span_names


@pytest.mark.unit
def test_with_retry_tracing_span_attributes(tmp_path: Any) -> None:
    """retry span 的 attributes 應該帶 attempt / decision / error 資訊。"""
    tracer = Tracer()
    jsonl_path = tmp_path / "trace2.jsonl"
    tracer.register_processor(JsonlTracingProcessor(jsonl_path))

    policy = RetryPolicies.network_error(max_retries=1, base_delay=0.0)

    @with_retry(policy, tracer=tracer, sleep=lambda _: None)
    def call() -> str:
        raise _FakeHttpError(status_code=502, message="bad gateway")

    with tracer.start_trace("test.span_attrs"), pytest.raises(_FakeHttpError):
        call()

    import json
    lines = [
        json.loads(line)
        for line in jsonl_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    retry_spans = [
        ev
        for ev in lines
        if ev.get("event") == "span.end" and ev.get("name", "").startswith("retry.attempt.")
    ]
    assert len(retry_spans) == 2  # attempt 1(yes)+ attempt 2(no, max_retries=1)
    # attempt 1:should_retry=True
    assert retry_spans[0]["attributes"]["retry.attempt"] == 1
    assert retry_spans[0]["attributes"]["retry.should_retry"] is True
    assert retry_spans[0]["attributes"]["retry.error_type"] == "_FakeHttpError"
    assert "bad gateway" in retry_spans[0]["attributes"]["retry.error_message"]
    # attempt 2:max_retries 用盡,should_retry=False
    assert retry_spans[1]["attributes"]["retry.should_retry"] is False


@pytest.mark.unit
def test_with_retry_without_tracer_works() -> None:
    """tracer=None 不該影響 retry 行為(best-effort tracing)。"""
    policy = RetryPolicies.network_error(max_retries=2, base_delay=0.0)
    state = {"calls": 0}

    @with_retry(policy, sleep=lambda _: None)  # tracer 預設 None
    def call() -> str:
        state["calls"] += 1
        if state["calls"] == 1:
            raise _FakeHttpError(status_code=500)
        return "ok"

    assert call() == "ok"


# ---------------------------------------------------------------------------
# PTLRetryPolicy adapter
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_ptl_policy_retries_on_ptl_error_and_calls_truncate() -> None:
    """PTL error 應 yes,並呼叫 truncate;truncate 結果寫到 last_truncated。"""
    calls: list[tuple[Any, BaseException]] = []

    def truncate(prompt: Any, error: BaseException) -> str:
        calls.append((prompt, error))
        return "shortened"

    policy = PTLRetryPolicy(truncate=truncate, max_retries=3)
    policy.last_truncated = "original prompt"

    error = Exception("prompt is too long for context window")
    decision = policy.should_retry(error, attempt=1)
    assert decision.should_retry is True
    assert policy.last_truncated == "shortened"
    assert calls == [("original prompt", error)]


@pytest.mark.unit
def test_ptl_policy_does_not_retry_non_ptl() -> None:
    """非 PTL error 一律 no。"""
    policy = PTLRetryPolicy(truncate=lambda p, e: p)
    decision = policy.should_retry(Exception("random error"), attempt=1)
    assert decision.should_retry is False
    assert "not a prompt-too-long" in decision.reason


@pytest.mark.unit
def test_ptl_policy_respects_max_retries() -> None:
    """attempt 超 max_retries → no(reason 帶數字)。"""
    policy = PTLRetryPolicy(truncate=lambda p, e: p, max_retries=2)
    error = Exception("context length exceeded")
    assert policy.should_retry(error, attempt=2).should_retry is True
    decision = policy.should_retry(error, attempt=3)
    assert decision.should_retry is False
    assert "max_retries=2" in decision.reason


@pytest.mark.unit
def test_ptl_policy_implements_retry_policy_protocol() -> None:
    """PTLRetryPolicy 應 satisfy RetryPolicy Protocol(runtime_checkable)。"""
    policy = PTLRetryPolicy(truncate=lambda p, e: p)
    assert isinstance(policy, RetryPolicy)


@pytest.mark.unit
def test_ptl_policy_factory_returns_protocol_compatible() -> None:
    """``RetryPolicies.ptl(...)`` factory 也該回 RetryPolicy。"""
    policy = RetryPolicies.ptl(truncate=lambda p, e: p, max_retries=5)
    assert isinstance(policy, RetryPolicy)
    assert isinstance(policy, PTLRetryPolicy)
    assert policy.max_retries == 5


@pytest.mark.unit
def test_ptl_policy_works_inside_combined() -> None:
    """PTLRetryPolicy 跟其他 policy 串成 combined 也該正常運作。"""
    policy = RetryPolicies.combined(
        RetryPolicies.ptl(truncate=lambda p, e: "shorter"),
        RetryPolicies.network_error(),
    )
    # PTL error → ptl yes
    decision_a = policy.should_retry(
        Exception("maximum context length is 4096"), attempt=1
    )
    assert decision_a.should_retry is True
    # 5xx → network_error yes(ptl no)
    decision_b = policy.should_retry(_FakeHttpError(status_code=503), attempt=1)
    assert decision_b.should_retry is True


@pytest.mark.unit
def test_ptl_policy_with_retry_decorator() -> None:
    """``with_retry(PTLRetryPolicy)`` 應正確 retry + truncate。"""
    truncate_log: list[Any] = []

    def truncate(prompt: Any, error: BaseException) -> str:
        new = f"shorter-{len(truncate_log)}"
        truncate_log.append(new)
        return new

    policy = PTLRetryPolicy(truncate=truncate, max_retries=3)
    policy.last_truncated = "initial"

    state = {"calls": 0}

    @with_retry(policy, sleep=lambda _: None)
    def call() -> str:
        state["calls"] += 1
        if state["calls"] < 3:
            raise Exception("context length too large")
        return policy.last_truncated  # type: ignore[no-any-return]

    result = call()
    # call 1 fail → truncate 一次,call 2 fail → truncate 兩次,call 3 success
    assert state["calls"] == 3
    assert len(truncate_log) == 2
    assert result == truncate_log[-1]


# ---------------------------------------------------------------------------
# 確保整個 module export 對齊 __all__
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_providers_package_exports() -> None:
    """anila_agent.providers 應 export 預期符號。"""
    from anila_agent import providers

    assert hasattr(providers, "RetryPolicy")
    assert hasattr(providers, "RetryDecision")
    assert hasattr(providers, "RetryPolicies")
    assert hasattr(providers, "with_retry")
    assert hasattr(providers, "PTLRetryPolicy")


@pytest.mark.unit
def test_anila_agent_root_exports_providers() -> None:
    """頂層 anila_agent 也該能直接 import providers。"""
    import anila_agent

    assert hasattr(anila_agent, "providers")
