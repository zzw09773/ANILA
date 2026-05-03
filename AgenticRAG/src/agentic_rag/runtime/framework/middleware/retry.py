"""RetryMiddleware — retry an Action with exponential backoff.

Retry on either:
  - an exception raised inside ``next_`` matching ``on_exceptions``
  - a returned ``ActionResult`` with ``is_error=True`` matching the
    ``retry_on_error`` predicate

Local-deployment use cases (ANILA's primary target):
  - vLLM hiccups during model warmup → retry once or twice
  - flaky cross-encoder reranker subprocess → retry with backoff
  - PgVector connection blip on cold pool → retry

Backoff strategy: exponential with jitter. Defaults are conservative
(1 retry, 250ms initial delay) so adding RetryMiddleware to an
existing chain doesn't dramatically change p99 latency for happy-path
calls.

This is NOT a circuit breaker — there's no global state across
retries. If you need to stop retrying after seeing N consecutive
failures across runs, layer that logic on top (or use a real circuit
breaker library). The middleware is per-call only.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Callable
from dataclasses import dataclass

from agentic_rag.runtime.framework.action import Action, ActionContext, ActionResult
from agentic_rag.runtime.framework.middleware.protocol import NextHandler

logger = logging.getLogger(__name__)


ErrorPredicate = Callable[[ActionResult], bool]
"""Returns ``True`` if the error result should be retried."""


# ── Retry policy ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class RetryPolicy:
    """How to retry. Defaults are tuned for local-service flakiness.

    ``max_attempts`` includes the initial attempt — so ``max_attempts=3``
    means up to 2 retries after the first call.

    ``initial_delay_seconds`` is the wait before the first retry;
    subsequent retries multiply by ``backoff_multiplier``. A small
    random jitter is added to avoid thundering herd on shared
    downstream services.

    ``max_delay_seconds`` caps the per-retry wait so a runaway
    multiplier doesn't park a request for minutes.
    """

    max_attempts: int = 2
    initial_delay_seconds: float = 0.25
    backoff_multiplier: float = 2.0
    max_delay_seconds: float = 8.0
    jitter: float = 0.1
    """Fraction of the computed delay added as ±jitter (0.1 = ±10%)."""

    def delay_for(self, attempt: int) -> float:
        """Compute the wait before retry ``attempt``-th time (1-indexed).

        ``attempt=1`` is the wait before the first retry.
        """
        base = self.initial_delay_seconds * (self.backoff_multiplier ** (attempt - 1))
        capped = min(base, self.max_delay_seconds)
        if self.jitter > 0:
            spread = capped * self.jitter
            capped += random.uniform(-spread, spread)
        return max(0.0, capped)


# ── Middleware ─────────────────────────────────────────────────────────


class RetryMiddleware:
    """Retry the inner handler on exception or matching error result.

    Construction:

        RetryMiddleware(
            policy=RetryPolicy(max_attempts=3),
            on_exceptions=(ConnectionError, TimeoutError),
            retry_on_error=lambda r: "rate limited" in (r.error or "").lower(),
        )

    If neither ``on_exceptions`` nor ``retry_on_error`` matches the
    failure, the original outcome propagates unchanged. Either argument
    can be omitted; passing neither makes the middleware a no-op (it
    still runs the handler, just doesn't retry anything).

    On retries that ultimately fail, the LAST observed error /
    exception is what propagates. Earlier attempts are logged at
    DEBUG.
    """

    def __init__(
        self,
        *,
        policy: RetryPolicy | None = None,
        on_exceptions: tuple[type[BaseException], ...] = (),
        retry_on_error: ErrorPredicate | None = None,
    ) -> None:
        self._policy = policy or RetryPolicy()
        self._on_exceptions = on_exceptions
        self._retry_on_error = retry_on_error

    async def __call__(
        self,
        action: Action,
        context: ActionContext,
        next_: NextHandler,
    ) -> ActionResult:
        last_exc: BaseException | None = None
        last_result: ActionResult | None = None

        for attempt in range(1, self._policy.max_attempts + 1):
            try:
                result = await next_(context)
            except self._on_exceptions as exc:
                last_exc = exc
                if attempt >= self._policy.max_attempts:
                    raise
                delay = self._policy.delay_for(attempt)
                logger.debug(
                    "retry %d/%d for %s after %s: %s (sleep %.3fs)",
                    attempt,
                    self._policy.max_attempts,
                    action.name,
                    type(exc).__name__,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)
                continue

            # Result-based retry: only kicks in if a predicate is set
            # and the result is an error matching it.
            if (
                result.is_error
                and self._retry_on_error is not None
                and self._retry_on_error(result)
            ):
                last_result = result
                if attempt >= self._policy.max_attempts:
                    return result
                delay = self._policy.delay_for(attempt)
                logger.debug(
                    "retry %d/%d for %s on error %r (sleep %.3fs)",
                    attempt,
                    self._policy.max_attempts,
                    action.name,
                    result.error,
                    delay,
                )
                await asyncio.sleep(delay)
                continue

            return result

        # Loop exhausted without an early return — the only way to
        # land here is exception path with all attempts raising.
        if last_exc is not None:
            raise last_exc
        # Or all attempts produced retryable errors and we ran out;
        # surface the last one.
        if last_result is not None:
            return last_result
        # Defensive: should never reach here given the loop body.
        return ActionResult(error="RetryMiddleware exhausted with no result")


__all__ = ["ErrorPredicate", "RetryMiddleware", "RetryPolicy"]
