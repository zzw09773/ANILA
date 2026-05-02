"""Approval / interrupt primitive for pause-resume run loops.

Used by ``AskUserQuestion`` (PR 3), ``PlanMode`` (PR 3) and any future
tool that needs to wait on a user decision mid-turn. Sits below the
tool layer: a tool implementation returns
:class:`anila_core.models.interrupt.InterruptItem` to signal "pause the
run; await an answer; then feed it back as my tool result".

Three pieces:

- :class:`RunPaused` — exception QueryEngine raises after persisting
  state, so the FastAPI handler can flush its SSE stream cleanly.
- :func:`to_record` — convert in-memory :class:`InterruptItem` to the
  storage-form :class:`InterruptRecord`. Captures sibling tool results
  so we can stitch the conversation back together on resume.
- :func:`resume_with` — pop the named interrupt from the Session and
  build the next-turn :class:`UserMessage` from the user's answer.

The contract is "one interrupt per turn". A turn that produces multiple
``InterruptItem`` results raises :class:`MultipleInterruptsError` — Sprint 9
keeps this simple; Sprint 10 may relax if real workloads hit it.
"""

from __future__ import annotations

import json
from typing import Any, cast

from ..memory.session import InterruptRecord, Session
from ..models.interrupt import InterruptItem, InterruptKind
from ..models.message import ToolCall, ToolResult, UserMessage


class RunPaused(Exception):
    """Raised by QueryEngine after a tool returned :class:`InterruptItem`.

    Carries enough info for the SSE handler to emit ``anila.paused`` and
    for the caller to know which session/interrupt the resume endpoint
    should target.
    """

    def __init__(
        self,
        session_id: str,
        interrupt_id: str,
        kind: InterruptKind,
    ) -> None:
        super().__init__(
            f"run paused on session={session_id} "
            f"interrupt={interrupt_id} kind={kind}"
        )
        self.session_id = session_id
        self.interrupt_id = interrupt_id
        self.kind = kind


class MultipleInterruptsError(RuntimeError):
    """Raised when more than one tool in a single turn returned an interrupt.

    Sprint 9 mandates one-interrupt-per-turn. If real workloads demand it,
    revisit in Sprint 10 by extending :class:`InterruptRecord` to a list.
    """


def to_record(
    item: InterruptItem,
    *,
    tool_call: ToolCall,
    sibling_results: list[ToolResult],
) -> InterruptRecord:
    """Convert in-memory :class:`InterruptItem` to storage-form record.

    Args:
        item: The InterruptItem returned by the tool implementation.
        tool_call: The originating tool call (we need its id + name + input
            so the resume side knows which tool result block to stitch).
        sibling_results: Tool results for the OTHER calls in the same turn.
            Cached in payload until the user answers, then replayed
            alongside the answer so the model sees one
            :class:`UserMessage` with all ``tool_result`` blocks.
    """
    return InterruptRecord(
        id=item.id,
        kind=item.kind,
        payload={
            "data": item.payload,
            "tool_call": {
                "id": tool_call.id,
                "name": tool_call.name,
                "input": tool_call.input,
            },
            "sibling_results": [
                _serialize_tool_result(r) for r in sibling_results
            ],
        },
    )


def build_resume_message(
    record: InterruptRecord,
    answer: dict[str, Any] | str,
) -> UserMessage:
    """Build the :class:`UserMessage` that completes the paused turn.

    The result combines:
    - ``tool_result`` blocks for sibling calls (cached at pause), and
    - a ``tool_result`` block for the interrupted call rendered from the
      user's answer (see :func:`_render_answer`).
    """
    payload = record.payload
    tool_call_meta = payload["tool_call"]
    sibling_results = [
        _deserialize_tool_result(d)
        for d in payload.get("sibling_results", [])
    ]

    # ``InterruptRecord.kind`` is stored as ``str`` for storage flexibility;
    # here it must be one of the InterruptKind literals for the renderer.
    answer_text = _render_answer(cast(InterruptKind, record.kind), answer)

    blocks: list[dict[str, Any]] = []
    for r in sibling_results:
        block: dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": r.tool_call_id,
            "content": (
                r.content if isinstance(r.content, str)
                else _flatten_content(r.content)
            ),
        }
        if r.is_error:
            block["is_error"] = True
        blocks.append(block)
    blocks.append(
        {
            "type": "tool_result",
            "tool_use_id": tool_call_meta["id"],
            "content": answer_text,
        }
    )
    return UserMessage(content=blocks)


async def resume_with(
    session: Session,
    interrupt_id: str,
    answer: dict[str, Any] | str,
) -> UserMessage:
    """Pop the named interrupt and build the resume :class:`UserMessage`.

    Caller (typically ``QueryEngine.resume_from_interrupt``) is expected
    to append the result to session history and continue the run loop.

    Resuming a ``plan`` interrupt clears
    :attr:`AgentContext.plan_mode` if a context is bound — once the plan
    has been approved (or rejected) the destructive-tool gate is
    released so the model can execute.

    Raises:
        ValueError: if the interrupt_id is not in the session's pending
            queue (already answered, or unknown id).
    """
    record = await session.pop_interrupt(interrupt_id)
    if record is None:
        raise ValueError(
            f"Interrupt '{interrupt_id}' not found in session "
            f"'{session.session_id}' (already answered, or unknown id)."
        )
    if record.kind == "plan":
        # Best-effort: only clears if the resume runs inside an
        # AgentContext. Server wiring may need a separate hook for
        # cross-task contexts.
        from ..context.agent_context import get_current_context
        ctx = get_current_context()
        if ctx is not None:
            ctx.plan_mode = False
    return build_resume_message(record, answer)


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _serialize_tool_result(r: ToolResult) -> dict[str, Any]:
    return {
        "tool_call_id": r.tool_call_id,
        "content": r.content,
        "is_error": r.is_error,
    }


def _deserialize_tool_result(d: dict[str, Any]) -> ToolResult:
    return ToolResult(
        tool_call_id=d["tool_call_id"],
        content=d["content"],
        is_error=d.get("is_error", False),
    )


def _flatten_content(content: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "\n".join(parts)


def _render_answer(kind: InterruptKind, answer: dict[str, Any] | str) -> str:
    """Render the user's answer as model-visible text.

    Producing tools may pass a string directly (escape hatch). The default
    formatters keep the wire shape stable so the model can rely on it.
    """
    if isinstance(answer, str):
        return answer

    if kind == "ask_user":
        selected = answer.get("selected") or []
        other_text = answer.get("other_text")
        parts: list[str] = []
        if selected:
            parts.append("user_selected: " + ", ".join(str(s) for s in selected))
        if other_text:
            parts.append(f"user_input: {other_text}")
        return "\n".join(parts) if parts else "(no answer provided)"

    if kind == "plan":
        approved = bool(answer.get("approved", False))
        comment = (answer.get("comment") or "").strip()
        head = "plan_approved" if approved else "plan_rejected"
        return f"{head}\nuser_comment: {comment}" if comment else head

    if kind == "tool_approval":
        approved = bool(answer.get("approved", False))
        return "tool_approved" if approved else "tool_denied"

    # Unknown kind — JSON-dump as escape hatch.
    return json.dumps(answer, ensure_ascii=False)
