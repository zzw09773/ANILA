"""SlidingWindowCompact — Layer 3 hard truncation fallback.

When Layer 1 (micro_compact) and Layer 2 (auto_compact / LLM summary)
are insufficient or unavailable, this layer performs a hard sliding-window
truncation: keep the system message + the most recent N turns, drop everything
else.

This is the last-resort safety net to prevent OOM or context overflow.
"""

from __future__ import annotations

from typing import Optional

from ..models.message import Message, UserMessage


# Synthetic summary message inserted when old messages are dropped.
SLIDING_WINDOW_SUMMARY = "[Earlier conversation history was truncated to fit context limits.]"


def sliding_window_compact(
    messages: list[Message],
    max_tokens: int,
    token_estimator: Optional[callable] = None,
    keep_recent_turns: int = 4,
) -> tuple[list[Message], int]:
    """Hard-truncate old messages, keeping only recent turns.

    Args:
        messages: Full conversation history.
        max_tokens: Maximum token budget for the conversation.
        token_estimator: Callable(list[Message]) -> int. If None, uses
                         rough char/4 estimate.
        keep_recent_turns: Minimum number of user+assistant turn pairs to keep.

    Returns:
        (truncated_messages, tokens_dropped) tuple.
    """
    if not messages:
        return [], 0

    if token_estimator is None:
        token_estimator = _rough_token_count

    current_tokens = token_estimator(messages)
    if current_tokens <= max_tokens:
        return list(messages), 0

    # Separate system-role messages (keep unconditionally) from conversation
    system_msgs: list[Message] = []
    conversation: list[Message] = []
    for msg in messages:
        if _is_system_like(msg):
            system_msgs.append(msg)
        else:
            conversation.append(msg)

    # Find turn boundaries (a turn = contiguous user + assistant messages)
    turns = _split_into_turns(conversation)

    # Keep at least keep_recent_turns from the end
    min_keep = max(1, keep_recent_turns)
    kept_turns = turns[-min_keep:] if len(turns) > min_keep else list(turns)

    # Progressively add older turns back if they fit
    remaining_turns = turns[:-min_keep] if len(turns) > min_keep else []
    for turn in reversed(remaining_turns):
        candidate = system_msgs + _flatten_turns([turn] + kept_turns)
        if token_estimator(candidate) <= max_tokens:
            kept_turns = [turn] + kept_turns
        else:
            break

    # Build result
    kept_messages = _flatten_turns(kept_turns)

    # Insert a synthetic summary marker if we dropped anything
    dropped_count = len(conversation) - len(kept_messages)
    if dropped_count > 0:
        summary_msg = UserMessage(content=SLIDING_WINDOW_SUMMARY)
        result = system_msgs + [summary_msg] + kept_messages
    else:
        result = system_msgs + kept_messages

    tokens_before = current_tokens
    tokens_after = token_estimator(result)
    tokens_dropped = max(0, tokens_before - tokens_after)

    return result, tokens_dropped


def _is_system_like(msg: Message) -> bool:
    """Check if a message acts as a system prompt."""
    if isinstance(msg, UserMessage):
        content = msg.content
        if isinstance(content, str) and content.startswith("[歷史摘要]"):
            return True
    return False


def _split_into_turns(messages: list[Message]) -> list[list[Message]]:
    """Group messages into logical turns (user → assistant + tool results)."""
    turns: list[list[Message]] = []
    current_turn: list[Message] = []

    for msg in messages:
        if isinstance(msg, UserMessage) and current_turn:
            # Check if this user message is a tool result (part of current turn)
            content = msg.content
            is_tool_result = (
                isinstance(content, list)
                and any(
                    isinstance(b, dict) and b.get("type") == "tool_result"
                    for b in content
                )
            )
            if not is_tool_result:
                turns.append(current_turn)
                current_turn = []

        current_turn.append(msg)

    if current_turn:
        turns.append(current_turn)

    return turns


def _flatten_turns(turns: list[list[Message]]) -> list[Message]:
    """Flatten turn groups back into a flat message list."""
    result: list[Message] = []
    for turn in turns:
        result.extend(turn)
    return result


def _rough_token_count(messages: list[Message]) -> int:
    """Rough estimate: total chars / 4, padded by 4/3."""
    total = 0
    for msg in messages:
        content = getattr(msg, "content", None)
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    text = block.get("text", block.get("content", ""))
                    if isinstance(text, str):
                        total += len(text)
    return int((total / 4) * (4 / 3))
