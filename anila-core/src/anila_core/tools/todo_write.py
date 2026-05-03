"""``todo_write`` tool — agent-managed task board.

Mirrors Claude Code's ``TodoWriteTool`` semantics. The tool body
**replaces** the entire todo list on each call (so the model can drop
done items, append new ones, or update statuses in one shot). Validation:

- Each item must carry both ``content`` (imperative) and ``active_form``
  (present-continuous).
- ``status`` must be one of ``pending`` / ``in_progress`` / ``completed``.
- At most one item may be ``in_progress`` at a time.

State lives on :attr:`AgentContext.todos`. After a successful write the
tool emits an ``anila.todos_updated`` SSE event via
:attr:`AgentContext.event_emitter` (when the server installed one); the
UI re-renders its task board from the payload.

When no AgentContext is bound (e.g. unit tests calling the impl
directly) the tool short-circuits and just echoes the validated todos
back to the model so it still sees a useful tool result.
"""

from __future__ import annotations

from typing import Any

from ..context.agent_context import get_current_context
from ..models.agent import Todo
from ..models.tool import ToolDefinition, ToolSafety


TOOL_NAME = "todo_write"


DESCRIPTION = (
    "Replace the agent's task board with the given list of todos. Use "
    "proactively for any multi-step task: capture the steps before you "
    "start, mark exactly one as in_progress while you work on it, and "
    "mark items completed as you finish them. Skip for trivial single "
    "or two-step tasks."
)


_VALID_STATUSES: set[str] = {"pending", "in_progress", "completed"}


INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "todos": {
            "type": "array",
            "description": "Replacement task list (full replace, not patch).",
            "items": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": (
                            "Imperative form, e.g. 'Run the test suite'. "
                            "Shown when the task is pending or done."
                        ),
                    },
                    "active_form": {
                        "type": "string",
                        "description": (
                            "Present-continuous form, e.g. 'Running the "
                            "test suite'. Shown while the task is in_progress."
                        ),
                    },
                    "status": {
                        "type": "string",
                        "enum": sorted(_VALID_STATUSES),
                        "default": "pending",
                    },
                },
                "required": ["content", "active_form"],
            },
        }
    },
    "required": ["todos"],
}


class TodoValidationError(ValueError):
    """Raised when the LLM produces a malformed todo list."""


def _validate(items: list[dict[str, Any]]) -> list[Todo]:
    """Normalise + validate. Returns frozen Todo records."""
    todos: list[Todo] = []
    in_progress_seen = 0
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            raise TodoValidationError(
                f"todos[{idx}] must be an object, got {type(item).__name__}"
            )
        content = str(item.get("content", "")).strip()
        active_form = str(item.get("active_form", "")).strip()
        status_str = str(item.get("status", "pending")).strip() or "pending"
        if not content:
            raise TodoValidationError(f"todos[{idx}].content is required")
        if not active_form:
            raise TodoValidationError(
                f"todos[{idx}].active_form is required (present-continuous form)"
            )
        if status_str not in _VALID_STATUSES:
            raise TodoValidationError(
                f"todos[{idx}].status must be one of "
                f"{sorted(_VALID_STATUSES)}; got {status_str!r}"
            )
        if status_str == "in_progress":
            in_progress_seen += 1
        todos.append(
            Todo(
                content=content,
                active_form=active_form,
                status=status_str,  # type: ignore[arg-type]
            )
        )
    if in_progress_seen > 1:
        raise TodoValidationError(
            f"At most one todo may be in_progress at a time; "
            f"got {in_progress_seen}. Mark the others as pending."
        )
    return todos


async def _todo_write_impl(input: dict[str, Any], **_: Any) -> str:
    raw_items = input.get("todos") or []
    if not isinstance(raw_items, list):
        return (
            f"todo_write error: 'todos' must be an array, "
            f"got {type(raw_items).__name__}"
        )
    try:
        validated = _validate(list(raw_items))
    except TodoValidationError as exc:
        return f"todo_write error: {exc}"

    ctx = get_current_context()
    if ctx is not None:
        ctx.todos = validated
        if ctx.event_emitter is not None:
            await ctx.event_emitter(
                "todos_updated",
                {"todos": [t.model_dump() for t in validated]},
            )

    summary = ", ".join(
        f"[{t.status[:1]}] {t.content}" for t in validated
    ) or "(empty list)"
    return f"todos updated ({len(validated)} item(s)): {summary}"


def todo_write_tool() -> ToolDefinition:
    return ToolDefinition(
        name=TOOL_NAME,
        description=DESCRIPTION,
        input_schema=INPUT_SCHEMA,
        safety=ToolSafety.READ_ONLY,
        implementation=_todo_write_impl,
    )


__all__ = [
    "TOOL_NAME",
    "TodoValidationError",
    "todo_write_tool",
]
