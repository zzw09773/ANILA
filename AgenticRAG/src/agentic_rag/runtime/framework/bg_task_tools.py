"""LLM-callable tool Actions for BG_TASK runtime control.

After an LLM spawns a BG_TASK (e.g. ``rebuild_vector_index``) it
needs to inspect / control progress without blocking. These tool
factories give the LLM the surface:

- ``check_bg_task(task_id, tail_chars)`` — non-blocking status +
  recent output tail
- ``cancel_bg_task(task_id)`` — best-effort cancel
- ``list_bg_tasks(state)`` — enumerate active or filtered handles

Wire them into a coordinator-style agent the same way as
coordinator_tools::

    bg_runner = BgTaskRunner(output_dir="/var/agent/bg")
    actions = make_bg_task_actions(bg_runner)
    runner = Runner(bg_task_runner=bg_runner)
    agent = Agent(name="ops", instructions=..., provider=p, model="m",
                  actions=tuple([rebuild_index_action, *actions]))
"""

from __future__ import annotations

from agentic_rag.runtime.framework.action import (
    Action,
    ActionContext,
    ActionKind,
    ActionResult,
    SideEffectClass,
)
from agentic_rag.runtime.framework.bg_task import BgTaskRunner, BgTaskState


# ── check_bg_task ────────────────────────────────────────────────────


def make_check_bg_task_action(
    bg_runner: BgTaskRunner,
    *,
    name: str = "check_bg_task",
) -> Action:
    """Returns an Action that snapshots one BG task's state + output tail.

    Output dict shape mirrors ``BgTaskHandle.to_summary()`` plus an
    ``output_tail`` field with the last ``tail_chars`` of recorded
    output (capped to keep the LLM context bounded).
    """

    async def _handler(ctx: ActionContext) -> ActionResult:
        task_id = ctx.params.get("task_id")
        if not isinstance(task_id, str) or not task_id.strip():
            return ActionResult(error="task_id is required (string)")
        tail_chars = int(ctx.params.get("tail_chars", 4_000))
        if tail_chars < 0:
            tail_chars = 0
        handle = bg_runner.get(task_id)
        if handle is None:
            return ActionResult(error=f"unknown bg task_id {task_id!r}")
        snapshot = handle.to_summary()
        snapshot["output_tail"] = handle.output_tail(tail_chars=tail_chars)
        return ActionResult(output=snapshot)

    return Action(
        name=name,
        description=(
            "Inspect a background task by id. Returns state + recent output tail. "
            "Non-blocking — safe to call repeatedly while the task is RUNNING."
        ),
        kind=ActionKind.SYNC_TOOL,
        handler=_handler,
        side_effect_class=SideEffectClass.PURE,
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Bg task id returned by the original spawning tool.",
                },
                "tail_chars": {
                    "type": "integer",
                    "description": (
                        "Maximum chars of recent output to return (default 4000)."
                    ),
                    "default": 4000,
                },
            },
            "required": ["task_id"],
            "additionalProperties": False,
        },
    )


# ── cancel_bg_task ───────────────────────────────────────────────────


def make_cancel_bg_task_action(
    bg_runner: BgTaskRunner,
    *,
    name: str = "cancel_bg_task",
) -> Action:
    """Returns an Action that best-effort cancels a BG task by id."""

    async def _handler(ctx: ActionContext) -> ActionResult:
        task_id = ctx.params.get("task_id")
        if not isinstance(task_id, str) or not task_id.strip():
            return ActionResult(error="task_id is required (string)")
        cancelled = bg_runner.cancel(task_id)
        handle = bg_runner.get(task_id)
        return ActionResult(
            output={
                "task_id": task_id,
                "cancelled": cancelled,
                "state": handle.state.value if handle else "unknown",
            }
        )

    return Action(
        name=name,
        description=(
            "Cancel a running background task. Returns cancelled=False if the "
            "task is already finished."
        ),
        kind=ActionKind.SYNC_TOOL,
        handler=_handler,
        side_effect_class=SideEffectClass.NETWORKED,
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
            },
            "required": ["task_id"],
            "additionalProperties": False,
        },
    )


# ── list_bg_tasks ────────────────────────────────────────────────────


def make_list_bg_tasks_action(
    bg_runner: BgTaskRunner,
    *,
    name: str = "list_bg_tasks",
) -> Action:
    """Returns an Action that enumerates BG task handles, optionally filtered.

    ``state`` filter is one of ``pending|running|completed|failed|cancelled``
    or omitted to list every handle the runner knows about.
    """

    async def _handler(ctx: ActionContext) -> ActionResult:
        state_filter = ctx.params.get("state")
        target_state: BgTaskState | None = None
        if state_filter is not None:
            if not isinstance(state_filter, str):
                return ActionResult(error="state must be a string if supplied")
            try:
                target_state = BgTaskState(state_filter)
            except ValueError:
                return ActionResult(
                    error=f"unknown state {state_filter!r}; allowed: "
                    f"{[s.value for s in BgTaskState]}"
                )
        handles = bg_runner.list_handles(state=target_state)
        return ActionResult(
            output={
                "count": len(handles),
                "tasks": [h.to_summary() for h in handles],
            }
        )

    return Action(
        name=name,
        description=(
            "List background tasks, optionally filtered by state. "
            "Useful for the LLM to recover task ids it lost across turns."
        ),
        kind=ActionKind.SYNC_TOOL,
        handler=_handler,
        side_effect_class=SideEffectClass.PURE,
        input_schema={
            "type": "object",
            "properties": {
                "state": {
                    "type": "string",
                    "description": (
                        "Filter to one state: pending|running|completed|failed|"
                        "cancelled. Omit to list everything."
                    ),
                },
            },
            "additionalProperties": False,
        },
    )


# ── Convenience: full set ────────────────────────────────────────────


def make_bg_task_actions(bg_runner: BgTaskRunner) -> list[Action]:
    """Returns the three canonical BG-task control Actions in one call."""
    return [
        make_check_bg_task_action(bg_runner),
        make_cancel_bg_task_action(bg_runner),
        make_list_bg_tasks_action(bg_runner),
    ]


__all__ = [
    "make_bg_task_actions",
    "make_cancel_bg_task_action",
    "make_check_bg_task_action",
    "make_list_bg_tasks_action",
]
