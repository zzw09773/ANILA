"""Read-only Gate 4 timeline projection for the official agent runtime."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from agents import RunHooks
from anila_contracts import Classification, StepEvent
from anila_contracts.events import STEP_EVENT_SSE_NAME, StepKind, StepStatus

from anila_agent.retrieval.base import Retriever
from anila_agent.retrieval.schemas import Document

TIMELINE_EVENT_NAME = STEP_EVENT_SSE_NAME


class TimelineEmitter:
    """Create safe StepEvents and queue named SSE frames for one invocation."""

    def __init__(
        self,
        *,
        task_id: str,
        trace_id: str,
        agent_id: str,
        session_id: str,
        run_id: str,
        classification: Classification,
        invocation_id: str | None = None,
    ) -> None:
        self.task_id = task_id
        self.trace_id = trace_id
        self.agent_id = agent_id
        self.session_id = session_id
        self.run_id = run_id
        self.classification = classification
        self.invocation_id = invocation_id or uuid.uuid4().hex
        self._sequence = 0
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._events: list[StepEvent] = []
        self._started: dict[str, float] = {}
        self._terminal_emitted = False

    def _next(self) -> int:
        self._sequence += 1
        return self._sequence

    def emit(
        self,
        *,
        step_id: str,
        kind: StepKind,
        status: StepStatus,
        parent_step_id: str | None = None,
        tool_name: str | None = None,
        safe_input_summary: str | None = None,
        safe_output_summary: str | None = None,
        terminal: bool = False,
    ) -> None:
        if terminal:
            if self._terminal_emitted:
                return
            self._terminal_emitted = True
        now = datetime.now(timezone.utc)
        if status == StepStatus.RUNNING:
            self._started.setdefault(step_id, time.monotonic())
        started = self._started.get(step_id)
        latency_ms = None
        completed_at = None
        if status in {StepStatus.COMPLETED, StepStatus.FAILED, StepStatus.CANCELLED}:
            completed_at = now
            if started is not None:
                latency_ms = max(0, int((time.monotonic() - started) * 1000))
        sequence = self._next()
        event = StepEvent(
            event_id=uuid.uuid4().hex,
            sequence=sequence,
            cursor=str(sequence),
            trace_id=self.trace_id,
            task_id=self.task_id,
            session_id=self.session_id,
            invocation_id=self.invocation_id,
            run_id=self.run_id,
            step_id=step_id,
            parent_step_id=parent_step_id,
            kind=kind,
            status=status,
            safe_input_summary=safe_input_summary,
            safe_output_summary=safe_output_summary,
            agent_id=self.agent_id,
            tool_name=tool_name,
            started_at=now if status == StepStatus.RUNNING else None,
            completed_at=completed_at,
            latency_ms=latency_ms,
            classification=self.classification,
        )
        self._events.append(event)
        self._queue.put_nowait(
            f"event: {TIMELINE_EVENT_NAME}\n"
            f"data: {event.model_dump_json()}\n\n"
        )

    def drain(self) -> list[str]:
        frames: list[str] = []
        while not self._queue.empty():
            frames.append(self._queue.get_nowait())
        return frames

    def set_sequence_floor(self, sequence: int) -> None:
        """Continue a durable task timeline without reusing sequence cursors.

        A resume starts in a fresh process-local emitter, but its StepEvents
        are appended to the same durable Task record.  Advancing this floor
        keeps SSE cursor order monotonic across a pause/restart boundary.
        """

        if sequence < 0:
            raise ValueError("timeline sequence floor must be non-negative")
        if self._events:
            raise RuntimeError("cannot change timeline sequence after emitting events")
        self._sequence = max(self._sequence, sequence)

    @property
    def events(self) -> tuple[StepEvent, ...]:
        """Read-only canonical event history for durable task persistence."""

        return tuple(self._events)

    def cancel(self) -> None:
        self.emit(
            step_id=f"agent:{self.agent_id}",
            kind=StepKind.AGENT,
            status=StepStatus.CANCELLED,
            safe_output_summary="執行已取消",
            terminal=True,
        )

    def fail(self) -> None:
        self.emit(
            step_id=f"agent:{self.agent_id}",
            kind=StepKind.AGENT,
            status=StepStatus.FAILED,
            safe_output_summary="Agent 執行失敗",
            terminal=True,
        )


class TimelineRunHooks(RunHooks):
    """Project SDK agent/tool hooks without exposing raw arguments or output."""

    def __init__(self, emitter: TimelineEmitter, *, inner: RunHooks | None = None) -> None:
        self.emitter = emitter
        self.inner = inner
        self._tools: dict[str, list[str]] = defaultdict(list)

    async def _fan(self, method: str, *args: Any) -> None:
        fn = getattr(self.inner, method, None) if self.inner is not None else None
        if fn is not None:
            await fn(*args)

    async def on_agent_start(self, context: Any, agent: Any) -> None:
        self.emitter.emit(
            step_id=f"agent:{self.emitter.agent_id}",
            kind=StepKind.AGENT,
            status=StepStatus.RUNNING,
            safe_input_summary="開始唯讀 Agent 執行",
        )
        await self._fan("on_agent_start", context, agent)

    async def on_agent_end(self, context: Any, agent: Any, output: Any) -> None:
        self.emitter.emit(
            step_id=f"agent:{self.emitter.agent_id}",
            kind=StepKind.AGENT,
            status=StepStatus.COMPLETED,
            safe_output_summary="Agent 執行完成",
            # The service/Task runner owns the durable terminal transition.
            # Keeping this SDK callback non-terminal leaves a real cancellation
            # race able to emit exactly one authoritative cancelled terminal.
            terminal=False,
        )
        await self._fan("on_agent_end", context, agent, output)

    async def on_tool_start(self, context: Any, agent: Any, tool: Any) -> None:
        name = str(getattr(tool, "name", "tool"))
        step_id = f"tool:{uuid.uuid4().hex}"
        self._tools[name].append(step_id)
        kind = StepKind.SKILL if name in {"load_skill", "use_skill"} else StepKind.TOOL
        self.emitter.emit(
            step_id=step_id,
            parent_step_id=f"agent:{self.emitter.agent_id}",
            kind=kind,
            status=StepStatus.RUNNING,
            tool_name=name,
            safe_input_summary=f"開始執行 {name}",
        )
        await self._fan("on_tool_start", context, agent, tool)

    async def on_tool_end(self, context: Any, agent: Any, tool: Any, result: Any) -> None:
        name = str(getattr(tool, "name", "tool"))
        stack = self._tools.get(name)
        step_id = stack.pop() if stack else f"tool:{uuid.uuid4().hex}"
        count = len(result) if isinstance(result, (list, tuple, set, dict)) else None
        summary = f"{name} 執行完成" + (f"，回傳 {count} 項" if count is not None else "")
        kind = StepKind.SKILL if name in {"load_skill", "use_skill"} else StepKind.TOOL
        self.emitter.emit(
            step_id=step_id,
            parent_step_id=f"agent:{self.emitter.agent_id}",
            kind=kind,
            status=StepStatus.COMPLETED,
            tool_name=name,
            safe_output_summary=summary,
        )
        await self._fan("on_tool_end", context, agent, tool, result)


class TimelineRetriever:
    """Retriever decorator that emits count-only, content-free events."""

    def __init__(self, inner: Retriever, emitter: TimelineEmitter) -> None:
        self.inner = inner
        self.emitter = emitter

    @property
    def name(self) -> str:
        return self.inner.name

    @property
    def metadata(self) -> dict[str, Any]:
        return self.inner.metadata

    async def search(self, query: str, k: int = 5) -> list[Document]:
        step_id = f"retrieval:{uuid.uuid4().hex}"
        self.emitter.emit(
            step_id=step_id,
            parent_step_id=f"agent:{self.emitter.agent_id}",
            kind=StepKind.RETRIEVAL,
            status=StepStatus.RUNNING,
            safe_input_summary=f"開始 {self.name} 檢索（上限 {k} 筆）",
        )
        hits = await self.inner.search(query, k=k)
        self.emitter.emit(
            step_id=step_id,
            parent_step_id=f"agent:{self.emitter.agent_id}",
            kind=StepKind.RETRIEVAL,
            status=StepStatus.COMPLETED,
            safe_output_summary=f"{self.name} 檢索完成，共 {len(hits)} 筆",
        )
        return hits

    async def fetch(self, doc_id: str) -> Document | None:
        return await self.inner.fetch(doc_id)


__all__ = ["TimelineEmitter", "TimelineRetriever", "TimelineRunHooks"]
