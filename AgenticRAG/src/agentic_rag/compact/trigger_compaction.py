"""LLM-driven Layer-2 compaction — summarise old turns, keep recent.

Layer 1 (``micro_compact``) trims whitespace and metadata. Layer 3
(``sliding_window``) hard-truncates as a last-resort safety net. This
module is **Layer 2**: the LLM-assisted summary that strips the bulk
of the cost while keeping enough context for the next turn to feel
continuous.

Strategy:

1. Decide WHEN to compact: ``should_compact(messages, model)`` checks
   whether estimated tokens have crossed the auto-compact threshold
   for the agent's model (using ``ModelWindowTable``).
2. Decide WHAT to compact: split history into "old" (everything older
   than the last ``keep_recent_turns`` turns) and "recent". The recent
   slice stays at full fidelity; the old slice goes to the LLM.
3. Run the summary call: a caller-supplied ``SummariseFn`` (the same
   provider the agent uses, or a smaller / cheaper local model) takes
   the old slice and returns a single-paragraph summary.
4. Splice: replace the old slice with one synthetic ``UserMessage``
   carrying the summary, prefixed by a marker token so downstream
   compaction layers know not to compact the summary again.

What this module does NOT do:

- Stream compaction across many small calls — one summary call per
  trigger, sized so it fits in the model's own context.
- Persist compaction state — each call is self-contained. The
  caller's session loop decides when to call.
- Re-rank or reorder — strict chronological. If you need a smarter
  ordering, layer it on top with a separate module.

The LLM call is injected as a callable so this module stays
provider-agnostic and testable without spinning up a real model.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Optional

from ..models.message import AssistantMessage, Message, UserMessage
from .auto_compact import rough_token_count, should_compact
from .model_windows import ModelWindowTable
from .sliding_window import _split_into_turns

logger = logging.getLogger(__name__)


COMPACT_SUMMARY_MARKER = "[歷史摘要]"
"""Prefix on the synthetic summary message. Matches the marker used
by ``sliding_window._is_system_like`` so subsequent compactions
recognise the marker and don't try to recompact a summary."""


SummariseFn = Callable[[list[Message], str], Awaitable[str]]
"""Caller-provided async function: ``(messages_to_summarise, hint) -> summary_text``.
Implementations typically call the same LLM the agent uses — or a
smaller / cheaper local model dedicated to summarisation.
``hint`` is a short string the trigger passes describing the goal
(e.g., ``"Summarise these N turns; preserve user intent and key
results"``); the implementation may inject it into its prompt."""


_DEFAULT_SUMMARY_HINT = (
    "Summarise the conversation below into a single dense paragraph. "
    "Preserve the user's stated goals, decisions made, and any concrete "
    "facts or identifiers (file paths, ids, parameters) that later turns "
    "may need to refer back to. Drop pleasantries and routine status text."
)


# ── Decision ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CompactionDecision:
    """Plan emitted by ``plan_compaction`` — describes what would happen.

    ``compact_indices`` are positions in the input ``messages`` list
    that would be replaced by the summary; ``keep_indices`` survive at
    full fidelity. Empty lists indicate "no compaction needed."
    """

    should_compact: bool
    compact_indices: list[int]
    keep_indices: list[int]
    estimated_tokens_before: int
    threshold_tokens: int


def plan_compaction(
    messages: list[Message],
    model: str,
    *,
    window_table: ModelWindowTable | None = None,
    keep_recent_turns: int = 4,
) -> CompactionDecision:
    """Decide which messages would be compacted, without running the LLM.

    Useful for dashboards / dry-run mode. The actual compaction goes
    through ``run_compaction`` which calls this internally.
    """
    table = window_table or ModelWindowTable()
    window = table.get(model)
    estimated = rough_token_count(messages)
    threshold = _threshold_for(window)

    if not should_compact(window, estimated):
        return CompactionDecision(
            should_compact=False,
            compact_indices=[],
            keep_indices=list(range(len(messages))),
            estimated_tokens_before=estimated,
            threshold_tokens=threshold,
        )

    # Build the (compact, keep) split using turn boundaries.
    compact_idx, keep_idx = _split_indices_by_turn(messages, keep_recent_turns)
    return CompactionDecision(
        should_compact=True,
        compact_indices=compact_idx,
        keep_indices=keep_idx,
        estimated_tokens_before=estimated,
        threshold_tokens=threshold,
    )


# ── Execution ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CompactionResult:
    """What ``run_compaction`` produced.

    ``new_messages`` is the rewritten history ready to send to the
    next LLM turn. ``summary_text`` is the LLM's output verbatim
    (without the marker prefix), useful for logging / display.
    ``tokens_before`` / ``tokens_after`` track how much was saved.
    Includes ``skipped_reason`` when compaction was decided against;
    callers should branch on ``compacted`` when reading the result.
    """

    compacted: bool
    new_messages: list[Message]
    summary_text: str
    tokens_before: int
    tokens_after: int
    skipped_reason: Optional[str] = None


