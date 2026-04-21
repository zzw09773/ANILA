"""BudgetTracker - diminishing-returns detection for the query loop.

Ported directly from Claude Code tokenBudget.ts.

COMPLETION_THRESHOLD = 0.9   (90 percent of budget used -> stop continuing)
DIMINISHING_THRESHOLD = 500  (delta < 500 tokens = not making progress)

Diminishing returns fires when ALL of:
  - continuation_count >= 3
  - delta since last check < 500 tokens
  - last_delta_tokens < 500 tokens
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional, Union


COMPLETION_THRESHOLD = 0.9
DIMINISHING_THRESHOLD = 500


def get_budget_continuation_message(pct: int, turn_tokens: int, budget: int) -> str:
    """Generate a nudge message to inject into the conversation.

    Tells the model how much of its budget it has used so it can
    modulate its output length accordingly.
    """
    remaining_pct = 100 - pct
    return (
        f"You have used approximately {pct}% of your output budget "
        f"({turn_tokens}/{budget} tokens). "
        f"Approximately {remaining_pct}% remains. "
        "Please plan your remaining response accordingly - wrap up if the work "
        "is complete, or focus on the most important remaining steps."
    )


@dataclass
class BudgetTracker:
    """Mutable state for tracking token budget across continuations."""

    continuation_count: int = 0
    last_delta_tokens: int = 0
    last_global_turn_tokens: int = 0
    started_at: float = field(default_factory=time.time)


@dataclass
class ContinueDecision:
    action: str = "continue"
    nudge_message: str = ""
    continuation_count: int = 0
    pct: int = 0
    turn_tokens: int = 0
    budget: int = 0


@dataclass
class StopDecision:
    action: str = "stop"
    continuation_count: int = 0
    pct: int = 0
    turn_tokens: int = 0
    budget: int = 0
    diminishing_returns: bool = False
    duration_ms: float = 0.0
    has_event: bool = False


TokenBudgetDecision = Union[ContinueDecision, StopDecision]


def check_token_budget(
    tracker: BudgetTracker,
    agent_id: Optional[str],
    budget: Optional[int],
    global_turn_tokens: int,
) -> TokenBudgetDecision:
    """Decide whether the query loop should continue or stop.

    Args:
        tracker: Mutable budget state (mutated in-place on continue).
        agent_id: If set, this is a subagent - always stop (no budget pressure).
        budget: Token budget for this turn. None or 0 means no limit.
        global_turn_tokens: Total tokens consumed this turn so far.

    Returns:
        ContinueDecision with a nudge message, or StopDecision.
    """
    if agent_id or budget is None or budget <= 0:
        return StopDecision(has_event=False)

    turn_tokens = global_turn_tokens
    pct = round((turn_tokens / budget) * 100)
    delta_since_last_check = global_turn_tokens - tracker.last_global_turn_tokens

    is_diminishing = (
        tracker.continuation_count >= 3
        and delta_since_last_check < DIMINISHING_THRESHOLD
        and tracker.last_delta_tokens < DIMINISHING_THRESHOLD
    )

    if not is_diminishing and turn_tokens < budget * COMPLETION_THRESHOLD:
        # Continue: update tracker state in-place
        tracker.continuation_count += 1
        tracker.last_delta_tokens = delta_since_last_check
        tracker.last_global_turn_tokens = global_turn_tokens
        return ContinueDecision(
            action="continue",
            nudge_message=get_budget_continuation_message(pct, turn_tokens, budget),
            continuation_count=tracker.continuation_count,
            pct=pct,
            turn_tokens=turn_tokens,
            budget=budget,
        )

    if is_diminishing or tracker.continuation_count > 0:
        return StopDecision(
            action="stop",
            continuation_count=tracker.continuation_count,
            pct=pct,
            turn_tokens=turn_tokens,
            budget=budget,
            diminishing_returns=is_diminishing,
            duration_ms=(time.time() - tracker.started_at) * 1000,
            has_event=True,
        )

    return StopDecision(has_event=False)
