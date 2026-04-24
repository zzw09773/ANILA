"""Coordinator mode — multi-agent task decomposition and worker spawning.

The Coordinator is an optional layer on top of QueryEngine. When enabled:
  - The coordinator agent decomposes the user's request into tasks
  - Worker agents execute individual tasks
  - Results are synthesized back to the coordinator

Worker spawn rules:
  - Read-only tasks: spawn in parallel
  - Write tasks: execute sequentially

Task notification XML format:
  <task-notification task-id="..." status="..." summary="...">result</task-notification>

SendMessage/TaskStop protocol:
  - SendMessage: continue a completed worker with its accumulated context
  - TaskStop: stop a running worker and return its result
"""

from __future__ import annotations

import asyncio
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from ..context.agent_context import AgentContext, create_subagent_context
from ..engine.query_engine import QueryConfig, QueryEngine, TurnResult
from ..models.agent import AgentDefinition, TaskState
from ..models.message import Message, UserMessage
from ..providers.base import Provider
from ..router.tool_router import ToolRegistry


TASK_NOTIFICATION_TEMPLATE = (
    '<task-notification task-id="{task_id}" status="{status}" summary="{summary}">'
    "{result}"
    "</task-notification>"
)

_NOTIFICATION_RE = re.compile(
    r'<task-notification\s+task-id="([^"]+)"\s+status="([^"]+)"\s+summary="([^"]*)">'
    r"(.*?)</task-notification>",
    re.DOTALL,
)


def build_task_notification(
    task_id: str,
    status: str,
    summary: str,
    result: str,
) -> str:
    """Build a task notification XML string."""
    return TASK_NOTIFICATION_TEMPLATE.format(
        task_id=task_id,
        status=status,
        summary=summary,
        result=result,
    )


@dataclass
class TaskNotification:
    """Parsed task notification from a worker."""

    task_id: str
    status: str
    summary: str
    result: str


def parse_task_notification(text: str) -> Optional[TaskNotification]:
    """Extract the first task notification from a text block."""
    match = _NOTIFICATION_RE.search(text)
    if not match:
        return None
    return TaskNotification(
        task_id=match.group(1),
        status=match.group(2),
        summary=match.group(3),
        result=match.group(4).strip(),
    )


@dataclass
class WorkerTask:
    """A task assigned to a worker agent."""

    task_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    agent_type: str = "default"
    prompt: str = ""
    is_read_only: bool = True
    state: TaskState = field(
        default_factory=lambda: TaskState(agent_type="default")
    )
    result: Optional[TurnResult] = None

    def to_notification(self, result_text: str) -> str:
        """Format the task result as a task notification."""
        status = self.state.status
        summary = (result_text[:100] + "...") if len(result_text) > 100 else result_text
        return build_task_notification(self.task_id, status, summary, result_text)


class Coordinator:
    """Orchestrates multiple worker agents for a coordinator-mode session.

    The Coordinator does not run its own query loop — it provides helpers
    that the coordinator agent (running in a normal QueryEngine) can call
    via tool calls to spawn, continue, and stop workers.
    """

    def __init__(
        self,
        provider: Provider,
        tool_registry: ToolRegistry,
        parent_context: AgentContext,
        scratchpad_dir: Optional[str] = None,
    ) -> None:
        self._provider = provider
        self._tools = tool_registry
        self._parent = parent_context
        self._scratchpad_dir = scratchpad_dir
        self._tasks: dict[str, WorkerTask] = {}
        self._contexts: dict[str, AgentContext] = {}

    async def spawn_worker(
        self,
        agent_def: AgentDefinition,
        prompt: str,
        is_read_only: bool = True,
        context_override: Optional[dict[str, Any]] = None,
    ) -> WorkerTask:
        """Spawn a worker agent and return a WorkerTask handle.

        The worker runs to completion (or max_turns) and the result is
        stored in the WorkerTask.
        """
        task = WorkerTask(
            agent_type=agent_def.agent_type,
            prompt=prompt,
            is_read_only=is_read_only,
        )
        task.state = TaskState(
            task_id=task.task_id,
            agent_type=agent_def.agent_type,
        ).mark_running()
        self._tasks[task.task_id] = task

        # Fork context for the worker
        allowed = set(agent_def.tools) if agent_def.tools else set()
        worker_ctx = create_subagent_context(
            self._parent,
            agent_def=agent_def,
            allowed_tools=allowed,
        )
        self._contexts[task.task_id] = worker_ctx

        # Build config for the worker engine
        config = QueryConfig(
            max_turns=agent_def.max_turns,
            agent_id=task.task_id,
            system_prompt=agent_def.system_prompt,
            model=agent_def.model or self._parent.model,
            tool_names=list(agent_def.tools) if agent_def.tools else None,
        )
        engine = QueryEngine(self._provider, self._tools, config)

        # Seed messages with the coordinator's prompt
        seed: list[Message] = [UserMessage(content=prompt)]
        try:
            turn_result = await engine.run(seed)
            last_msg = turn_result.messages[-1]
            from ..models.message import AssistantMessage
            result_text = (
                last_msg.get_text()
                if isinstance(last_msg, AssistantMessage)
                else ""
            )
            task.state = task.state.mark_completed(
                result_text, usage=turn_result.total_usage
            )
            task.result = turn_result
        except Exception as exc:
            task.state = task.state.mark_failed(str(exc))

        return task

    async def spawn_workers_parallel(
        self,
        agent_def: AgentDefinition,
        prompts: list[str],
    ) -> list[WorkerTask]:
        """Spawn multiple read-only workers in parallel."""
        return list(
            await asyncio.gather(
                *[
                    self.spawn_worker(agent_def, prompt, is_read_only=True)
                    for prompt in prompts
                ]
            )
        )

    async def spawn_workers_sequential(
        self,
        agent_def: AgentDefinition,
        prompts: list[str],
    ) -> list[WorkerTask]:
        """Spawn multiple write workers sequentially."""
        results = []
        for prompt in prompts:
            task = await self.spawn_worker(agent_def, prompt, is_read_only=False)
            results.append(task)
        return results

    def get_task(self, task_id: str) -> Optional[WorkerTask]:
        """Return a worker task by ID."""
        return self._tasks.get(task_id)

    def all_tasks(self) -> list[WorkerTask]:
        """Return all worker tasks."""
        return list(self._tasks.values())

    def build_results_summary(self) -> str:
        """Collect all completed worker results as task notifications."""
        lines = []
        for task in self._tasks.values():
            if task.result:
                from ..models.message import AssistantMessage
                msgs = task.result.messages
                last = msgs[-1] if msgs else None
                text = (
                    last.get_text()
                    if last and isinstance(last, AssistantMessage)
                    else ""
                )
                lines.append(task.to_notification(text))
        return "\n".join(lines)
