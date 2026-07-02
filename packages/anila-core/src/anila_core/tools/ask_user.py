"""``ask_user`` tool — pause the run loop to ask the user a structured question.

The tool implementation is trivial: it returns an
:class:`anila_core.models.interrupt.InterruptItem`. QueryEngine sees the
:class:`~anila_core.models.message.ToolResult` carries an interrupt,
persists conversation state to the active :class:`Session`, and raises
:class:`~anila_core.engine.approvals.RunPaused`.

The HTTP layer (``api/server.py``) catches RunPaused, emits an
``INTERRUPT_REQUESTED`` SSE event with the payload, and waits for the
caller to ``POST /v1/sessions/{id}/answer``. See Sprint 9 plan.

Schema mirrors Claude Code's ``AskUserQuestion`` tool — multiple-choice
with optional free-form ``other_text``. Adapt the rendering convention
in :func:`anila_core.engine.approvals._render_answer` if you change the
answer shape.
"""

from __future__ import annotations

from typing import Any

from ..models.interrupt import InterruptItem
from ..models.tool import ToolDefinition, ToolSafety


TOOL_NAME = "ask_user"

DESCRIPTION = (
    "Ask the user a multiple-choice question to clarify requirements, "
    "preferences, or decisions during execution. The run pauses until "
    "the user answers; their selection is then fed back as your tool "
    "result on the next turn. Users can always pick 'other' to provide "
    "free-form text."
)


# JSON Schema for the tool's input. Keep parameter names stable — the
# Web UI (or whatever consumes INTERRUPT_REQUESTED) renders directly
# from this shape.
INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "question": {
            "type": "string",
            "description": "The question to show the user. Keep it short.",
        },
        "options": {
            "type": "array",
            "description": (
                "Choices the user picks from. If you have a recommended "
                "option, put it first and append '(Recommended)' to its label."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "label": {
                        "type": "string",
                        "description": "Short label rendered to the user.",
                    },
                    "value": {
                        "type": "string",
                        "description": (
                            "Stable identifier returned in the answer. "
                            "Defaults to label when omitted."
                        ),
                    },
                    "description": {
                        "type": "string",
                        "description": (
                            "Optional secondary text (one line, "
                            "renders below the label)."
                        ),
                    },
                },
                "required": ["label"],
            },
            "minItems": 2,
        },
        "multi_select": {
            "type": "boolean",
            "description": (
                "When true, the user may pick multiple options. Default false."
            ),
            "default": False,
        },
    },
    "required": ["question", "options"],
}


async def _ask_user_impl(input: dict[str, Any], **_: Any) -> InterruptItem:
    """Tool body — return InterruptItem to pause the run loop.

    The InterruptItem.payload is what the UI receives in the
    ``anila.interrupt_requested`` SSE event.
    """
    question = str(input.get("question", "")).strip()
    raw_options = input.get("options") or []
    multi_select = bool(input.get("multi_select", False))

    options: list[dict[str, Any]] = []
    for opt in raw_options:
        if not isinstance(opt, dict):
            continue
        label = str(opt.get("label", "")).strip()
        if not label:
            continue
        options.append(
            {
                "label": label,
                "value": str(opt.get("value", label)),
                "description": str(opt.get("description", "")),
            }
        )

    return InterruptItem(
        kind="ask_user",
        payload={
            "question": question,
            "options": options,
            "multi_select": multi_select,
            # ``allow_other`` is always-true here for now; it's exposed in the
            # payload so future UIs can decide whether to render the "other"
            # input. Toggling it off requires a tool-side opt-out.
            "allow_other": True,
        },
    )


def ask_user_tool() -> ToolDefinition:
    """Build the ToolDefinition. Register on each agent's ToolRegistry.

    Example::

        registry.register(ask_user_tool())
    """
    return ToolDefinition(
        name=TOOL_NAME,
        description=DESCRIPTION,
        input_schema=INPUT_SCHEMA,
        safety=ToolSafety.READ_ONLY,
        implementation=_ask_user_impl,
    )
