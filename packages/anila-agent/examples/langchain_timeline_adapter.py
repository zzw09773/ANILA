"""Official LangChain callback adapter for ANILA's named StepEvent SSE wire.

This file intentionally imports no LangChain package.  Register an instance in
``callbacks=[AnilaTimelineCallback(...)]``; LangChain invokes these duck-typed
methods.  The adapter projects only names/counts and never raw prompts,
arguments, retrieved documents, chain output, or model reasoning.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

from anila_contracts import Classification
from anila_contracts.events import StepKind, StepStatus

from anila_agent.observability.timeline import TimelineEmitter


class AnilaTimelineCallback:
    def __init__(
        self,
        *,
        task_id: str,
        trace_id: str,
        agent_id: str,
        session_id: str,
        run_id: str,
        classification: Classification,
        send_sse: Callable[[str], None],
    ) -> None:
        self.emitter = TimelineEmitter(
            task_id=task_id,
            trace_id=trace_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=run_id,
            classification=classification,
        )
        self.send_sse = send_sse
        self._steps: dict[str, tuple[str, StepKind, str | None]] = {}

    def _flush(self) -> None:
        for frame in self.emitter.drain():
            self.send_sse(frame)

    @staticmethod
    def _run_id(run_id: Any) -> str:
        return str(run_id or uuid.uuid4())

    def _start(self, run_id: Any, kind: StepKind, name: str, tool_name: str | None = None) -> None:
        key = self._run_id(run_id)
        self._steps[key] = (f"{kind.value}:{key}", kind, tool_name)
        self.emitter.emit(
            step_id=self._steps[key][0],
            parent_step_id=f"agent:{self.emitter.agent_id}",
            kind=kind,
            status=StepStatus.RUNNING,
            tool_name=tool_name,
            safe_input_summary=f"開始執行 {name}",
        )
        self._flush()

    def _end(self, run_id: Any, summary: str) -> None:
        key = self._run_id(run_id)
        step_id, kind, tool_name = self._steps.pop(
            key, (f"tool:{key}", StepKind.TOOL, None)
        )
        self.emitter.emit(
            step_id=step_id,
            parent_step_id=f"agent:{self.emitter.agent_id}",
            kind=kind,
            status=StepStatus.COMPLETED,
            tool_name=tool_name,
            safe_output_summary=summary,
        )
        self._flush()

    def on_chain_start(self, serialized: dict, inputs: Any, *, run_id=None, **kwargs) -> None:
        self._start(run_id, StepKind.SKILL, str(serialized.get("name") or "chain"))

    def on_chain_end(self, outputs: Any, *, run_id=None, **kwargs) -> None:
        self._end(run_id, "技能流程執行完成")

    def on_tool_start(self, serialized: dict, input_str: str, *, run_id=None, **kwargs) -> None:
        name = str(serialized.get("name") or "tool")
        self._start(run_id, StepKind.TOOL, name, tool_name=name)

    def on_tool_end(self, output: Any, *, run_id=None, **kwargs) -> None:
        self._end(run_id, "工具執行完成")

    def on_retriever_start(self, serialized: dict, query: str, *, run_id=None, **kwargs) -> None:
        self._start(run_id, StepKind.RETRIEVAL, str(serialized.get("name") or "retriever"))

    def on_retriever_end(self, documents: list[Any], *, run_id=None, **kwargs) -> None:
        self._end(run_id, f"檢索完成，共 {len(documents)} 筆")


__all__ = ["AnilaTimelineCallback"]
