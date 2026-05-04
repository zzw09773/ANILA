"""Relevance Selector — model-based memory selection via side query.

Ported from Claude Code findRelevantMemories.ts.

Uses a side query (separate API call with independent abort signal) to ask
the model which memories are relevant to the current query. Returns up to 5
memory headers sorted by relevance.

The RelevanceSelector is defined as a Protocol so it can be swapped for an
embedding-based implementation without changing callers.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable

from anila_core.models.memory import MemoryHeader
from anila_core.providers.base import Provider

logger = logging.getLogger(__name__)

MAX_RELEVANT_MEMORIES = 5
SIDE_QUERY_TIMEOUT = 10.0  # seconds

SELECT_MEMORIES_SYSTEM_PROMPT = """You are selecting memories that will be useful \
to an AI assistant as it processes a user's query. You will be given the user's \
query and a list of available memory files with their filenames and descriptions.

Return a JSON object with a "selected_memories" array containing filenames for \
memories that will clearly be useful (up to 5). Only include memories you are \
certain will be helpful based on their name and description.
- If unsure whether a memory will be useful, do not include it. Be selective.
- If no memories would clearly be useful, return an empty array.
- If a list of recently-used tools is provided, do not select memories that are \
usage reference or API documentation for those tools (the assistant is already \
using them). DO still select memories containing warnings, gotchas, or known issues.

Respond with only valid JSON matching: {"selected_memories": ["file1.md", "file2.md"]}
"""


@dataclass
class RelevantMemory:
    """A selected memory file with freshness metadata."""

    path: str
    mtime_ms: float
    filename: str = ""

    def __post_init__(self) -> None:
        if not self.filename:
            self.filename = os.path.basename(self.path)


@runtime_checkable
class RelevanceSelector(Protocol):
    """Protocol for memory relevance selection implementations."""

    async def select(
        self,
        query: str,
        memory_headers: list[MemoryHeader],
        recent_tools: list[str],
        already_surfaced: set[str],
        abort_event: Optional[asyncio.Event],
    ) -> list[RelevantMemory]:
        """Select up to MAX_RELEVANT_MEMORIES from memory_headers.

        Args:
            query: The current user query.
            memory_headers: All available memory headers.
            recent_tools: Tools recently used in the conversation.
            already_surfaced: Filenames already shown to the model.
            abort_event: Optional signal to cancel the selection.

        Returns:
            List of up to 5 RelevantMemory records.
        """
        ...  # pragma: no cover


def format_memory_manifest(headers: list[MemoryHeader]) -> str:
    """Format memory headers as a text manifest for the selector prompt."""
    lines = []
    for h in headers:
        lines.append(h.format_manifest_line())
    return "\n".join(lines)


class ModelBasedRelevanceSelector:
    """Selects relevant memories by asking the model via a side query.

    This is the initial implementation — the Protocol allows future
    replacement with embedding-based selection.
    """

    def __init__(self, provider: Provider) -> None:
        self._provider = provider

    async def select(
        self,
        query: str,
        memory_headers: list[MemoryHeader],
        recent_tools: list[str],
        already_surfaced: set[str],
        abort_event: Optional[asyncio.Event] = None,
    ) -> list[RelevantMemory]:
        """Select memories via a model side query."""
        # Filter already surfaced
        candidates = [h for h in memory_headers if h.filename not in already_surfaced]
        if not candidates:
            return []

        manifest = format_memory_manifest(candidates)
        tools_section = ""
        if recent_tools:
            tools_section = f"\n\nRecently used tools: {', '.join(recent_tools)}"

        user_content = f"Query: {query}\n\nAvailable memories:\n{manifest}{tools_section}"

        selected_filenames = await self._side_query(
            user_content, candidates, abort_event
        )

        # Map filenames back to MemoryHeader records
        by_filename = {h.filename: h for h in candidates}
        results: list[RelevantMemory] = []
        for fname in selected_filenames[:MAX_RELEVANT_MEMORIES]:
            header = by_filename.get(fname)
            if header:
                results.append(
                    RelevantMemory(
                        path=header.file_path,
                        mtime_ms=header.mtime_ms,
                        filename=fname,
                    )
                )
        return results

    async def _side_query(
        self,
        user_content: str,
        candidates: list[MemoryHeader],
        abort_event: Optional[asyncio.Event],
    ) -> list[str]:
        """Run the side query with timeout and abort signal support."""
        valid_filenames = {h.filename for h in candidates}

        async def _do_query() -> list[str]:
            from anila_core.providers.base import ProviderRequest
            from anila_core.models.message import UserMessage

            request = ProviderRequest(
                model="",  # caller should set; provider may override
                system=SELECT_MEMORIES_SYSTEM_PROMPT,
                messages=[UserMessage(content=user_content)],
                max_tokens=256,
                temperature=0.0,
            )

            full_text = ""
            async for delta in self._provider.stream_completion(request):
                if delta.type == "text" and delta.text:
                    full_text += delta.text
                if abort_event and abort_event.is_set():
                    return []

            try:
                data = json.loads(full_text.strip())
                filenames = data.get("selected_memories", [])
                return [f for f in filenames if f in valid_filenames]
            except (json.JSONDecodeError, AttributeError):
                logger.warning("[RelevanceSelector] JSON parse failed: %r", full_text[:200])
                return []

        try:
            if abort_event and abort_event.is_set():
                return []
            return await asyncio.wait_for(_do_query(), timeout=SIDE_QUERY_TIMEOUT)
        except asyncio.TimeoutError:
            logger.debug("[RelevanceSelector] side query timed out")
            return []
        except Exception as exc:
            logger.warning("[RelevanceSelector] side query failed: %s", exc)
            return []
