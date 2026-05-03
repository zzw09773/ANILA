"""``enter_plan_mode`` / ``exit_plan_mode`` tools — propose-then-execute flow.

Plan mode lets the model **draft** a plan without committing to destructive
actions until the user approves. Two tools:

- :func:`enter_plan_mode_tool` — flips
  ``AgentContext.plan_mode = True``. Subsequent tool calls that are
  classified as DESTRUCTIVE will be rejected by
  :class:`anila_core.router.tool_router.ToolRegistry` with a helpful
  message; the model can still use READ_ONLY / CONCURRENCY_SAFE tools to
  research the plan.
- :func:`exit_plan_mode_tool` — surfaces the drafted plan to the user via
  an :class:`InterruptItem` of kind ``"plan"``. The run loop pauses; on
  approval the plan_mode flag is reset and the model continues.

Mirrors Claude Code's ``EnterPlanModeTool`` / ``ExitPlanModeTool`` shape
without their CLI rendering. Web UIs render the plan from the
``INTERRUPT_REQUESTED`` SSE payload.
"""

from __future__ import annotations

from typing import Any

from ..context.agent_context import get_current_context
from ..models.interrupt import InterruptItem
from ..models.tool import ToolDefinition, ToolSafety


# ---------------------------------------------------------------------------
# enter_plan_mode
# ---------------------------------------------------------------------------


ENTER_TOOL_NAME = "enter_plan_mode"

ENTER_DESCRIPTION = (
    "Switch into plan mode: only read-only tools may run, and any "
    "destructive action must be proposed via exit_plan_mode for user "
    "approval first. Use this when the user asks you to plan, design, "
    "or analyse before changing anything."
)


ENTER_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}


async def _enter_plan_mode_impl(input: dict[str, Any], **_: Any) -> str:
    ctx = get_current_context()
    if ctx is None:
        # No AgentContext bound — gating won't fire, but the model still gets
        # a usable acknowledgement so it knows to plan rather than act.
        return (
            "plan_mode_entered (note: no AgentContext active, "
            "destructive-tool gating is not in effect)"
        )
    ctx.plan_mode = True
    return "plan_mode_entered"


def enter_plan_mode_tool() -> ToolDefinition:
    return ToolDefinition(
        name=ENTER_TOOL_NAME,
        description=ENTER_DESCRIPTION,
        input_schema=ENTER_INPUT_SCHEMA,
        safety=ToolSafety.READ_ONLY,
        implementation=_enter_plan_mode_impl,
    )


# ---------------------------------------------------------------------------
# exit_plan_mode
# ---------------------------------------------------------------------------


EXIT_TOOL_NAME = "exit_plan_mode"

EXIT_DESCRIPTION = (
    "Surface a finalised plan to the user for approval. The run pauses; "
    "the user sees the plan rendered (markdown) with approve/reject "
    "controls. On approval, plan mode exits and you may execute the plan."
)


EXIT_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "plan": {
            "type": "string",
            "description": (
                "Markdown-formatted plan. Be concrete: list each step "
                "you intend to take and which tool you will use."
            ),
        },
    },
    "required": ["plan"],
}


async def _exit_plan_mode_impl(input: dict[str, Any], **_: Any) -> InterruptItem:
    """Pause for plan approval. Plan mode flag is cleared on resume."""
    plan_text = str(input.get("plan", "")).strip()
    return InterruptItem(
        kind="plan",
        payload={"plan": plan_text},
    )


def exit_plan_mode_tool() -> ToolDefinition:
    return ToolDefinition(
        name=EXIT_TOOL_NAME,
        description=EXIT_DESCRIPTION,
        input_schema=EXIT_INPUT_SCHEMA,
        safety=ToolSafety.READ_ONLY,
        implementation=_exit_plan_mode_impl,
    )


# ---------------------------------------------------------------------------
# Helper for tools that want to honor plan mode on their own
# ---------------------------------------------------------------------------


def is_plan_mode_active() -> bool:
    """Return True iff the current AgentContext has plan_mode set.

    Tool implementations that aren't classified DESTRUCTIVE but still
    want to respect plan mode (e.g. an HTTP-mutating client) can call
    this directly.
    """
    ctx = get_current_context()
    return bool(ctx and ctx.plan_mode)
