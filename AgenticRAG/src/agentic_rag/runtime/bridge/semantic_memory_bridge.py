"""``MemdirSemanticMemory`` — bridge from agentic_rag/memory/ to framework SemanticMemory.

AgenticRAG already ships a sophisticated memory system: memdir
(MEMORY.md + frontmatter), MemoryHeader, scan_memory_files,
ModelBasedRelevanceSelector. This bridge wraps those into the
framework's ``SemanticMemory`` Protocol so framework-level consumers
(middleware, agents) can read/write through one shape.

Design split:

- **Reads** go through ``ModelBasedRelevanceSelector`` (LLM-side
  ranking against the manifest) when a relevance selector is
  configured. Without one, falls back to substring matching against
  scanned memory bodies.
- **Writes** create / update markdown files in the memory directory
  with YAML frontmatter that round-trips through the existing memdir
  parser. Filename is derived from ``MemoryEntry.id``.

What the bridge does NOT do:

- Run the post-turn extraction pipeline. That stays driven by the
  existing ``MemoryExtractor`` — it operates at a different layer
  (post-turn rather than per-recall).
- Manage the cursor state. That's still ``CursorStore`` territory.

Usage::

    from agentic_rag.memory.memdir import scan_memory_files
    from agentic_rag.memory.relevance_selector import ModelBasedRelevanceSelector
    from agentic_rag.runtime.bridge.semantic_memory_bridge import MemdirSemanticMemory

    selector = ModelBasedRelevanceSelector(provider, model="haiku-local")
    memory = MemdirSemanticMemory(
        memory_dir="/var/agent/memory",
        relevance_selector=selector,
    )
    # Now memory satisfies the framework SemanticMemory Protocol.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml  # type: ignore[import-untyped]

from agentic_rag.memory.memdir import scan_memory_files
from agentic_rag.memory.relevance_selector import (
    RelevanceSelector,
)
from agentic_rag.models.memory import MemoryHeader, MemoryType
from agentic_rag.runtime.framework.memory.protocol import (
    MemoryEntry,
    MemoryKind,
)

logger = logging.getLogger(__name__)


# ── MemoryKind ↔ AgenticRAG MemoryType mapping ─────────────────────


_KIND_TO_TYPE: dict[MemoryKind, MemoryType] = {
    MemoryKind.USER: MemoryType.USER_PREFERENCE,
    MemoryKind.FEEDBACK: MemoryType.USER_PREFERENCE,
    MemoryKind.PROJECT: MemoryType.PROJECT_CONVENTION,
    MemoryKind.REFERENCE: MemoryType.API_PATTERN,
    MemoryKind.WORKING: MemoryType.GENERAL,
}


_TYPE_TO_KIND: dict[MemoryType, MemoryKind] = {
    MemoryType.USER_PREFERENCE: MemoryKind.USER,
    MemoryType.PROJECT_CONVENTION: MemoryKind.PROJECT,
    MemoryType.DEBUGGING_LESSON: MemoryKind.PROJECT,
    MemoryType.API_PATTERN: MemoryKind.REFERENCE,
    MemoryType.GENERAL: MemoryKind.WORKING,
}


def _kind_for_type(memtype: MemoryType) -> MemoryKind:
    return _TYPE_TO_KIND.get(memtype, MemoryKind.WORKING)


def _type_for_kind(kind: MemoryKind) -> MemoryType:
    return _KIND_TO_TYPE.get(kind, MemoryType.GENERAL)


# ── The bridge ──────────────────────────────────────────────────────


class MemdirSemanticMemory:
    """Adapt the existing AgenticRAG memdir + relevance selector to the
    framework's ``SemanticMemory`` Protocol.

    Construction:

        MemdirSemanticMemory(
            memory_dir="/var/agent/memory",
            relevance_selector=ModelBasedRelevanceSelector(p, model="haiku"),
        )

    ``relevance_selector`` is optional; if omitted, recall falls back
    to plain substring matching against scanned memory bodies. For
    production use cases, plug in a real selector.
    """

    def __init__(
        self,
        memory_dir: str | Path,
        *,
        relevance_selector: RelevanceSelector | None = None,
    ) -> None:
        self._memory_dir = Path(memory_dir)
        self._selector = relevance_selector

    # ── Protocol surface ────────────────────────────────────────────

    async def remember(self, entry: MemoryEntry) -> None:
        """Write the entry to ``memory_dir/<id>.md`` with frontmatter."""
        self._memory_dir.mkdir(parents=True, exist_ok=True)
        path = self._memory_dir / f"{_safe_filename(entry.id)}.md"

        memtype = _type_for_kind(entry.kind)
        title = entry.metadata.get("title") or entry.id
        description = entry.metadata.get("description") or _summarise(entry.content)
        tags = entry.metadata.get("tags") or []

        frontmatter: dict[str, Any] = {
            "title": title,
            "description": description,
            "type": memtype.value,
            "tags": tags,
            "created": entry.created_at.isoformat(),
            "updated": entry.updated_at.isoformat(),
        }
        if entry.ttl_seconds is not None:
            frontmatter["ttl_seconds"] = entry.ttl_seconds

        content = (
            "---\n"
            + yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True).strip()
            + "\n---\n\n"
            + entry.content.strip()
            + "\n"
        )

        try:
            tmp = path.with_suffix(".tmp")
            tmp.write_text(content, encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            logger.warning(
                "MemdirSemanticMemory.remember: write failed for %s: %s",
                path,
                exc,
            )

    async def recall(
        self,
        query: str,
        *,
        kind: MemoryKind | None = None,
        limit: int = 5,
    ) -> list[MemoryEntry]:
        headers = await scan_memory_files(str(self._memory_dir))
        if kind is not None:
            target_type = _type_for_kind(kind)
            headers = [h for h in headers if h.memory_type is target_type]
        if not headers:
            return []

        # Use the LLM-side selector when configured.
        if self._selector is not None:
            try:
                relevant = await self._selector.select(
                    query=query,
                    memory_headers=headers,
                    recent_tools=[],
                    already_surfaced=set(),
                    abort_event=None,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "MemdirSemanticMemory.recall: selector failed (%s); "
                    "falling back to substring scan",
                    exc,
                )
                relevant = []
            if relevant:
                paths = [r.path for r in relevant[:limit]]
                out: list[MemoryEntry] = []
                for path in paths:
                    entry = await self._read_entry(path)
                    if entry is not None:
                        out.append(entry)
                return out

        # Fallback: substring match against headers.
        matches = _substring_rank(query, headers, limit)
        fallback: list[MemoryEntry] = []
        for header in matches:
            entry = await self._read_entry(header.file_path)
            if entry is not None:
                fallback.append(entry)
        return fallback

    async def forget(self, entry_id: str) -> None:
        path = self._memory_dir / f"{_safe_filename(entry_id)}.md"
        try:
            path.unlink()
        except FileNotFoundError:
            return
        except OSError as exc:
            logger.warning("MemdirSemanticMemory.forget: unlink failed for %s: %s", path, exc)

    async def list_all(
        self, *, kind: MemoryKind | None = None
    ) -> list[MemoryEntry]:
        headers = await scan_memory_files(str(self._memory_dir))
        if kind is not None:
            target_type = _type_for_kind(kind)
            headers = [h for h in headers if h.memory_type is target_type]
        results: list[MemoryEntry] = []
        for header in headers:
            entry = await self._read_entry(header.file_path)
            if entry is not None:
                results.append(entry)
        return results

    # ── helpers ─────────────────────────────────────────────────────

    async def _read_entry(self, path: str) -> Optional[MemoryEntry]:
        """Load a single memory file from disk and convert to MemoryEntry."""
        p = Path(path)
        try:
            text = p.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("MemdirSemanticMemory._read_entry: read failed %s: %s", p, exc)
            return None
        frontmatter, body = _split_frontmatter(text)
        memtype_value = frontmatter.get("type")
        try:
            memtype = MemoryType(memtype_value) if memtype_value else MemoryType.GENERAL
        except ValueError:
            memtype = MemoryType.GENERAL
        kind = _kind_for_type(memtype)
        created = _parse_datetime(frontmatter.get("created")) or _utc_now()
        updated = _parse_datetime(frontmatter.get("updated")) or created
        ttl = frontmatter.get("ttl_seconds")
        try:
            ttl_seconds = float(ttl) if ttl is not None else None
        except (TypeError, ValueError):
            ttl_seconds = None
        # Use filename stem as id so round-trip with remember() is stable.
        entry_id = p.stem
        return MemoryEntry(
            id=entry_id,
            kind=kind,
            content=body,
            metadata={
                "title": frontmatter.get("title", entry_id),
                "description": frontmatter.get("description", ""),
                "tags": frontmatter.get("tags", []),
                "source_path": str(p),
            },
            created_at=created,
            updated_at=updated,
            ttl_seconds=ttl_seconds,
        )


# ── Module-level helpers ───────────────────────────────────────────


def _safe_filename(name: str) -> str:
    """Filesystem-safe filename slug. Same approach as CursorStore."""
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in name)
    return safe or "entry"


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    stripped = text.strip()
    if not stripped.startswith("---"):
        return {}, stripped
    rest = stripped[3:]
    end = rest.find("\n---")
    if end == -1:
        return {}, stripped
    yaml_block = rest[:end].strip()
    body = rest[end + 4:].strip()
    try:
        data = yaml.safe_load(yaml_block) or {}
    except yaml.YAMLError:
        return {}, stripped
    if not isinstance(data, dict):
        return {}, stripped
    return data, body


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _summarise(content: str, *, max_chars: int = 140) -> str:
    """One-line description from the body's first non-empty line."""
    for line in content.splitlines():
        line = line.strip()
        if line:
            return line[:max_chars]
    return ""


def _substring_rank(
    query: str, headers: list[MemoryHeader], limit: int
) -> list[MemoryHeader]:
    """Naive fallback when no relevance_selector is configured."""
    tokens = [t for t in query.lower().split() if t]
    if not tokens:
        return headers[:limit]
    scored: list[tuple[int, MemoryHeader]] = []
    for header in headers:
        text = (header.title + " " + header.description).lower()
        score = sum(1 for t in tokens if t in text)
        if score > 0:
            scored.append((score, header))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [h for _, h in scored[:limit]]


__all__ = ["MemdirSemanticMemory"]
