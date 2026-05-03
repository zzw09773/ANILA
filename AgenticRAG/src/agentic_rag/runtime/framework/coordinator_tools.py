"""Coordinator-tool Actions the LLM-driven coordinator agent uses.

The Coordinator class (``coordinator.py``) is the runtime; this
module exposes its capabilities AS Actions the coordinator agent
can call from its prompt. The coordinator agent is just a regular
``Agent`` whose action registry contains these factories' output.

Three actions:

- ``spawn_worker(agent_type, prompt, is_read_only)`` — fire-and-track,
  returns ``task_id`` immediately. Read-only spawns are
  ``parallel_safe=True`` by default.
- ``check_worker(task_id)`` — non-blocking status query. Returns the
  worker's current ``TaskNotification`` (status, summary, partial
  result if available).
- ``wait_for_workers(task_ids?)`` — blocking wait. With no ids,
  waits for every spawned task; with ids, waits for that subset.
  Returns the joined notification block (parseable by
  ``task_notification.parse_all``).

The coordinator agent typically uses them in the pattern::

    1. spawn_worker for each parallel sub-task
    2. wait_for_workers
    3. read the joined notifications
    4. summarise / synthesise final answer to the user

Restrict the worker types the coordinator can spawn via the
``allowed_worker_types`` argument — defaults to all registered types.
"""

from __future__ import annotations

from collections.abc import Iterable

from agentic_rag.runtime.framework.action import (
    Action,
    ActionContext,
    ActionKind,
    ActionResult,
    SideEffectClass,
)
from agentic_rag.runtime.framework.coordinator import Coordinator, WorkerState
from agentic_rag.runtime.framework.task_notification import (
    build_task_notification,
    collect_for_summary,
)


# ── spawn_worker ─────────────────────────────────────────────────────


def make_spawn_worker_action(
    coordinator: Coordinator,
    *,
    allowed_worker_types: Iterable[str] | None = None,
    name: str = "spawn_worker",
) -> Action:
    """Build the ``spawn_worker`` Action bound to ``coordinator``.

    Returned ``ActionResult.output`` shape::

        {"task_id": "task_abc123", "agent_type": "verifier", "state": "pending"}

    The coordinator agent typically captures the ``task_id`` and feeds
    it into a later ``wait_for_workers`` or ``check_worker`` call.

    ``allowed_worker_types`` restricts which worker types this Action
    will accept. None means "all currently-registered types"; pass a
    subset to lock the coordinator agent to specific worker pools.
    """
    allowed = set(allowed_worker_types) if allowed_worker_types is not None else None

    async def _handler(ctx: ActionContext) -> ActionResult:
        params = ctx.params
        agent_type = params.get("agent_type")
        prompt = params.get("prompt")
        is_read_only = bool(params.get("is_read_only", True))

        if not isinstance(agent_type, str) or not agent_type.strip():
            return ActionResult(error="agent_type is required (string)")
        if not isinstance(prompt, str) or not prompt.strip():
            return ActionResult(error="prompt is required (string)")

        registered = coordinator.worker_types
        if agent_type not in registered:
            return ActionResult(
                error=f"unknown agent_type {agent_type!r}; registered: {registered}"
            )
        if allowed is not None and agent_type not in allowed:
            return ActionResult(
                error=f"agent_type {agent_type!r} not allowed for this coordinator; "
                f"allowed: {sorted(allowed)}"
            )

        task = coordinator.spawn_worker(
            agent_type, prompt, parallel_safe=is_read_only
        )
        return ActionResult(
            output={
                "task_id": task.task_id,
                "agent_type": agent_type,
                "state": task.state.value,
            }
        )

    return Action(
        name=name,
        description=(
            "Spawn a worker sub-agent. Returns a task_id for later "
            "checking / waiting. Read-only workers are parallel-safe; "
            "set is_read_only=false for write-side work."
        ),
        kind=ActionKind.SYNC_TOOL,
        handler=_handler,
        side_effect_class=SideEffectClass.NETWORKED,
        input_schema={
            "type": "object",
            "properties": {
                "agent_type": {
                    "type": "string",
                    "description": "Registered worker agent type to spawn.",
                },
                "prompt": {
                    "type": "string",
                    "description": "Task prompt the worker will execute.",
                },
                "is_read_only": {
                    "type": "boolean",
                    "description": "True (default) marks the worker parallel-safe.",
                    "default": True,
                },
            },
            "required": ["agent_type", "prompt"],
            "additionalProperties": False,
        },
    )


# ── check_worker ─────────────────────────────────────────────────────


