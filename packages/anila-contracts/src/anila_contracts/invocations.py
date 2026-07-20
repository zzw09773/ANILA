"""Governed invocation command contract."""

from __future__ import annotations

import math
from typing import Literal

from pydantic import Field, JsonValue, StrictInt, field_validator, model_validator

from ._base import (
    ContractModel,
    Identifier,
    LongIdentifier,
    PositiveInt,
    ensure_unique,
    freeze_json,
)
from ._types import InvocationTargetKind
from .classification import ClassificationLevel
from .contexts import TaskContext, TraceContext
from .summaries import SafeSummary

INVOCATION_COMMAND_SCHEMA_VERSION = "invocation-command/v1"


def _reject_non_finite_json(value: JsonValue, *, path: str = "input") -> None:
    """Keep canonical JSON from silently rewriting NaN/Infinity to null."""

    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{path} 不允許 NaN 或 Infinity")
    if isinstance(value, list):
        for index, item in enumerate(value):
            _reject_non_finite_json(item, path=f"{path}[{index}]")
    elif isinstance(value, dict):
        for key, item in value.items():
            _reject_non_finite_json(item, path=f"{path}.{key}")


class InvocationCommand(ContractModel):
    """One immutable, idempotent command sent to a governed runtime target."""

    schema_version: Literal["invocation-command/v1"]
    invocation_id: Identifier
    request_id: Identifier
    task_context: TaskContext
    trace_context: TraceContext
    target_kind: InvocationTargetKind
    target_id: LongIdentifier
    source_snapshot_id: PositiveInt | None = None
    input: dict[str, JsonValue] = Field(min_length=1)
    safe_input_summary: SafeSummary | None = None
    classification: ClassificationLevel
    idempotency_key: LongIdentifier
    timeout_ms: StrictInt = Field(ge=1, le=3_600_000)
    max_steps: StrictInt = Field(ge=1, le=1000)
    allowed_tools: tuple[LongIdentifier, ...]
    event_protocol: Literal["step-event/v1"]
    registry_snapshot: Identifier
    manifest_revision: Identifier

    @field_validator("allowed_tools")
    @classmethod
    def _allowed_tools_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        ensure_unique(value, field_name="allowed_tools")
        return value

    @field_validator("input")
    @classmethod
    def _input_keys_are_nonempty(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        if any(not key.strip() for key in value):
            raise ValueError("input key 不得為空白")
        _reject_non_finite_json(value)
        return freeze_json(value)

    @model_validator(mode="after")
    def _authority_contexts_match(self) -> InvocationCommand:
        task = self.task_context
        trace = self.trace_context
        mismatches: list[str] = []
        if self.invocation_id != trace.invocation_id:
            mismatches.append("invocation_id")
        if task.task_id != trace.task_id:
            mismatches.append("task_id")
        if task.run_id != trace.run_id:
            mismatches.append("run_id")
        if task.session_id != trace.session_id:
            mismatches.append("session_id")
        if self.source_snapshot_id != task.source_snapshot_id:
            mismatches.append("source_snapshot_id")
        if not (
            self.classification
            is task.classification
            is trace.classification
        ):
            mismatches.append("classification")
        if self.safe_input_summary is not None:
            if self.safe_input_summary.classification is not self.classification:
                mismatches.append("safe_input_summary.classification")
            if (
                self.safe_input_summary.source_content_hash is not None
                and self.source_snapshot_id is None
            ):
                mismatches.append("safe_input_summary.source_content_hash")
        if mismatches:
            raise ValueError(
                "InvocationCommand 權威 context 不一致:" + ", ".join(mismatches)
            )
        return self


__all__ = ["INVOCATION_COMMAND_SCHEMA_VERSION", "InvocationCommand"]
