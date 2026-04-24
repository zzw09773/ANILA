"""Memory models for the AgenticRAG agent runtime.

Memory is organized in three scopes:
  - session:  bound to a single session_id, frozen when session ends
  - project:  bound to user_id + project_id, persists across sessions
  - global:   bound to user_id, persists across all projects
"""

from __future__ import annotations

import os
from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class MemoryType(str, Enum):
    """Semantic classification of a memory file."""

    USER_PREFERENCE = "user_preference"
    PROJECT_CONVENTION = "project_convention"
    DEBUGGING_LESSON = "debugging_lesson"
    API_PATTERN = "api_pattern"
    GENERAL = "general"


class MemoryScope(str, Enum):
    """Persistence scope for a memory."""

    SESSION = "session"    # bound to session_id
    PROJECT = "project"    # bound to user_id + project_id
    GLOBAL = "global"      # bound to user_id


class MemoryHeader(BaseModel):
    """Frontmatter metadata for a memory file.

    Loaded from YAML frontmatter at the top of .md files in the memory directory.
    The mtime_ms field is derived from the file's modification time, not stored
    in frontmatter — it is used as a freshness signal during relevance selection.
    """

    filename: str
    file_path: str
    title: str
    description: str = ""
    memory_type: MemoryType = MemoryType.GENERAL
    tags: list[str] = Field(default_factory=list)
    created: Optional[datetime] = None
    updated: Optional[datetime] = None
    mtime_ms: float = 0.0
    scope: MemoryScope = MemoryScope.PROJECT

    def format_manifest_line(self) -> str:
        """Format as a single manifest line for the relevance selector prompt."""
        tag = f"[{self.memory_type.value}] " if self.memory_type else ""
        ts = datetime.utcfromtimestamp(self.mtime_ms / 1000).isoformat() if self.mtime_ms else ""
        if self.description:
            return f"- {tag}{self.filename} ({ts}): {self.description}"
        return f"- {tag}{self.filename} ({ts})"

    @classmethod
    def from_dict(
        cls,
        data: dict,
        filename: str,
        file_path: str,
        mtime_ms: float = 0.0,
    ) -> "MemoryHeader":
        """Construct from parsed frontmatter dict."""
        return cls(
            filename=filename,
            file_path=file_path,
            title=data.get("title", os.path.splitext(filename)[0]),
            description=data.get("description", ""),
            memory_type=MemoryType(data.get("type", MemoryType.GENERAL)),
            tags=data.get("tags", []),
            created=data.get("created"),
            updated=data.get("updated"),
            mtime_ms=mtime_ms,
            scope=MemoryScope(data.get("scope", MemoryScope.PROJECT)),
        )


class MemoryFile(BaseModel):
    """A memory file with header and body content."""

    header: MemoryHeader
    body: str
    frontmatter_raw: str = ""

    def to_markdown(self) -> str:
        """Render the memory file back to Markdown with frontmatter."""
        lines = ["---"]
        lines.append(f"title: {self.header.title}")
        if self.header.description:
            lines.append(f"description: {self.header.description}")
        lines.append(f"type: {self.header.memory_type.value}")
        if self.header.tags:
            lines.append(f"tags: {self.header.tags}")
        if self.header.created:
            lines.append(f"created: {self.header.created.isoformat()}")
        if self.header.updated:
            lines.append(f"updated: {self.header.updated.isoformat()}")
        lines.append(f"scope: {self.header.scope.value}")
        lines.append("---")
        lines.append("")
        lines.append(self.body)
        return "\n".join(lines)


class SessionMemoryNote(BaseModel):
    """Persistent session-scoped notes extracted from the conversation."""

    session_id: str
    content: str
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    message_count: int = 0


class ConsolidationLock(BaseModel):
    """File-based mutex for memory consolidation (autoDream).

    The lock file mtime IS the lastConsolidatedAt timestamp.
    The body contains the holder's PID.
    """

    locked_at: datetime
    pid: int
    session_id: str

    def is_stale(self, stale_after_seconds: int = 3600) -> bool:
        """Return True if the lock is older than stale_after_seconds."""
        age = (datetime.utcnow() - self.locked_at).total_seconds()
        return age > stale_after_seconds
