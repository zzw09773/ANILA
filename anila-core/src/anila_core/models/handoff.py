"""Runtime form of a handoff request.

A handoff is a **control transfer** from the currently running agent to
another agent (typically a different specialist) for subsequent turns.
Distinct from ``dispatch_to_agent`` which is a **sub-call** (block on
the dispatched agent, continue with its result).

A tool implementation that wants to hand off returns a
:class:`HandoffRequest`. QueryEngine catches the result, persists state
to the active :class:`Session`, and raises
:class:`anila_core.engine.handoff.RunHandoff`. The caller — typically
the Router — catches that and dispatches the next agent with the
filtered context.

Lives in ``models/`` so :class:`anila_core.models.message.ToolResult`
can hold one without an inter-package import cycle (mirrors
:class:`InterruptItem` from PR 2).

Mirrors openai-agents `handoffs/` shape, adapted for ANILA's HTTP
between-agent architecture (the actual transfer happens via Router →
CSP → next agent rather than in-process).
"""

from __future__ import annotations

import uuid
from typing import Any, Optional

from pydantic import BaseModel, Field


def _new_handoff_id() -> str:
    return f"hand-{uuid.uuid4().hex[:16]}"


class HandoffRequest(BaseModel):
    """In-process payload returned by a tool to transfer control.

    Attributes:
        id: Stable identifier — quoted in SSE events and stored as
            :class:`InterruptRecord.id`-style record so the resume
            endpoint can refer to it.
        target_agent_id: The agent to transfer to. Caller (Router)
            looks this up in :class:`RemoteAgentRegistry`.
        message: The instruction / prompt for the target agent. Plays
            the role of the user-message on the target's first turn.
        context_messages: Filtered conversation history to ship along
            so the target sees relevant prior turns. Already filtered
            by the producing tool — see
            :mod:`anila_core.engine.handoff` for built-in filters.
        reason: Free-text "why" the source agent chose this handoff.
            Surfaced in :attr:`anila_meta.handoff_chain` for debugging.
        metadata: Arbitrary JSON-serialisable payload the caller may
            need (priority hints, tracing tags, etc.). Kept opaque at
            this layer.
    """

    id: str = Field(default_factory=_new_handoff_id)
    target_agent_id: str
    message: str
    context_messages: list[dict[str, Any]] = Field(default_factory=list)
    reason: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    model_config = {"frozen": True}
