"""CostMiddleware — token-count tracking + optional tool-side budget gate.

**ANILA deployment reality:** the platform runs **only against local
models** (vLLM / NIM / TGI / Ollama on-prem). LLM inference has no
per-token dollar cost and no rate limit; operators pay for hardware.
For ANILA itself, the primary value of this middleware is:

1. **Token-count tracking per model** — capacity planning, GPU
   utilisation reports, "which agent burns the most context window"
   answers. Always recorded, regardless of pricing.
2. **Tool-side dollar budget caps** — useful when an Action wraps a
   paid third-party API (e.g. an external reranker subscription, a
   hosted vector DB add-on). The Action declares
   ``cost_estimate.dollars`` and the middleware refuses to run it
   if the projected total exceeds ``CostBudget.hard_cap_dollars``.

The LLM-side dollar attribution path (``PriceTable`` /
``record_llm_usage_from_run``) exists for other framework consumers
who DO route through cloud APIs. For ANILA itself, leave the
``PriceTable`` empty — unknown models default to zero, the helpers
become free no-ops, and only token totals get accumulated.

The cloud-default tables (``DEFAULT_OPENAI_PRICES`` /
``DEFAULT_ANTHROPIC_PRICES``) are reference examples for
non-ANILA forks; ANILA itself never imports them.

Rate limiting is intentionally NOT a middleware in v0.1 — local
deployments don't need it, and cloud SDKs (used by other framework
consumers) already retry 429s in their own retry layers.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from agentic_rag.runtime.framework.action import Action, ActionContext, ActionResult
from agentic_rag.runtime.framework.exceptions import AgentsException
from agentic_rag.runtime.framework.items import MessageOutputItem, RunItem
from agentic_rag.runtime.framework.middleware.protocol import NextHandler
from agentic_rag.runtime.framework.usage import Usage

logger = logging.getLogger(__name__)


# ── Price table ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ModelPrice:
    """Per-model token price.

    Units are USD per 1,000,000 tokens — the convention every public
    cloud LLM price page uses, so callers can paste numbers from a
    pricing page directly without conversion gymnastics.

    ``cached_input_per_million`` is the discounted rate for prompt
    cache hits (OpenAI: 50% off, Anthropic: 90% off). Defaults to
    ``input_per_million`` if not set.
    """

    input_per_million: float
    output_per_million: float
    cached_input_per_million: float | None = None

    def cost_for_usage(self, usage: Usage) -> float:
        """Dollar cost for one ``Usage`` aggregate.

        Subtracts cached tokens from the regular input bucket so the
        cached portion gets billed at the discounted rate.
        """
        cached = usage.input_tokens_details.cached_tokens or 0
        regular_input = max(0, usage.input_tokens - cached)
        cached_rate = (
            self.cached_input_per_million
            if self.cached_input_per_million is not None
            else self.input_per_million
        )
        return (
            regular_input * self.input_per_million / 1_000_000
            + cached * cached_rate / 1_000_000
            + usage.output_tokens * self.output_per_million / 1_000_000
        )


class PriceTable:
    """Lookup table from model name → ``ModelPrice``.

    Lookup semantics:
      - exact match wins
      - falls back to longest-matching prefix (so ``gpt-4o-mini-2024-07-18``
        resolves to a ``gpt-4o-mini`` entry)
      - missing → ``None``, NOT an exception. Callers (and CostMiddleware)
        treat ``None`` as "free / unpriced" so local models just work.
    """

    def __init__(self, prices: dict[str, ModelPrice] | None = None) -> None:
        self._exact: dict[str, ModelPrice] = dict(prices or {})

    def add(self, model: str, price: ModelPrice) -> None:
        self._exact[model] = price

    def update(self, prices: dict[str, ModelPrice]) -> None:
        self._exact.update(prices)

    def get(self, model: str) -> ModelPrice | None:
        if model in self._exact:
            return self._exact[model]
        # Longest-prefix fallback. Sort keys by length desc so
        # ``gpt-4o-mini-2024`` matches before ``gpt-4o`` if both are
        # present.
        for key in sorted(self._exact, key=len, reverse=True):
            if model.startswith(key):
                return self._exact[key]
        return None

    def __contains__(self, model: object) -> bool:
        return isinstance(model, str) and self.get(model) is not None


# ── Default reference tables (cloud only — local stays empty by design) ──


DEFAULT_OPENAI_PRICES: dict[str, ModelPrice] = {
    "gpt-4o": ModelPrice(2.50, 10.00, cached_input_per_million=1.25),
    "gpt-4o-mini": ModelPrice(0.15, 0.60, cached_input_per_million=0.075),
    "gpt-4-turbo": ModelPrice(10.00, 30.00),
    "gpt-3.5-turbo": ModelPrice(0.50, 1.50),
}
"""Reference rates as of late 2024. Operators should override with
their own contract pricing — these are public list prices and may be
out of date."""


DEFAULT_ANTHROPIC_PRICES: dict[str, ModelPrice] = {
    "claude-opus-4": ModelPrice(15.00, 75.00, cached_input_per_million=1.50),
    "claude-sonnet-4": ModelPrice(3.00, 15.00, cached_input_per_million=0.30),
    "claude-haiku-4": ModelPrice(0.80, 4.00, cached_input_per_million=0.08),
}


# ── Tracker ────────────────────────────────────────────────────────────


@dataclass
class CostTracker:
    """Per-run accumulator for tool + LLM cost.

    Mutable; one tracker per run is the typical pattern. Re-using a
    tracker across runs aggregates them, useful for "total spend this
    session" dashboards.

    Fields:
      - ``total_dollars`` — running sum
      - ``by_action`` — name → dollars (tool side)
      - ``by_model`` — model → dollars (LLM side)
      - ``token_totals`` — by_model → ``Usage`` aggregate (so reports can
        show tokens-only attribution for unpriced local models)
    """

    total_dollars: float = 0.0
    by_action: dict[str, float] = field(default_factory=dict)
    by_model: dict[str, float] = field(default_factory=dict)
    token_totals: dict[str, Usage] = field(default_factory=dict)

    def record_action_cost(self, action_name: str, dollars: float) -> None:
        if dollars <= 0:
            return
        self.by_action[action_name] = self.by_action.get(action_name, 0.0) + dollars
        self.total_dollars += dollars

    def record_llm_usage(
        self, model: str, usage: Usage, prices: PriceTable
    ) -> None:
        """Attribute one LLM call's tokens (and cost, if priced) to ``model``."""
        if model not in self.token_totals:
            self.token_totals[model] = Usage()
        self.token_totals[model].add(usage)

        price = prices.get(model)
        if price is None:
            return  # local / unpriced — tokens recorded, cost stays zero
        dollars = price.cost_for_usage(usage)
        if dollars <= 0:
            return
        self.by_model[model] = self.by_model.get(model, 0.0) + dollars
        self.total_dollars += dollars


