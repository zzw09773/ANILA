"""AutoCompact — threshold-based conversation compaction.

Ported from Claude Code autoCompact.ts.

Formula:
  effective_context_window = context_window - reserve_for_output
  threshold = effective_context_window - AUTOCOMPACT_BUFFER_TOKENS

When current_tokens >= threshold, compaction should be triggered.
"""

from __future__ import annotations

from ..models.message import AssistantMessage, Message, UserMessage


# Reserve this many tokens for the compaction summary output.
# Based on p99.99 of compact summary output being ~17,387 tokens.
MAX_OUTPUT_TOKENS_FOR_SUMMARY = 20_000

# Additional buffer between threshold and effective context window.
AUTOCOMPACT_BUFFER_TOKENS = 13_000

# Warning thresholds
WARNING_THRESHOLD_BUFFER_TOKENS = 20_000
ERROR_THRESHOLD_BUFFER_TOKENS = 20_000


def rough_token_count(messages: list[Message]) -> int:
    """Rough token estimation: sum of all text content lengths / 4.

    Pads by 4/3 to be conservative (we are approximating).
    """
    total = 0
    for msg in messages:
        content = None
        if isinstance(msg, (UserMessage, AssistantMessage)):
            content = msg.content

        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    total += len(block.get("text", ""))
                elif block.get("type") == "tool_result":
                    c = block.get("content", "")
                    if isinstance(c, str):
                        total += len(c)
                elif block.get("type") == "tool_use":
                    import json
                    total += len(block.get("name", ""))
                    try:
                        total += len(json.dumps(block.get("input", {})))
                    except (TypeError, ValueError):
                        pass

    # ~4 chars per token, pad by 4/3
    return int((total / 4) * (4 / 3))


def get_effective_context_window(
    context_window: int,
    max_output_tokens: int = MAX_OUTPUT_TOKENS_FOR_SUMMARY,
) -> int:
    """Return context window minus reserved output space."""
    reserve = min(max_output_tokens, MAX_OUTPUT_TOKENS_FOR_SUMMARY)
    return context_window - reserve


def get_auto_compact_threshold(
    context_window: int,
    max_output_tokens: int = MAX_OUTPUT_TOKENS_FOR_SUMMARY,
) -> int:
    """Return the token count at which autocompaction should trigger."""
    effective = get_effective_context_window(context_window, max_output_tokens)
    return effective - AUTOCOMPACT_BUFFER_TOKENS


def should_compact(
    context_window: int,
    current_tokens: int,
    max_output_tokens: int = MAX_OUTPUT_TOKENS_FOR_SUMMARY,
) -> bool:
    """Return True if the context has grown large enough to warrant compaction.

    Args:
        context_window: Total context window size for the model.
        current_tokens: Estimated current token usage.
        max_output_tokens: Reserved tokens for the compaction summary.
    """
    threshold = get_auto_compact_threshold(context_window, max_output_tokens)
    return current_tokens >= threshold


def calculate_token_warning_state(
    token_usage: int,
    context_window: int,
    max_output_tokens: int = MAX_OUTPUT_TOKENS_FOR_SUMMARY,
) -> dict:
    """Return a dict with warning/error/compact flags and percent remaining."""
    threshold = get_auto_compact_threshold(context_window, max_output_tokens)

    percent_left = max(
        0, round(((threshold - token_usage) / max(1, threshold)) * 100)
    )
    warning_threshold = threshold - WARNING_THRESHOLD_BUFFER_TOKENS
    error_threshold = threshold - ERROR_THRESHOLD_BUFFER_TOKENS

    return {
        "percent_left": percent_left,
        "is_above_warning_threshold": token_usage >= warning_threshold,
        "is_above_error_threshold": token_usage >= error_threshold,
        "is_above_autocompact_threshold": token_usage >= threshold,
    }
