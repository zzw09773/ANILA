"""Handoff primitive — control transfer to another agent.

Three pieces:

- :class:`RunHandoff` — exception QueryEngine raises after a tool
  returned :class:`HandoffRequest`. Carries enough info for the SSE
  handler (or Router) to know which agent to dispatch next.
- :class:`HandoffFilter` — Protocol for "given the conversation, what
  do we ship to the target agent". Tools call a filter to populate
  :attr:`HandoffRequest.context_messages`.
- Built-in filters — :class:`NoFilter`, :class:`LastNFilter`,
  :class:`SummaryFilter` (placeholder; LLM summarisation in PR 3).

The Router-side wiring (catch RunHandoff → dispatch target agent) lives
in PR 3. PR 1 ships the primitive + integration with QueryEngine so
in-process tests can exercise it.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from ..models.handoff import HandoffRequest
from ..models.message import AssistantMessage, Message, UserMessage


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------


class RunHandoff(Exception):
    """Raised by QueryEngine after a tool returned :class:`HandoffRequest`.

    Carries enough info for callers (Router / SSE handler) to dispatch
    the target agent. The full :class:`HandoffRequest` is exposed as
    ``request`` so downstream handlers don't have to re-load from session.
    """

    def __init__(
        self,
        session_id: str,
        request: HandoffRequest,
    ) -> None:
        super().__init__(
            f"run handoff on session={session_id} "
            f"target={request.target_agent_id} id={request.id}"
        )
        self.session_id = session_id
        self.request = request


# ---------------------------------------------------------------------------
# Filter Protocol + built-ins
# ---------------------------------------------------------------------------


@runtime_checkable
class HandoffFilter(Protocol):
    """Compute the context-window the target agent should see.

    Implementations are pure: given the source agent's conversation,
    return a list of dicts (OpenAI-style messages) that the Router will
    forward to the target agent. Returning ``[]`` means "send only the
    handoff message; target starts fresh".
    """

    def __call__(self, history: list[Message]) -> list[dict[str, Any]]:
        ...


class NoFilter:
    """Pass the whole history through.

    Use when the target agent genuinely needs the full prior context
    (e.g. handing off a complex debugging session). Be aware this can
    blow up the target's prompt budget.
    """

    def __call__(self, history: list[Message]) -> list[dict[str, Any]]:
        return [_to_dict(m) for m in history]


class LastNFilter:
    """Keep only the last ``n`` user / assistant turns.

    Cheap and predictable — the dominant case for "dispatch to a
    related specialist who needs to see the immediate context".
    """

    def __init__(self, n: int) -> None:
        if n <= 0:
            raise ValueError("LastNFilter requires n >= 1")
        self._n = n

    def __call__(self, history: list[Message]) -> list[dict[str, Any]]:
        # Walk backwards collecting visible user / assistant messages
        # (skip tool_result UserMessages — they're noise for the target).
        kept: list[Message] = []
        for msg in reversed(history):
            if not _is_visible_turn(msg):
                continue
            kept.append(msg)
            if len(kept) >= self._n:
                break
        kept.reverse()
        return [_to_dict(m) for m in kept]


class SummaryFilter:
    """Replace history with a single short summary as the assistant's
    parting note.

    PR 1 placeholder: ships a static summary string the producing tool
    must provide (caller passes ``summary=`` at construction). PR 3 will
    add an LLM-driven variant that calls the active provider with a
    summarisation prompt.
    """

    def __init__(self, summary: str) -> None:
        self._summary = summary.strip()

    def __call__(self, history: list[Message]) -> list[dict[str, Any]]:
        if not self._summary:
            return []
        return [
            {
                "role": "assistant",
                "content": f"[handoff summary]\n{self._summary}",
            }
        ]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_dict(msg: Message) -> dict[str, Any]:
    """Render a Message to OpenAI-style {role, content} dict.

    AssistantMessage tool_calls are dropped — the target agent has its
    own ToolRegistry and shouldn't try to re-execute someone else's
    tool calls. Plain text content survives.
    """
    if isinstance(msg, AssistantMessage):
        return {"role": "assistant", "content": msg.get_text()}
    if isinstance(msg, UserMessage):
        return {"role": "user", "content": msg.get_text()}
    # Fallback for unknown subtypes — best effort.
    return {"role": getattr(msg, "role", "user"), "content": str(msg)}


def _is_visible_turn(msg: Message) -> bool:
    """Skip tool_result-only UserMessages — they're noise for the target."""
    if isinstance(msg, UserMessage):
        content = msg.content
        if isinstance(content, list) and content:
            kinds = {block.get("type") for block in content if isinstance(block, dict)}
            if kinds == {"tool_result"}:
                return False
    return True


__all__ = [
    "RunHandoff",
    "HandoffFilter",
    "NoFilter",
    "LastNFilter",
    "SummaryFilter",
]
