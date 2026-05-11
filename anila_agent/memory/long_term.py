"""Long-term memory — recall + save on top of `MemdirStore`.

Recall mirrors claude-code-src `findRelevantMemories.ts`:
  1. Scan the memory dir, gather frontmatter for each `.md` file.
  2. Format a manifest (`[type] filename (ts): description`).
  3. Ask a small LLM call to pick up to k filenames relevant to the query.
  4. Return the selected files.

The LLM call uses the openai-agents Runner with a one-shot lightweight Agent so
the same OpenAI-compatible endpoint serves both main and side calls.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agents import Agent, ModelSettings, Runner
from agents.models.interface import Model

from anila_agent.memory.store import MemdirStore, MemoryHeader
from anila_agent.models.schemas import MemoryFrontmatter
from anila_agent.utils.logging import get_logger

logger = get_logger(__name__)

MemoryType = str  # one of: user | feedback | project | reference


_SELECT_SYSTEM_PROMPT = (
    "You are selecting memories that will be useful as the agent processes the user's "
    "query. You are given the query and a list of available memory files with their "
    "filenames, types, timestamps, and descriptions.\n\n"
    "Return JSON: {\"selected_memories\": [\"filename1.md\", ...]}.\n"
    "- Pick at most 5 filenames.\n"
    "- Only include memories that are clearly useful based on description.\n"
    "- If unsure, exclude. Empty list is a valid answer.\n"
    "- Use exact filenames from the manifest."
)


@dataclass(frozen=True)
class RecalledMemory:
    filename: str
    path: Path
    body: str
    frontmatter: MemoryFrontmatter | None


class LongTermMemory:
    def __init__(
        self,
        store: MemdirStore,
        *,
        model: str | Model | None = None,
        model_settings: ModelSettings | None = None,
    ) -> None:
        self.store = store
        self._model = model
        self._model_settings = model_settings or ModelSettings(temperature=0.0, max_tokens=512)

    async def recall(
        self,
        query: str,
        *,
        k: int = 5,
        recent_tools: list[str] | None = None,
        already_surfaced: set[str] | None = None,
    ) -> list[RecalledMemory]:
        headers = self.store.scan()
        if already_surfaced:
            headers = [h for h in headers if h.filename not in already_surfaced]
        if not headers:
            return []
        selected = await self._select(query, headers, recent_tools or [], k=k)
        out: list[RecalledMemory] = []
        for filename in selected:
            try:
                fm, body = self.store.read(filename)
                out.append(
                    RecalledMemory(
                        filename=filename,
                        path=self.store.dir / filename,
                        body=body,
                        frontmatter=fm,
                    )
                )
            except FileNotFoundError:
                continue
        return out

    async def _select(
        self,
        query: str,
        headers: list[MemoryHeader],
        recent_tools: list[str],
        *,
        k: int,
    ) -> list[str]:
        manifest = self.store.format_manifest(headers)
        valid = {h.filename for h in headers}
        tools_section = (
            f"\n\nRecently used tools: {', '.join(recent_tools)}" if recent_tools else ""
        )
        user_msg = f"Query: {query}\n\nAvailable memories:\n{manifest}{tools_section}"

        if self._model is None:
            return self._fallback_select(query, headers, k=k)

        agent = Agent[Any](
            name="anila-memory-selector",
            instructions=_SELECT_SYSTEM_PROMPT,
            model=self._model,
            model_settings=self._model_settings,
        )
        try:
            result = await Runner.run(starting_agent=agent, input=user_msg, max_turns=1)
        except Exception as e:
            logger.warning("memory recall LLM call failed (%s); falling back to keyword overlap", e)
            return self._fallback_select(query, headers, k=k)

        text = (result.final_output or "").strip()
        try:
            parsed = json.loads(text)
            picked = parsed.get("selected_memories", [])
        except json.JSONDecodeError:
            return self._fallback_select(query, headers, k=k)
        if not isinstance(picked, list):
            return []
        return [p for p in picked if isinstance(p, str) and p in valid][:k]

    @staticmethod
    def _fallback_select(query: str, headers: list[MemoryHeader], *, k: int) -> list[str]:
        """Token-overlap heuristic for when no model is configured / call failed."""
        import re

        q = {m.group(0).lower() for m in re.finditer(r"\w+", query)}
        if not q:
            return []
        scored: list[tuple[int, str]] = []
        for h in headers:
            text = " ".join(filter(None, [h.filename, h.description or "", h.type or ""])).lower()
            tokens = {m.group(0) for m in re.finditer(r"\w+", text)}
            overlap = len(q & tokens)
            if overlap > 0:
                scored.append((overlap, h.filename))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [filename for _, filename in scored[:k]]

    def save(
        self,
        *,
        filename: str,
        name: str,
        description: str,
        type: MemoryType,
        body: str,
        index_line: str | None = None,
    ) -> Path:
        """Write a memory file and (optionally) append an index entry to MEMORY.md."""
        fm = MemoryFrontmatter(name=name, description=description, type=type)  # type: ignore[arg-type]
        path = self.store.write(filename, fm, body)
        if index_line:
            self.store.append_index_entry(index_line)
        return path

    def list_index(self) -> str:
        return self.store.truncate_index().content
