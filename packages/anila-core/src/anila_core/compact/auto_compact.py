"""AutoCompact — threshold-based conversation compaction.

Ported from Claude Code autoCompact.ts.

Formula:
  effective_context_window = context_window - reserve_for_output
  threshold = effective_context_window - AUTOCOMPACT_BUFFER_TOKENS

When current_tokens >= threshold, compaction should be triggered.
"""

from __future__ import annotations

from ..models.message import AssistantMessage, Message, UserMessage
from .strip_images import estimate_message_tokens_with_images


# Reserve this many tokens for the compaction summary output.
# Based on p99.99 of compact summary output being ~17,387 tokens.
MAX_OUTPUT_TOKENS_FOR_SUMMARY = 20_000

# Additional buffer between threshold and effective context window.
AUTOCOMPACT_BUFFER_TOKENS = 13_000

# model_registry.context_window 為空時與 CSP 附件預算同一後援。
FALLBACK_CONTEXT_WINDOW = 128_000

# Warning thresholds
WARNING_THRESHOLD_BUFFER_TOKENS = 20_000
ERROR_THRESHOLD_BUFFER_TOKENS = 20_000


def rough_token_count(messages: list[Message]) -> int:
    """Rough token estimation: text chars/4 plus clamped image tokens.

    Pads text by 4/3 to be conservative. ``data:`` / ``image_url`` parts
    use the 800–2000 clamp in :mod:`strip_images` so a screenshot cannot
    be estimated as 0.
    """
    text_and_images = estimate_message_tokens_with_images(messages)
    extra = 0
    for msg in messages:
        content = None
        if isinstance(msg, (UserMessage, AssistantMessage)):
            content = msg.content
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            import json
            extra += len(block.get("name", ""))
            try:
                extra += len(json.dumps(block.get("input", {})))
            except (TypeError, ValueError):
                pass
    return text_and_images + int((extra / 4) * (4 / 3)) if extra else text_and_images


def _clamped_reserve_and_buffer(
    context_window: int,
    max_output_tokens: int,
) -> tuple[int, int]:
    """Keep reserve+buffer from swallowing small context windows."""
    window = max(int(context_window), 1)
    reserve = min(int(max_output_tokens), MAX_OUTPUT_TOKENS_FOR_SUMMARY)
    reserve = min(reserve, max(1, window // 4))
    buffer = min(AUTOCOMPACT_BUFFER_TOKENS, max(1, window // 8))
    return reserve, buffer


def get_effective_context_window(
    context_window: int,
    max_output_tokens: int = MAX_OUTPUT_TOKENS_FOR_SUMMARY,
) -> int:
    """Return context window minus reserved output space."""
    reserve, _ = _clamped_reserve_and_buffer(context_window, max_output_tokens)
    return max(1, int(context_window) - reserve)


def get_auto_compact_threshold(
    context_window: int,
    max_output_tokens: int = MAX_OUTPUT_TOKENS_FOR_SUMMARY,
) -> int:
    """Return the token count at which autocompaction should trigger."""
    reserve, buffer = _clamped_reserve_and_buffer(context_window, max_output_tokens)
    return max(1, int(context_window) - reserve - buffer)


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
