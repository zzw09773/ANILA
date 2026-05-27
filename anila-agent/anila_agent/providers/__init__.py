"""anila-agent providers sub-package。

集中放「跟外部 LLM / HTTP provider 互動的可重用元件」。Stage 1 只有 retry
framework;之後若要新增 rate-limit guard、circuit breaker、provider-specific
adapter,也歸在這。

公開介面:
    RetryDecision: 單次 ``should_retry`` 回傳的決策(retry 與否 + delay + reason)。
    RetryPolicy: ``should_retry(error, attempt) -> RetryDecision`` Protocol。
    RetryPolicies: 內建 policy factory(network_error / retry_after / rate_limit
        / combined / ptl)。
    with_retry: sync / async 雙模式 decorator,套用 policy 自動 retry 並開
        tracing child span。
    PTLRetryPolicy: 把既有 ``core.token_budget.PTLRetry`` 對齊 RetryPolicy
        interface 的 adapter。

整合點:
- 與 P0-1 hooks:retry 時上層可在 hook callback 內讀 RetryDecision。
- 與 P0-9 tracing:每次 retry 自動開 ``retry.attempt.N`` child span(若 caller
  傳入 tracer 或當 thread 已有 active trace 時)。
- 與 P1-9 token_budget.PTLRetry:見 :class:`PTLRetryPolicy` 適配層。
"""

from anila_agent.providers.retry import (
    PTLRetryPolicy,
    RetryDecision,
    RetryPolicies,
    RetryPolicy,
    with_retry,
)

__all__ = [
    "PTLRetryPolicy",
    "RetryDecision",
    "RetryPolicies",
    "RetryPolicy",
    "with_retry",
]
