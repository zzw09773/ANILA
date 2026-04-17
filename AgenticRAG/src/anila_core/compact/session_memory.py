"""SessionMemoryService — background session fact extraction.

Ported from Claude Code sessionMemory.ts.

Session memory is a markdown file updated by a forked background agent
that reads the conversation and writes down key facts. It is kept across
compact operations (unlike the compressed summary which loses details).

Trigger conditions:
  1. INIT threshold: must have accumulated enough tokens before first extraction.
  2. UPDATE threshold: update every N model-visible messages.
  3. Only fires when last assistant turn has no pending tool calls.

The service runs sequentially (no overlapping extractions).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Callable, Coroutine, Optional

from ..context.agent_context import AgentContext, create_subagent_context
from ..models.message import AssistantMessage, Message, UserMessage

logger = logging.getLogger(__name__)

# Default thresholds
DEFAULT_MIN_TOKENS_TO_INIT = 10_000
DEFAULT_MIN_TOKENS_BETWEEN_UPDATES = 5_000
DEFAULT_TOOL_CALLS_BETWEEN_UPDATES = 10


@dataclass
class SessionMemoryConfig:
    """Configuration for session memory extraction thresholds."""

    minimum_tokens_to_init: int = DEFAULT_MIN_TOKENS_TO_INIT
    minimum_tokens_between_updates: int = DEFAULT_MIN_TOKENS_BETWEEN_UPDATES
    tool_calls_between_updates: int = DEFAULT_TOOL_CALLS_BETWEEN_UPDATES


@dataclass
class SessionMemoryState:
    """Mutable state for the session memory service."""

    initialized: bool = False
    last_extraction_tokens: int = 0
    last_message_uuid: Optional[str] = None
    in_progress: bool = False
    extraction_count: int = 0


def _count_model_visible_messages(messages: list[Message]) -> int:
    """Count user + assistant messages."""
    return sum(1 for m in messages if isinstance(m, (UserMessage, AssistantMessage)))


def _count_tool_calls_since(
    messages: list[Message], since_uuid: Optional[str]
) -> int:
    """Count tool_use blocks in assistant messages after since_uuid."""
    count = 0
    found = since_uuid is None

    for msg in messages:
        if not found:
            if hasattr(msg, "uuid") and msg.uuid == since_uuid:
                found = True
            continue

        if isinstance(msg, AssistantMessage):
            content = msg.content
            if isinstance(content, list):
                count += sum(
                    1 for b in content
                    if isinstance(b, dict) and b.get("type") == "tool_use"
                )

    return count


def _rough_token_count(messages: list[Message]) -> int:
    """Rough estimation using total text and tool_result content length / 4."""
    total = 0
    for msg in messages:
        if isinstance(msg, (UserMessage, AssistantMessage)):
            content = msg.content
            if isinstance(content, str):
                total += len(content)
            elif isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type", "")
                    if btype == "text":
                        total += len(block.get("text", ""))
                    elif btype == "tool_result":
                        c = block.get("content", "")
                        if isinstance(c, str):
                            total += len(c)
                        elif isinstance(c, list):
                            for sub in c:
                                if isinstance(sub, dict) and sub.get("type") == "text":
                                    total += len(sub.get("text", ""))
                    elif btype == "tool_use":
                        import json
                        total += len(block.get("name", ""))
                        try:
                            total += len(json.dumps(block.get("input", {})))
                        except (TypeError, ValueError):
                            pass
    return max(1, total // 4)


def _has_tool_calls_in_last_assistant_turn(messages: list[Message]) -> bool:
    """Return True if the most recent assistant message has tool calls."""
    for msg in reversed(messages):
        if isinstance(msg, AssistantMessage):
            return bool(msg.tool_calls)
    return False


class SessionMemoryService:
    """Background service that maintains session memory notes.

    Call should_extract() before extract() to check thresholds.
    Extraction runs sequentially; overlapping calls are no-ops.
    """

    def __init__(
        self,
        config: Optional[SessionMemoryConfig] = None,
    ) -> None:
        self._config = config or SessionMemoryConfig()
        self._state = SessionMemoryState()
        self._lock = asyncio.Lock()

    def should_extract(self, messages: list[Message]) -> bool:
        """Return True if extraction thresholds have been met."""
        current_tokens = _rough_token_count(messages)

        # INIT gate
        if not self._state.initialized:
            if current_tokens < self._config.minimum_tokens_to_init:
                return False
            self._state.initialized = True

        # Token update threshold
        token_delta = current_tokens - self._state.last_extraction_tokens
        has_met_token_threshold = token_delta >= self._config.minimum_tokens_between_updates

        # Tool call threshold
        tool_calls_since = _count_tool_calls_since(
            messages, self._state.last_message_uuid
        )
        has_met_tool_threshold = tool_calls_since >= self._config.tool_calls_between_updates

        # Do not extract if last turn still has pending tool calls
        has_tool_calls_in_last = _has_tool_calls_in_last_assistant_turn(messages)

        # Fire when (token AND tool) OR (token AND no pending tools)
        if has_met_token_threshold and has_met_tool_threshold:
            return True
        if has_met_token_threshold and not has_tool_calls_in_last:
            return True

        return False

    async def extract(
        self,
        messages: list[Message],
        context: AgentContext,
        run_extraction: Callable[[list[Message], AgentContext], Coroutine[Any, Any, str]],
    ) -> Optional[str]:
        """Run extraction if not already in progress.

        Args:
            messages: Current conversation history.
            context: Parent agent context (will be forked for extraction).
            run_extraction: Async callable that does the actual extraction.
                            Returns the extracted note text.

        Returns:
            Extracted note text, or None if skipped.
        """
        # Check-and-set atomically using the lock
        acquired = False
        async with self._lock:
            if self._state.in_progress:
                return None
            self._state.in_progress = True
            acquired = True

        if not acquired:
            return None

        try:
            # Fork context: restrict to read-only tools + memory writes
            forked = create_subagent_context(
                context,
                allowed_tools={"file_read", "grep", "glob", "bash_readonly"},
            )
            result = await run_extraction(messages, forked)

            # Advance state
            self._state.last_extraction_tokens = _rough_token_count(messages)
            last_msg = messages[-1] if messages else None
            if last_msg and hasattr(last_msg, "uuid"):
                self._state.last_message_uuid = last_msg.uuid
            self._state.extraction_count += 1
            return result

        except Exception as exc:
            logger.warning("SessionMemory extraction failed: %s", exc)
            return None

        finally:
            self._state.in_progress = False

    def reset(self) -> None:
        """Reset state (used in tests)."""
        self._state = SessionMemoryState()
