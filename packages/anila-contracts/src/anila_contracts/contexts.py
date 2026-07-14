"""Task and trace correlation contexts for governed Gate 2 calls."""

from __future__ import annotations

from typing import Literal

from pydantic import AwareDatetime, Field, StrictBool, field_validator, model_validator

from ._base import ContractModel, Identifier, NonEmptyString, PositiveInt, ensure_unique
from ._types import SourceScope
from .classification import ClassificationLevel

TASK_CONTEXT_SCHEMA_VERSION = "task-context/v1"
TRACE_CONTEXT_SCHEMA_VERSION = "trace-context/v1"


class AuthAssurance(ContractModel):
    """Authentication assurance captured at the trusted CSP boundary."""

    sid: Identifier
    amr: tuple[NonEmptyString, ...] = Field(min_length=1)
    acr: NonEmptyString
    auth_time: AwareDatetime
    break_glass: StrictBool

    @field_validator("amr")
    @classmethod
    def _amr_is_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        ensure_unique(value, field_name="amr")
        return value


class TaskContext(ContractModel):
    """Immutable task authority carried into a governed invocation.

    Clearance is deliberately absent.  A caller may carry authentication
    assurance, but effective clearance remains server-derived policy state.
    """

    schema_version: Literal["task-context/v1"]
    task_id: PositiveInt
    run_id: PositiveInt
    requester_user_id: PositiveInt
    session_id: Identifier | None = None
    conversation_id: PositiveInt | None = None
    source_scope: SourceScope
    source_snapshot_id: PositiveInt | None = None
    collection_ids: tuple[PositiveInt, ...] = ()
    classification: ClassificationLevel
    auth_assurance: AuthAssurance | None = None

    @field_validator("collection_ids")
    @classmethod
    def _collection_ids_are_unique(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        ensure_unique(value, field_name="collection_ids")
        return value

    @model_validator(mode="after")
    def _source_declaration_is_coherent(self) -> TaskContext:
        if self.source_scope is SourceScope.NONE:
            if self.collection_ids:
                raise ValueError(
                    "source_scope=none 必須明確表示無來源，不得夾帶 collection"
                )
            # ``source_snapshot_id`` may point at the persisted origin=none
            # declaration that CSP already creates for every formal Task.
            return self

        if self.source_snapshot_id is None:
            raise ValueError("正式來源 scope 必須綁定 source_snapshot_id")
        if self.source_scope is SourceScope.REGISTERED_SERVICE and self.collection_ids:
            raise ValueError("registered_service scope 不得夾帶 collection_ids")
        return self


class TraceContext(ContractModel):
    """Correlation identifiers and classification for one invocation trace."""

    schema_version: Literal["trace-context/v1"]
    trace_id: Identifier
    task_id: PositiveInt
    run_id: PositiveInt
    invocation_id: Identifier
    session_id: Identifier | None = None
    parent_span_id: Identifier | None = None
    classification: ClassificationLevel

    @model_validator(mode="after")
    def _parent_span_cannot_be_trace_id(self) -> TraceContext:
        if self.parent_span_id == self.trace_id:
            raise ValueError("parent_span_id 不得等於 trace_id")
        return self


__all__ = [
    "TASK_CONTEXT_SCHEMA_VERSION",
    "TRACE_CONTEXT_SCHEMA_VERSION",
    "AuthAssurance",
    "TaskContext",
    "TraceContext",
]
