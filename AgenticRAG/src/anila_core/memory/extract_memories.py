"""Memory Extraction — post-turn hook that saves persistent memories.

Ported from Claude Code extractMemories.ts.

Triggers:
  - Fires after each turn when the model produces a final answer (no tool calls).
  - Only processes messages NEW since the last extraction (cursor tracking).
  - Skips if main agent already wrote to the memory dir this turn.
  - Background agent has restricted tool set: reads + memory-dir writes only.

The forked agent reads the existing memory manifest, then decides what to
write / update in the memory directory.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, Optional

from ..context.agent_context import AgentContext, create_subagent_context
from ..models.message import AssistantMessage, Message, UserMessage

logger = logging.getLogger(__name__)

EXTRACTION_ALLOWED_TOOLS = frozenset(
    {
        "file_read",
        "grep",
        "glob",
        "bash_readonly",
        "file_edit",   # restricted to memory dir by caller
        "file_write",  # restricted to memory dir by caller
    }
)

EXTRACT_PROMPT_TEMPLATE = """You are now acting as the memory extraction subagent. \
Analyze the most recent ~{new_message_count} messages above and use them to update \
your persistent memory systems.

Available tools: file_read, grep, glob, read-only bash (ls/find/cat/stat/wc/head/tail \
and similar), and file_edit/file_write for paths inside the memory directory only. \
bash rm is not permitted.

You have a limited turn budget. file_edit requires a prior file_read of the same file, \
so the efficient strategy is: turn 1 - issue all file_read calls in parallel for every \
file you might update; turn 2 - issue all file_write/file_edit calls in parallel.

You MUST only use content from the last ~{new_message_count} messages to update your \
persistent memories. Do not waste turns investigating or verifying content further.
{existing_memories_section}

## Memory types
- user_preference: How the user likes to work, communication style, tooling preferences
- project_convention: Patterns, architecture decisions, coding standards specific to the project
- debugging_lesson: Root causes discovered, debugging approaches that worked or failed
- api_pattern: API usage patterns, gotchas, version-specific behavior

## What NOT to save
- Content derivable from reading the codebase (architecture, code patterns)
- Temporary debugging notes that won't apply in other sessions
- Routine task completions without lessons learned

## How to save memories
Saving a memory is a two-step process:

Step 1 - write the memory to its own file (e.g. user_role.md) with this frontmatter:
---
title: Short title
description: One-line description of what this memory contains
type: user_preference|project_convention|debugging_lesson|api_pattern
tags: [optional, list]
created: YYYY-MM-DD
scope: project
---

Memory content here.

Step 2 - add a pointer in MEMORY.md: `- [Title](file.md) - one-line hook`
"""


def _is_model_visible(msg: Message) -> bool:
    return isinstance(msg, (UserMessage, AssistantMessage))


def _count_model_visible_since(messages: list[Message], since_uuid: Optional[str]) -> int:
    if since_uuid is None:
        return sum(1 for m in messages if _is_model_visible(m))

    found = False
    count = 0
    for msg in messages:
        if not found:
            if hasattr(msg, "uuid") and msg.uuid == since_uuid:
                found = True
            continue
        if _is_model_visible(msg):
            count += 1

    # Fallback: if UUID disappeared (compaction), count all
    return count if found else sum(1 for m in messages if _is_model_visible(m))


def _has_memory_writes_since(
    messages: list[Message],
    since_uuid: Optional[str],
    memory_dir: str,
) -> bool:
    """Return True if any assistant message after since_uuid wrote to memory_dir."""
    found = since_uuid is None
    for msg in messages:
        if not found:
            if hasattr(msg, "uuid") and msg.uuid == since_uuid:
                found = True
            continue
        if not isinstance(msg, AssistantMessage):
            continue
        content = msg.content
        if not isinstance(content, list):
            continue
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_use"
                and block.get("name") in {"file_edit", "file_write"}
            ):
                fp = block.get("input", {}).get("file_path", "")
                if isinstance(fp, str) and fp.startswith(memory_dir):
                    return True
    return False


RunForkedAgentFn = Callable[
    [list[Message], AgentContext, str],
    Coroutine[Any, Any, list[Message]],
]


@dataclass
class MemoryExtractor:
    """Post-turn hook that extracts persistent memories via a forked agent.

    State is instance-level (not module-level) so tests can use fresh instances.
    """

    memory_dir: str
    last_message_uuid: Optional[str] = field(default=None, repr=False)
    _in_progress: bool = field(default=False, repr=False, init=False)
    _pending_context: Optional[Any] = field(default=None, repr=False, init=False)
    _turns_since_last: int = field(default=0, repr=False, init=False)
    throttle_every_n_turns: int = 1

    async def run(
        self,
        messages: list[Message],
        context: AgentContext,
        run_forked: RunForkedAgentFn,
        existing_memories: str = "",
    ) -> list[str]:
        """Run memory extraction. Returns list of written file paths.

        Args:
            messages: Current conversation history.
            context: Parent agent context.
            run_forked: Async function that actually runs the forked agent.
            existing_memories: Pre-formatted memory manifest (optional).

        Returns:
            List of file paths written by the extraction agent.
        """
        if self._in_progress:
            logger.debug("[MemoryExtractor] already in progress, skipping")
            return []

        # Skip if main agent already wrote memories this turn
        if _has_memory_writes_since(messages, self.last_message_uuid, self.memory_dir):
            logger.debug("[MemoryExtractor] skipping — main agent already wrote memories")
            last = messages[-1] if messages else None
            if last and hasattr(last, "uuid"):
                self.last_message_uuid = last.uuid
            return []

        # Throttle: only run every N turns
        self._turns_since_last += 1
        if self._turns_since_last < self.throttle_every_n_turns:
            return []
        self._turns_since_last = 0

        new_count = _count_model_visible_since(messages, self.last_message_uuid)
        if new_count == 0:
            return []

        self._in_progress = True
        try:
            # Build extraction prompt
            mem_section = ""
            if existing_memories:
                mem_section = (
                    "\n\n## Existing memory files\n\n"
                    + existing_memories
                    + "\n\nCheck this list before writing — update an existing file "
                    "rather than creating a duplicate."
                )

            prompt = EXTRACT_PROMPT_TEMPLATE.format(
                new_message_count=new_count,
                existing_memories_section=mem_section,
            )

            # Fork context with restricted tools
            forked = create_subagent_context(
                context,
                allowed_tools=set(EXTRACTION_ALLOWED_TOOLS),
            )

            agent_messages = await run_forked(messages, forked, prompt)

            # Collect written paths from the agent's output
            written = _extract_written_paths(agent_messages, self.memory_dir)

            # Advance cursor
            last = messages[-1] if messages else None
            if last and hasattr(last, "uuid"):
                self.last_message_uuid = last.uuid

            return written

        except Exception as exc:
            logger.warning("[MemoryExtractor] extraction failed: %s", exc)
            return []

        finally:
            self._in_progress = False

    def reset(self) -> None:
        """Reset extraction state (for tests)."""
        self.last_message_uuid = None
        self._in_progress = False
        self._turns_since_last = 0


def _extract_written_paths(messages: list[Message], memory_dir: str) -> list[str]:
    """Collect file paths written by the extraction agent."""
    paths: list[str] = []
    seen: set[str] = set()
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
                and block.get("name") in {"file_edit", "file_write"}
            ):
                fp = block.get("input", {}).get("file_path", "")
                if isinstance(fp, str) and fp.startswith(memory_dir) and fp not in seen:
                    paths.append(fp)
                    seen.add(fp)
    return paths
