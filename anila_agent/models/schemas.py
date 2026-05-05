"""Shared Pydantic schemas. Kept in `models/` so any module can import without cycles."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class HookOutput(BaseModel):
    """Return value from a hook callback. Mirrors the claude-code-src sync hook schema.

    Fields:
        continue_: when False, the runner aborts the current turn with `stop_reason`.
        decision: 'approve' (force allow) | 'block' (force deny) | None (defer).
        reason: explanation surfaced to the user / model when blocking.
        additional_context: text merged into the next model turn as a system reminder.
        updated_input: replacement tool input. Honoured by PreToolUse only.
    """

    model_config = {"populate_by_name": True}

    continue_: bool = Field(default=True, alias="continue")
    decision: Literal["approve", "block"] | None = None
    reason: str | None = None
    stop_reason: str | None = None
    additional_context: str | None = None
    updated_input: dict[str, Any] | None = None


class MemoryFrontmatter(BaseModel):
    """YAML frontmatter on a memory `.md` file."""

    name: str
    description: str
    type: Literal["user", "feedback", "project", "reference"]


class Document(BaseModel):
    """Retrieval result. Override fields by subclassing if you need richer metadata."""

    id: str
    text: str
    score: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
