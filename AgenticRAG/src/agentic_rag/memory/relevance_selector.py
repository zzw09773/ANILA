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

from ..models.memory import MemoryHeader
from ..providers.base import Provider

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
    """Selects relevant memories by asking a model via a side query.

    Initial implementation; the ``RelevanceSelector`` Protocol allows
    future replacement with embedding-based selection.

    For ANILA's local-only deployment, point ``model`` at a
    Haiku-class on-prem inference endpoint (vLLM with a small model
    runs this query in <500ms; reusing the agent's main 70B model
    burns capacity and latency for what is essentially a routing
    decision).
    """

    def __init__(self, provider: Provider, *, model: str) -> None:
        if not model:
            raise ValueError(
                "ModelBasedRelevanceSelector requires an explicit model — "
                "pass the local model id you want for the side query "
                "(typically a small Haiku-tier model)."
            )
        self._provider = provider
        self._model = model

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
            from ..providers.base import ProviderRequest
            from ..models.message import UserMessage

            request = ProviderRequest(
                model=self._model,
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


# ── Memory injection into prompts ─────────────────────────────────────


MEMORY_INJECTION_HEADER = "## Relevant Memories"
"""Header used at the top of the injected memory block."""


def render_memories_for_prompt(
    memories: list[RelevantMemory],
    *,
    read_body: Optional[callable] = None,
    max_chars_per_memory: int = 2_000,
) -> str:
    """Format a list of selected memories as a system-prompt section.

    Returns an empty string when ``memories`` is empty (so callers can
    do ``system_prompt + render_memories_for_prompt(...)`` without a
    branch). Each memory's body is read via ``read_body(path)`` (a
    callable so tests can inject without touching disk); on failure,
    the memory is skipped with a warning.

    ``max_chars_per_memory`` caps each body to keep the injected block
    bounded — a misconfigured 50KB memory file shouldn't blow the
    prompt budget.
    """
    if not memories:
        return ""

    if read_body is None:
        def _default_read(path: str) -> str:
            from pathlib import Path

            return Path(path).read_text(encoding="utf-8")

        read_body = _default_read

    sections: list[str] = [MEMORY_INJECTION_HEADER, ""]
    for mem in memories:
        try:
            body = read_body(mem.path)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "render_memories_for_prompt: failed to read %s: %s", mem.path, exc
            )
            continue
        if not body:
            continue
        if len(body) > max_chars_per_memory:
            body = body[:max_chars_per_memory] + "\n[…truncated…]"
        sections.append(f"### {mem.filename}\n{body}\n")
    if len(sections) <= 2:
        return ""  # All reads failed
    return "\n".join(sections).rstrip() + "\n"


__all__ = [
    "MAX_RELEVANT_MEMORIES",
    "MEMORY_INJECTION_HEADER",
    "ModelBasedRelevanceSelector",
    "RelevanceSelector",
    "RelevantMemory",
    "format_memory_manifest",
    "render_memories_for_prompt",
]
