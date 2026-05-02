"""SSE adapter over the framework ``Runner`` for the AgenticRAG API.

Drives ``Runner.run(agent, ...)`` and yields ``ServerEvent`` objects
formatted as raw SSE strings — wire-compatible with what ``/chat``
already emits, so frontend code does not have to branch on which
endpoint produced a given stream.

Stage B granularity:

The framework ``Runner`` is unary in v0.1 (no token-by-token
streaming — that lands in Sprint 2 alongside Middleware). The
``Runner`` returns a ``RunResult`` once, then this adapter walks
``RunResult.items`` to synthesise the SSE stream:

  - each ``MessageOutputItem`` with non-empty text → ``MESSAGE_DELTA``
    (one event for the whole message rather than per-token)
  - each ``ToolCallItem`` → ``TOOL_CALL_STARTED``
  - each ``ToolResultItem`` → ``TOOL_CALL_FINISHED``
  - the final ``MessageOutputItem.usage`` aggregates → ``USAGE_UPDATE``
  - terminal ``STREAM_DONE`` (or ``ERROR`` on exception)

This is intentionally coarser than ``/chat``'s per-token stream so
clients see no message-content drift while we wait for token streaming
to land in Sprint 2/3. The wire envelope (``ServerEvent.to_sse``) is
unchanged.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

from agentic_rag.runtime.framework import Agent, Runner
from agentic_rag.runtime.framework.exceptions import AgentsException
from agentic_rag.runtime.framework.items import (
    HandoffItem,
    Message as FwMessage,
    MessageOutputItem,
    ToolCall,
    ToolCallItem,
    ToolResultItem,
)

from agentic_rag.api.events import (
    ErrorPayload,
    EventType,
    MessageDeltaPayload,
    ServerEvent,
    ToolCallFinishedPayload,
    ToolCallStartedPayload,
    UsageUpdatePayload,
)

logger = logging.getLogger(__name__)


_PREVIEW_LIMIT = 200
"""Cap tool output preview at 200 chars in TOOL_CALL_FINISHED — full
content stays in the LLM's tool-result message; the SSE preview is for
UI rendering."""


async def run_agentic_chat_sse(
    agent: Agent,
    user_input: str | FwMessage | list[FwMessage],
    *,
    session_id: str,
    runner: Runner | None = None,
) -> AsyncIterator[str]:
    """Run an Agent through the framework Runner and yield SSE strings.

    Caller is responsible for wrapping the returned iterator in
    ``StreamingResponse(..., media_type='text/event-stream')``.

    On framework / handler exceptions the iterator emits a single
    ``ERROR`` event followed by ``STREAM_DONE(status=error)`` rather
    than raising — matches the behaviour of the existing ``/chat``
    endpoint so the frontend disconnect logic is uniform.
    """
    runner = runner or Runner()

    try:
        result = await runner.run(agent, user_input)
    except AgentsException as exc:
        logger.warning("agentic runner raised: %s: %s", type(exc).__name__, exc)
        yield ServerEvent(
            type=EventType.ERROR,
            session_id=session_id,
            payload=ErrorPayload(
                message=str(exc),
                code=type(exc).__name__,
            ).model_dump(),
        ).to_sse()
        yield ServerEvent(
            type=EventType.STREAM_DONE,
            session_id=session_id,
            payload={"status": "error"},
        ).to_sse()
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception("agentic runner crashed unexpectedly")
        yield ServerEvent(
            type=EventType.ERROR,
            session_id=session_id,
            payload=ErrorPayload(
                message=f"unexpected runtime error: {exc}",
                code="internal_error",
            ).model_dump(),
        ).to_sse()
        yield ServerEvent(
            type=EventType.STREAM_DONE,
            session_id=session_id,
            payload={"status": "error"},
        ).to_sse()
        return

    # Walk the audit-trail items in the order the runner emitted them so
    # the SSE stream reads chronologically: each LLM turn followed by
    # any tool call / result it triggered, then the next LLM turn, etc.
    turn_index = 0
    for item in result.items:
        if isinstance(item, MessageOutputItem):
            text = _extract_text(item.message)
            if text:
                yield ServerEvent(
                    type=EventType.MESSAGE_DELTA,
                    session_id=session_id,
                    payload=MessageDeltaPayload(
                        text=text, turn_index=turn_index
                    ).model_dump(),
                ).to_sse()
            turn_index += 1
        elif isinstance(item, ToolCallItem):
            yield ServerEvent(
                type=EventType.TOOL_CALL_STARTED,
                session_id=session_id,
                payload=ToolCallStartedPayload(
                    tool_call_id=item.call.id,
                    tool_name=item.call.name,
                    input=_safe_parsed_args(item.call),
                ).model_dump(),
            ).to_sse()
        elif isinstance(item, ToolResultItem):
            yield ServerEvent(
                type=EventType.TOOL_CALL_FINISHED,
                session_id=session_id,
                payload=ToolCallFinishedPayload(
                    tool_call_id=item.result.call_id,
                    tool_name=item.result.name,
                    is_error=item.result.is_error,
                    output_preview=_truncate(
                        item.result.error or item.result.output or ""
                    ),
                ).model_dump(),
            ).to_sse()
        elif isinstance(item, HandoffItem):
            # Handoff is informational on the SSE — the next
            # MessageOutputItem will already carry the new agent's
            # voice. Surface as TASK_NOTIFICATION so the existing
            # client renderer can pick it up.
            yield ServerEvent(
                type=EventType.TASK_NOTIFICATION,
                session_id=session_id,
                payload={
                    "from_agent": item.from_agent,
                    "to_agent": item.to_agent,
                    "reason": item.reason,
                },
            ).to_sse()

    # Final usage aggregate
    yield ServerEvent(
        type=EventType.USAGE_UPDATE,
        session_id=session_id,
        payload=UsageUpdatePayload(
            input_tokens=result.usage.input_tokens,
            output_tokens=result.usage.output_tokens,
            cache_read_tokens=result.usage.input_tokens_details.cached_tokens,
            cache_write_tokens=0,
            turn_count=result.turns,
        ).model_dump(),
    ).to_sse()

    yield ServerEvent(
        type=EventType.STREAM_DONE,
        session_id=session_id,
        payload={"status": "completed"},
    ).to_sse()


# ── helpers ────────────────────────────────────────────────────────────


def _extract_text(message: FwMessage) -> str:
    """Pull the assistant's text from string or content-parts shape."""
    if isinstance(message.content, str):
        return message.content
    parts: list[str] = []
    for part in message.content:
        text = getattr(part, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts)


def _safe_parsed_args(call: ToolCall) -> dict[str, Any] | None:
    """Best-effort decode of tool call arguments for UI display.

    Returns ``None`` rather than raising if the model emitted malformed
    JSON — the framework will surface that as a tool error in its own
    message; the SSE preview just doesn't show args.
    """
    try:
        parsed: dict[str, Any] = call.parsed_arguments()
        return parsed
    except Exception:  # noqa: BLE001
        return None


def _truncate(text: str) -> str:
    if len(text) <= _PREVIEW_LIMIT:
        return text
    return text[:_PREVIEW_LIMIT] + "…"


__all__ = ["run_agentic_chat_sse"]