def make_check_worker_action(
    coordinator: Coordinator,
    *,
    name: str = "check_worker",
) -> Action:
    """Non-blocking status query for one task."""

    async def _handler(ctx: ActionContext) -> ActionResult:
        task_id = ctx.params.get("task_id")
        if not isinstance(task_id, str) or not task_id.strip():
            return ActionResult(error="task_id is required (string)")
        if task_id not in coordinator.tasks:
            return ActionResult(error=f"unknown task_id {task_id!r}")
        task = coordinator.tasks[task_id]
        notif = task.to_notification()
        return ActionResult(
            output={
                "task_id": task.task_id,
                "state": task.state.value,
                "is_done": task.is_done(),
                "notification": build_task_notification(
                    notif.task_id, notif.status, notif.summary, notif.result
                ),
            }
        )

    return Action(
        name=name,
        description="Check current state of a worker task by id (non-blocking).",
        kind=ActionKind.SYNC_TOOL,
        handler=_handler,
        side_effect_class=SideEffectClass.PURE,
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "ID returned by a previous spawn_worker call.",
                },
            },
            "required": ["task_id"],
            "additionalProperties": False,
        },
    )


# ── wait_for_workers ─────────────────────────────────────────────────


def make_wait_for_workers_action(
    coordinator: Coordinator,
    *,
    name: str = "wait_for_workers",
) -> Action:
    """Blocking wait for a list of tasks (or all) — returns joined notifications.

    Returned output shape::

        {
          "completed": ["task_abc", "task_def"],
          "failed":    ["task_xyz"],
          "notifications": "<task-notification ...>...</task-notification>\\n\\n..."
        }

    The coordinator agent reads ``notifications`` directly into its
    next-turn prompt — it's already pre-formatted XML the LLM
    recognises from earlier tool results.
    """

    async def _handler(ctx: ActionContext) -> ActionResult:
        raw_ids = ctx.params.get("task_ids")
        target_ids: list[str] | None = None
        if raw_ids is not None:
            if not isinstance(raw_ids, list) or not all(
                isinstance(x, str) for x in raw_ids
            ):
                return ActionResult(error="task_ids must be a list of strings")
            target_ids = list(raw_ids)
            unknown = [t for t in target_ids if t not in coordinator.tasks]
            if unknown:
                return ActionResult(error=f"unknown task_ids: {unknown}")

        tasks = await coordinator.wait_all(target_ids)
        notifs = [t.to_notification() for t in tasks]
        completed = [
            t.task_id for t in tasks if t.state is WorkerState.COMPLETED
        ]
        failed = [t.task_id for t in tasks if t.state is WorkerState.FAILED]
        cancelled = [t.task_id for t in tasks if t.state is WorkerState.CANCELLED]

        return ActionResult(
            output={
                "completed": completed,
                "failed": failed,
                "cancelled": cancelled,
                "notifications": collect_for_summary(notifs),
            }
        )

    return Action(
        name=name,
        description=(
            "Wait for worker tasks to finish. Pass task_ids to wait for a "
            "subset; omit to wait for every spawned task. Returns "
            "completed/failed lists + joined task notifications."
        ),
        kind=ActionKind.SYNC_TOOL,
        handler=_handler,
        side_effect_class=SideEffectClass.NETWORKED,
        input_schema={
            "type": "object",
            "properties": {
                "task_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Specific task ids to wait for. Omit to wait for all "
                        "currently-spawned tasks."
                    ),
                },
            },
            "additionalProperties": False,
        },
    )


# ── Convenience: build the full coordinator-tool set ─────────────────


def make_coordinator_actions(
    coordinator: Coordinator,
    *,
    allowed_worker_types: Iterable[str] | None = None,
) -> list[Action]:
    """Returns the three canonical coordinator-tool Actions in one call.

    Use when wiring a coordinator agent::

        coord = Coordinator(workers={...})
        coord_agent = Agent(
            name="coordinator",
            instructions="You decompose user requests into worker tasks. "
                         "Use spawn_worker / check_worker / wait_for_workers.",
            provider=p, model="gemma4",
            actions=tuple(make_coordinator_actions(coord)),
        )
    """
    return [
        make_spawn_worker_action(coordinator, allowed_worker_types=allowed_worker_types),
        make_check_worker_action(coordinator),
        make_wait_for_workers_action(coordinator),
    ]


__all__ = [
    "make_check_worker_action",
    "make_coordinator_actions",
    "make_spawn_worker_action",
    "make_wait_for_workers_action",
]