# ── Budget ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CostBudget:
    """Hard ceiling on dollar spend across a run.

    When ``hard_cap_dollars`` is set, ``CostMiddleware`` raises
    ``CostBudgetExceeded`` *before* invoking an Action whose
    ``cost_estimate.dollars`` would push the run past the cap. The
    Action handler never runs — useful for stopping a recursive
    handoff before it starts billing.

    Budget enforcement is best-effort and depends on accurate
    ``cost_estimate`` declarations on Actions and on LLM cost being
    attributed mid-run (which the middleware does not do — see the
    ``record_llm_usage_from_run`` helper). Pure post-run spend
    calculation is exact; pre-run gating is conservative on tool side
    and silent on LLM side. Operators wanting hard pre-LLM-call gates
    should run a separate budget service in front of the gateway.
    """

    hard_cap_dollars: float | None = None
    soft_warn_dollars: float | None = None


class CostBudgetExceeded(AgentsException):
    """Raised when a CostMiddleware budget gate fires."""

    def __init__(self, action_name: str, projected: float, cap: float) -> None:
        self.action_name = action_name
        self.projected = projected
        self.cap = cap
        super().__init__(
            f"Action {action_name!r} would push run cost to "
            f"${projected:.4f}, exceeding cap ${cap:.4f}"
        )


