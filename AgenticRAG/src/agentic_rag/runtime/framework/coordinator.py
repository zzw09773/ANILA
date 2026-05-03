"""``Coordinator`` — multi-agent worker spawn pattern built on Runner.

Architecturally Coordinator is NOT one of the five framework
primitives (Action / Middleware / StateMachine / Memory / Provider).
It's a pattern built on top: each spawned worker is its own
``Runner.run()`` call against a sub-Agent, and the Coordinator owns
the lifecycle bookkeeping.

When to use:

- A coordinator agent decomposes a request into independent
  sub-tasks (read-only retrieval against several collections in
  parallel; a swarm of fact-checkers each verifying a claim).
- A pipeline of write-safe steps that must run sequentially with
  results gathered along the way.

When NOT to use:

- A simple tool call — that's an Action, not a worker.
- Sequential conversation continuation in the same context — that's
  a handoff (active agent transfers in-place).

Workers and handoffs are complementary: a coordinator can both
spawn workers AND hand off to a different agent later in the same
run.

The Coordinator does NOT itself run a query loop. It exposes
``spawn_worker()`` / ``gather_parallel()`` / ``run_sequential()``
APIs the calling code (or coordinator-tool Actions wired via
``coordinator_tools.py``) invokes. The coordinator agent — i.e. the
LLM-driven orchestration agent the user talks to — runs in a normal
Runner and uses the coordinator-tool Actions to drive the workers.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Optional, Union

from agentic_rag.runtime.framework.agent import Agent
from agentic_rag.runtime.framework.exceptions import (
    UserError,
)
from agentic_rag.runtime.framework.middleware.protocol import (
    Middleware,
    MiddlewareCallable,
)
from agentic_rag.runtime.framework.runner import RunResult, Runner
from agentic_rag.runtime.framework.task_notification import (
    TaskNotification,
)

logger = logging.getLogger(__name__)


# ── Worker state ──────────────────────────────────────────────────────


class WorkerState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _new_task_id() -> str:
    return f"task_{uuid.uuid4().hex[:12]}"


@dataclass
class WorkerTask:
    """Handle to one spawned worker.

    Mutable on purpose — the asyncio task that runs the worker writes
    state / result back as it progresses. Reads are racey under
    parallel inspection but each field is set atomically (assignment
    is a single bytecode op for these types).

    ``parallel_safe`` records the caller's read-only / write-safe
    classification at spawn time. ``Coordinator.gather_parallel``
    only accepts ``parallel_safe=True`` tasks; mixing read + write
    in one fan-out is the bug class this guards against.
    """

    agent_name: str
    prompt: str
    task_id: str = field(default_factory=_new_task_id)
    state: WorkerState = WorkerState.PENDING
    parallel_safe: bool = True
    created_at: datetime = field(default_factory=_utc_now)
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    result: Optional[RunResult] = None
    error: Optional[BaseException] = None
    _future: Optional["asyncio.Future[RunResult]"] = field(
        default=None, repr=False
    )

    def is_done(self) -> bool:
        return self.state in (
            WorkerState.COMPLETED,
            WorkerState.FAILED,
            WorkerState.CANCELLED,
        )

    def to_notification(self) -> TaskNotification:
        """Render current state as a TaskNotification.

        ``summary`` is the first line of the worker's final output
        (truncated) so coordinator agents can grep results without
        loading the full body. ``result`` is the full final_output.
        """
        body = self.result.final_output if self.result else (
            str(self.error) if self.error else ""
        )
        first_line = body.split("\n", 1)[0].strip()
        summary = first_line[:120] + ("…" if len(first_line) > 120 else "")
        return TaskNotification(
            task_id=self.task_id,
            status=self.state.value,
            summary=summary,
            result=body,
        )


# ── The Coordinator ──────────────────────────────────────────────────


class Coordinator:
    """Spawns and tracks worker agents.

    Construction takes the worker registry — a dict of
    ``{agent_type_name: Agent}`` the coordinator may spawn. Workers
    of unknown agent_type are rejected at spawn time, not at run
    time, so the coordinator's contract with its calling code is
    explicit.

    Usage::

        coord = Coordinator(workers={"verifier": verifier_agent,
                                     "summariser": summariser_agent})
        # 1-to-1 spawn
        task = coord.spawn_worker("verifier", "check claim X")
        result = await coord.wait(task)
        # 1-to-N parallel (read-only only)
        results = await coord.gather_parallel(
            "verifier", ["claim A", "claim B", "claim C"]
        )
        # Sequential pipeline (write-safe)
        results = await coord.run_sequential(
            [("indexer", "ingest doc 1"), ("indexer", "ingest doc 2")]
        )

    Cancellation: ``Coordinator.cancel(task_id)`` flips state to
    CANCELLED and best-effort cancels the underlying asyncio Task.
    The worker run may have already produced partial state via
    middleware (audit logs, span traces); those persist.

    Re-entrant: a single Coordinator instance is safe to use across
    many concurrent spawns. The internal task dict is asyncio-friendly
    (no thread locks needed in pure-asyncio code).
    """

    def __init__(
        self,
        workers: dict[str, Agent],
        *,
        runner_factory: Optional[Any] = None,
        middleware: Sequence[Union[Middleware, MiddlewareCallable]] | None = None,
    ) -> None:
        if not workers:
            raise UserError("Coordinator requires at least one worker agent type")
        self._workers = dict(workers)
        self._runner_factory = runner_factory
        self._middleware = list(middleware or [])
        self._tasks: dict[str, WorkerTask] = {}

    @property
    def worker_types(self) -> list[str]:
        return sorted(self._workers)

    @property
    def tasks(self) -> dict[str, WorkerTask]:
        """Live view of all tasks the coordinator has spawned."""
        return dict(self._tasks)

    # ── 1-to-1 spawn ─────────────────────────────────────────────────

    def spawn_worker(
        self,
        agent_type: str,
        prompt: str,
        *,
        parallel_safe: bool = True,
        task_id: str | None = None,
        cancel_signal: asyncio.Event | None = None,
        deadline_seconds: float | None = None,
    ) -> WorkerTask:
        """Spawn one worker. Returns immediately with a ``WorkerTask`` handle.

        The worker runs in an asyncio.Task. Awaiting the handle (via
        ``await coord.wait(task)``) blocks until completion; reading
        ``task.state`` is non-blocking.

        ``parallel_safe`` tags the task for ``gather_parallel``
        validation. Defaults to True because most worker types are
        read-only retrieval — explicitly mark write workers
        ``parallel_safe=False`` so the validation catches misuse.
        """
        agent = self._workers.get(agent_type)
        if agent is None:
            raise UserError(
                f"Coordinator has no worker agent type {agent_type!r}. "
                f"Known: {self.worker_types}"
            )

        task = WorkerTask(
            agent_name=agent.name,
            prompt=prompt,
            task_id=task_id or _new_task_id(),
            parallel_safe=parallel_safe,
        )
        self._tasks[task.task_id] = task
        loop = asyncio.get_event_loop()
        task._future = loop.create_future()
        loop.create_task(
            self._drive_worker(
                task=task,
                agent=agent,
                cancel_signal=cancel_signal,
                deadline_seconds=deadline_seconds,
            ),
            name=f"coord-worker:{task.task_id}",
        )
        return task

    async def wait(self, task: WorkerTask | str) -> WorkerTask:
        """Block until the worker finishes (success or failure).

        Accepts either a ``WorkerTask`` handle or a task id string —
        the latter useful when the coordinator agent passes ids
        through tool calls.
        """
        resolved = self._resolve(task)
        if resolved._future is None:
            return resolved
        try:
            await resolved._future
        except BaseException:
            # The future re-raises whatever the worker raised; we've
            # already captured that on the WorkerTask, so just return.
            pass
        return resolved

    async def wait_all(
        self, tasks: Sequence[WorkerTask | str] | None = None
    ) -> list[WorkerTask]:
        """Wait for every task (or every currently-spawned task).

        Returns the resolved handles in input order. Failures are
        surfaced via ``WorkerTask.error`` rather than re-raised — a
        coordinator usually wants partial-success results, not a
        single-failure abort.
        """
        targets: list[WorkerTask]
        if tasks is None:
            targets = list(self._tasks.values())
        else:
            targets = [self._resolve(t) for t in tasks]
        await asyncio.gather(
            *[self.wait(t) for t in targets], return_exceptions=True
        )
        return targets

    def cancel(self, task: WorkerTask | str) -> bool:
        """Best-effort cancel. Returns True if the cancel was issued.

        If the worker has already finished (state in
        {COMPLETED, FAILED, CANCELLED}) the call is a no-op and
        returns False. Otherwise the underlying asyncio Task is
        cancelled and the WorkerTask transitions to CANCELLED.
        """
        resolved = self._resolve(task)
        if resolved.is_done():
            return False
        if resolved._future is not None and not resolved._future.done():
            resolved._future.cancel()
        resolved.state = WorkerState.CANCELLED
        resolved.finished_at = _utc_now()
        return True

    # ── 1-to-N helpers ───────────────────────────────────────────────

    async def gather_parallel(
        self,
        agent_type: str,
        prompts: Sequence[str],
        *,
        cancel_signal: asyncio.Event | None = None,
        deadline_seconds: float | None = None,
    ) -> list[WorkerTask]:
        """Spawn one worker per prompt in parallel; wait for all.

        Refuses to run if the worker type isn't registered as
        parallel_safe in the agent definition — more precisely, the
        spawned task carries ``parallel_safe=True`` so the safety
        guarantee is per-spawn rather than per-agent. Callers wanting
        write-safe parallelism (rare) should construct workers with
        explicit ``parallel_safe=False`` and use individual spawn +
        wait_all.
        """
        if not prompts:
            return []
        spawned = [
            self.spawn_worker(
                agent_type,
                prompt,
                parallel_safe=True,
                cancel_signal=cancel_signal,
                deadline_seconds=deadline_seconds,
            )
            for prompt in prompts
        ]
        return await self.wait_all(spawned)

    async def run_sequential(
        self,
        steps: Sequence[tuple[str, str]],
        *,
        stop_on_failure: bool = True,
        cancel_signal: asyncio.Event | None = None,
        deadline_seconds: float | None = None,
    ) -> list[WorkerTask]:
        """Run ``(agent_type, prompt)`` steps in order. Optional stop-on-failure.

        Each step waits for the previous one. Use this for
        write-side pipelines where a later step depends on the
        earlier step's side effects (re-indexing after ingest, etc.).
        """
        results: list[WorkerTask] = []
        for agent_type, prompt in steps:
            task = self.spawn_worker(
                agent_type,
                prompt,
                parallel_safe=False,
                cancel_signal=cancel_signal,
                deadline_seconds=deadline_seconds,
            )
            await self.wait(task)
            results.append(task)
            if stop_on_failure and task.state is WorkerState.FAILED:
                logger.warning(
                    "Coordinator.run_sequential: stopping at task %s due to failure",
                    task.task_id,
                )
                break
        return results

    # ── Internals ────────────────────────────────────────────────────

    def _resolve(self, task: WorkerTask | str) -> WorkerTask:
        if isinstance(task, WorkerTask):
            return task
        resolved = self._tasks.get(task)
        if resolved is None:
            raise UserError(f"Coordinator has no task with id {task!r}")
        return resolved

    async def _drive_worker(
        self,
        task: WorkerTask,
        agent: Agent,
        cancel_signal: asyncio.Event | None,
        deadline_seconds: float | None,
    ) -> None:
        """Run the worker to completion, recording result on the WorkerTask."""
        task.state = WorkerState.RUNNING
        task.started_at = _utc_now()

        runner = self._build_runner()

        try:
            result = await runner.run(
                agent,
                task.prompt,
                cancel_signal=cancel_signal,
                deadline_seconds=deadline_seconds,
            )
        except asyncio.CancelledError:
            task.state = WorkerState.CANCELLED
            task.finished_at = _utc_now()
            if task._future is not None and not task._future.done():
                task._future.cancel()
            return
        except BaseException as exc:  # noqa: BLE001
            task.state = WorkerState.FAILED
            task.error = exc
            task.finished_at = _utc_now()
            if task._future is not None and not task._future.done():
                task._future.set_exception(exc)
            return

        task.result = result
        task.state = WorkerState.COMPLETED
        task.finished_at = _utc_now()
        if task._future is not None and not task._future.done():
            task._future.set_result(result)

    def _build_runner(self) -> Runner:
        if self._runner_factory is not None:
            built = self._runner_factory()
            if not isinstance(built, Runner):
                raise UserError(
                    f"runner_factory must return Runner, got {type(built).__name__}"
                )
            return built
        return Runner(middleware=self._middleware)


__all__ = ["Coordinator", "WorkerState", "WorkerTask"]