async def run_compaction(
    messages: list[Message],
    model: str,
    summarise: SummariseFn,
    *,
    window_table: ModelWindowTable | None = None,
    keep_recent_turns: int = 4,
    summary_hint: str | None = None,
) -> CompactionResult:
    """Compact ``messages`` if past threshold; otherwise return unchanged.

    Failure mode: any exception inside ``summarise`` aborts compaction
    and returns the original messages with ``skipped_reason="summarise
    failed: ..."``. Compaction is always optional from the caller's
    perspective — the agent loop should run with the original history
    if the LLM summary call goes wrong.
    """
    decision = plan_compaction(
        messages,
        model,
        window_table=window_table,
        keep_recent_turns=keep_recent_turns,
    )

    if not decision.should_compact:
        return CompactionResult(
            compacted=False,
            new_messages=list(messages),
            summary_text="",
            tokens_before=decision.estimated_tokens_before,
            tokens_after=decision.estimated_tokens_before,
            skipped_reason="below_threshold",
        )

    if not decision.compact_indices:
        return CompactionResult(
            compacted=False,
            new_messages=list(messages),
            summary_text="",
            tokens_before=decision.estimated_tokens_before,
            tokens_after=decision.estimated_tokens_before,
            skipped_reason="nothing_to_compact",
        )

    to_summarise = [messages[i] for i in decision.compact_indices]
    to_keep = [messages[i] for i in decision.keep_indices]

    try:
        summary_text = await summarise(
            to_summarise, summary_hint or _DEFAULT_SUMMARY_HINT
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("compaction summarise call failed: %s", exc)
        return CompactionResult(
            compacted=False,
            new_messages=list(messages),
            summary_text="",
            tokens_before=decision.estimated_tokens_before,
            tokens_after=decision.estimated_tokens_before,
            skipped_reason=f"summarise_failed: {type(exc).__name__}: {exc}",
        )

    summary_text = (summary_text or "").strip()
    if not summary_text:
        return CompactionResult(
            compacted=False,
            new_messages=list(messages),
            summary_text="",
            tokens_before=decision.estimated_tokens_before,
            tokens_after=decision.estimated_tokens_before,
            skipped_reason="summarise_returned_empty",
        )

    summary_msg = UserMessage(
        content=f"{COMPACT_SUMMARY_MARKER} {summary_text}"
    )
    new_messages: list[Message] = [summary_msg, *to_keep]
    return CompactionResult(
        compacted=True,
        new_messages=new_messages,
        summary_text=summary_text,
        tokens_before=decision.estimated_tokens_before,
        tokens_after=rough_token_count(new_messages),
    )


# ── helpers ───────────────────────────────────────────────────────────


def _threshold_for(window: int) -> int:
    """Wrap auto_compact's threshold formula for the planner's reporting."""
    from .auto_compact import get_auto_compact_threshold

    return get_auto_compact_threshold(window)


def _split_indices_by_turn(
    messages: list[Message], keep_recent_turns: int
) -> tuple[list[int], list[int]]:
    """Return ``(compact_indices, keep_indices)`` honoring turn boundaries.

    A "turn" here matches ``sliding_window._split_into_turns`` —
    contiguous user (or tool-result) + assistant messages. We never
    split a turn across the compact/keep boundary; that would leave
    orphan tool-result messages without their tool-call assistant
    message.

    Existing summary markers (recognised by ``COMPACT_SUMMARY_MARKER``
    prefix) are kept on the "keep" side so we don't compact a summary.
    """
    if not messages:
        return [], []

    turns = _split_into_turns(messages)
    if len(turns) <= keep_recent_turns:
        # Not enough history to compact anything meaningful. Defer.
        return [], list(range(len(messages)))

    compact_turns = turns[:-keep_recent_turns]

    # Map turns back to original indices. _split_into_turns preserves
    # message identity, so we can rebuild positions by iterating both
    # the original list and the turn groups.
    compact_set: set[int] = set()
    pos = 0
    for turn in compact_turns:
        for _msg in turn:
            # Prior summary markers should NOT be compacted again.
            if isinstance(_msg, UserMessage) and _is_summary_marker(_msg):
                pass
            else:
                compact_set.add(pos)
            pos += 1

    keep_indices = [i for i in range(len(messages)) if i not in compact_set]
    compact_indices = sorted(compact_set)
    return compact_indices, keep_indices


def _is_summary_marker(msg: Message) -> bool:
    if isinstance(msg, AssistantMessage):
        return False
    content = getattr(msg, "content", None)
    if isinstance(content, str):
        return content.startswith(COMPACT_SUMMARY_MARKER)
    return False


__all__ = [
    "COMPACT_SUMMARY_MARKER",
    "CompactionDecision",
    "CompactionResult",
    "SummariseFn",
    "plan_compaction",
    "run_compaction",
]
