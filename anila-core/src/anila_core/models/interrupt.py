"""Runtime form of an interrupt request.

A tool implementation that needs to pause the run loop (AskUserQuestion,
PlanMode, future tool-approval flow) returns an :class:`InterruptItem`.
QueryEngine catches it, persists a storage-form
:class:`anila_core.memory.session.InterruptRecord` to the active Session,
then raises :class:`anila_core.engine.approvals.RunPaused`.

Lives in ``models/`` (not ``engine/``) so that
:class:`anila_core.models.message.ToolResult` can hold one without an
inter-package import cycle.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field


InterruptKind = Literal["ask_user", "plan", "tool_approval"]


def _new_interrupt_id() -> str:
    return f"int-{uuid.uuid4().hex[:16]}"


class InterruptItem(BaseModel):
    """In-process payload returned by a tool to pause the run loop.

    Attributes:
        id: Stable identifier; the same id is persisted as
            :class:`InterruptRecord.id` and quoted back by the resume
            endpoint, so producers should leave the default factory
            unless they need to dedupe across retries.
        kind: Discriminator the resume endpoint uses to validate the
            answer shape. New kinds need a renderer in
            :func:`anila_core.engine.approvals._render_answer`.
        payload: Tool-specific dict the UI renders. Schema is enforced
            by the producing tool, not here — keep it JSON-serialisable.
    """

    id: str = Field(default_factory=_new_interrupt_id)
    kind: InterruptKind
    payload: dict[str, Any] = Field(default_factory=dict)

    model_config = {"frozen": True}
