"""MicroCompact — lightweight tool result clearing before full compaction.

Ported from Claude Code microCompact.ts.

Instead of summarizing the conversation (which requires an API call),
MicroCompact replaces old tool results from compactable tools with a
placeholder string. This frees tokens without triggering a compaction API call.

COMPACTABLE_TOOLS is the whitelist of tools whose results may be cleared.
Only results that are NOT in the most recently kept N results are cleared.
"""

from __future__ import annotations


from ..models.message import AssistantMessage, Message, UserMessage


TIME_BASED_MC_CLEARED_MESSAGE = "[Old tool result content cleared]"

COMPACTABLE_TOOLS: frozenset[str] = frozenset(
    {
        "file_read",
        "bash",
        "shell",
        "grep",
        "glob",
        "web_search",
        "web_fetch",
        "file_edit",
        "file_write",
        # Common aliases
        "read_file",
        "execute_bash",
        "search",
    }
)


def _collect_compactable_tool_ids(messages: list[Message]) -> list[str]:
    """Walk messages and collect tool_use IDs from COMPACTABLE_TOOLS, in order."""
    ids: list[str] = []
    for msg in messages:
        if not isinstance(msg, AssistantMessage):
            continue
        content = msg.content
        if not isinstance(content, list):
            continue
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_use"
                and block.get("name") in COMPACTABLE_TOOLS
            ):
                tool_id = block.get("id")
                if tool_id:
                    ids.append(tool_id)
    return ids


def _rough_token_count(text: str) -> int:
    """Very rough estimate: ~4 chars per token."""
    return max(1, len(text) // 4)


def micro_compact_messages(
    messages: list[Message],
    tool_results_to_clear: list[str],
) -> list[Message]:
    """Clear old tool results from COMPACTABLE_TOOLS.

    Replaces matching tool_result blocks' content with
    TIME_BASED_MC_CLEARED_MESSAGE. Returns a new list (immutable pattern).

    Args:
        messages: Current message history.
        tool_results_to_clear: Set of tool_use IDs whose results should be cleared.

    Returns:
        New message list with old tool results replaced.
    """
    if not tool_results_to_clear:
        return list(messages)

    clear_set = set(tool_results_to_clear)
    result: list[Message] = []

    for msg in messages:
        if not isinstance(msg, UserMessage):
            result.append(msg)
            continue

        content = msg.content
        if not isinstance(content, list):
            result.append(msg)
            continue

        touched = False
        new_content = []
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_result"
                and block.get("tool_use_id") in clear_set
                and block.get("content") != TIME_BASED_MC_CLEARED_MESSAGE
            ):
                new_content.append({**block, "content": TIME_BASED_MC_CLEARED_MESSAGE})
                touched = True
            else:
                new_content.append(block)

        if touched:
            result.append(UserMessage(
                uuid=msg.uuid,
                content=new_content,
                timestamp=msg.timestamp,
            ))
        else:
            result.append(msg)

    return result


def time_based_micro_compact(
    messages: list[Message],
    keep_recent: int = 3,
) -> tuple[list[Message], int]:
    """Clear all compactable tool results except the most recent N.

    This is the time-based variant — fired when a session has been idle
    and the server cache is cold. We clear old results now since they
    would be re-sent anyway.

    Args:
        messages: Current message history.
        keep_recent: Number of recent compactable tool results to preserve.

    Returns:
        (new_messages, tokens_saved) tuple.
    """
    compactable_ids = _collect_compactable_tool_ids(messages)
    if not compactable_ids:
        return list(messages), 0

    keep_n = max(1, keep_recent)
    keep_set = set(compactable_ids[-keep_n:])
    clear_set = [id_ for id_ in compactable_ids if id_ not in keep_set]

    if not clear_set:
        return list(messages), 0

    # Estimate tokens saved
    tokens_saved = 0
    for msg in messages:
        if not isinstance(msg, UserMessage):
            continue
        content = msg.content
        if not isinstance(content, list):
            continue
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_result"
                and block.get("tool_use_id") in set(clear_set)
            ):
                old_content = block.get("content", "")
                if isinstance(old_content, str):
                    tokens_saved += _rough_token_count(old_content)

    new_messages = micro_compact_messages(messages, clear_set)
    return new_messages, tokens_saved