# ── Middleware ─────────────────────────────────────────────────────────


WarningCallback = Callable[[str, float, float], Awaitable[None]]
"""Called with ``(action_name, projected_dollars, soft_warn_dollars)``
when a soft warning threshold is crossed for the first time."""


class CostMiddleware:
    """Tool-side cost attribution + optional pre-run budget gate.

    Wrap your runner with this once; the middleware records each
    Action's declared ``cost_estimate.dollars`` to the tracker after a
    successful run. If a ``CostBudget`` is set, the middleware checks
    the projected cost (current total + this Action's estimate) before
    invoking the handler and raises ``CostBudgetExceeded`` if the cap
    would be breached.

    LLM-call cost is attributed separately — call
    ``record_llm_usage_from_run(tracker, prices, run_result)`` after
    ``Runner.run`` returns to walk the ``MessageOutputItem`` audit
    trail and accumulate LLM costs into the same tracker.
    """

    def __init__(
        self,
        tracker: CostTracker,
        *,
        budget: CostBudget | None = None,
        on_soft_warn: WarningCallback | None = None,
    ) -> None:
        self._tracker = tracker
        self._budget = budget or CostBudget()
        self._on_soft_warn = on_soft_warn
        self._soft_warned = False

    async def __call__(
        self,
        action: Action,
        context: ActionContext,
        next_: NextHandler,
    ) -> ActionResult:
        estimate = action.cost_estimate.dollars or 0.0
        projected = self._tracker.total_dollars + estimate

        # Hard cap → refuse to run.
        cap = self._budget.hard_cap_dollars
        if cap is not None and projected > cap:
            raise CostBudgetExceeded(action.name, projected, cap)

        # Soft warn → fire callback once when crossing.
        warn_at = self._budget.soft_warn_dollars
        if (
            warn_at is not None
            and not self._soft_warned
            and projected > warn_at
            and self._on_soft_warn is not None
        ):
            self._soft_warned = True
            try:
                await self._on_soft_warn(action.name, projected, warn_at)
            except Exception:  # noqa: BLE001
                logger.exception("on_soft_warn callback failed")

        result = await next_(context)

        # Attribute the estimate only on success — failed Actions
        # often didn't actually spend the resource (the third-party
        # API returned 4xx before billing). Conservative.
        if not result.is_error and estimate > 0:
            self._tracker.record_action_cost(action.name, estimate)

        return result


# ── Run-result post-processing helper ──────────────────────────────────


def record_llm_usage_from_run(
    tracker: CostTracker,
    prices: PriceTable,
    items: list[RunItem],
    *,
    model: str,
) -> None:
    """Walk a run's ``MessageOutputItem`` items and attribute LLM cost.

    Call after ``Runner.run`` returns:

        result = await Runner(middleware=[CostMiddleware(tracker)]).run(agent, ...)
        record_llm_usage_from_run(tracker, prices, result.items, model=agent.model)

    Stage B's runner doesn't expose per-LLM-call model (handoffs may
    switch models mid-run), so the helper takes ``model`` as a
    parameter. v0.2 will widen ``MessageOutputItem`` with the issuing
    model so this helper can route per-call to the right price entry.
    """
    for item in items:
        if isinstance(item, MessageOutputItem):
            tracker.record_llm_usage(model, item.usage, prices)


__all__ = [
    "CostBudget",
    "CostBudgetExceeded",
    "CostMiddleware",
    "CostTracker",
    "DEFAULT_ANTHROPIC_PRICES",
    "DEFAULT_OPENAI_PRICES",
    "ModelPrice",
    "PriceTable",
    "record_llm_usage_from_run",
]
