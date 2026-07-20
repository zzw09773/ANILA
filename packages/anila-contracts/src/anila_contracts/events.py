"""實際 Agent 執行步驟的 v1 wire envelope。"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from .classification import ClassificationLevel
from .errors import AgentError

STEP_EVENT_SCHEMA_VERSION = "step-event/v1"
STEP_EVENT_SSE_NAME = "anila.step"


class StepKind(str, Enum):
    SKILL = "skill"
    TOOL = "tool"
    COMMAND = "command"
    RETRIEVAL = "retrieval"
    AGENT = "agent"
    MODEL = "model"
    APPROVAL = "approval"
    ARTIFACT = "artifact"


class StepStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


_NonEmptyString = Annotated[str, Field(min_length=1)]


class StepEvent(BaseModel):
    """`step-event/v1` 的 canonical envelope。

    Todo 是預定計畫；本型別只表示已發生的執行事實。摘要欄位只允許
    safe/redacted projection，完整 payload 應留在受控 Full Trace。
    """

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_version: Literal["step-event/v1"] = "step-event/v1"
    event_id: str = Field(min_length=1)
    sequence: int = Field(ge=0)
    cursor: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    invocation_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    step_id: str = Field(min_length=1)
    parent_step_id: str | None = Field(default=None, min_length=1)
    depends_on: list[_NonEmptyString] = Field(default_factory=list)
    kind: StepKind
    status: StepStatus
    safe_input_summary: str | None = None
    safe_output_summary: str | None = None
    agent_id: str | None = Field(default=None, min_length=1)
    tool_name: str | None = Field(default=None, min_length=1)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    latency_ms: int | None = Field(default=None, ge=0)
    retry_count: int = Field(default=0, ge=0)
    error: AgentError | None = None
    classification: ClassificationLevel


__all__ = [
    "STEP_EVENT_SCHEMA_VERSION",
    "STEP_EVENT_SSE_NAME",
    "StepEvent",
    "StepKind",
    "StepStatus",
]
