"""Short-lived execution grant contract for governed downstream calls."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, StrictInt, field_validator, model_validator

from ._types import InvocationTargetKind
from ._v2 import IdentifierField, StrictContractModel, TokenField, validate_unique_tokens
from .agents import ModelBinding
from .classification import ClassificationLevel
from .contexts import AuthAssurance

EXECUTION_GRANT_SCHEMA_VERSION = "execution-grant/v1"
MAX_EXECUTION_GRANT_TTL = timedelta(minutes=5)

GrantIdentifier = Annotated[str, IdentifierField]
GrantToken = Annotated[str, TokenField]


class ExecutionTarget(StrictContractModel):
    """The one governed target a grant permits."""

    kind: InvocationTargetKind
    id: GrantIdentifier
    model_binding: ModelBinding | None = None

    @model_validator(mode="after")
    def _target_model_binding_is_explicit(self) -> ExecutionTarget:
        if self.kind in (InvocationTargetKind.MODEL, InvocationTargetKind.AGENT):
            if self.model_binding is None:
                raise ValueError("model/agent target 必須綁定 CSP model_binding")
        elif self.model_binding is not None and self.kind is InvocationTargetKind.RETRIEVAL:
            raise ValueError("retrieval target 不得攜帶 model_binding")
        return self


class ExecutionGrant(StrictContractModel):
    """A CSP-issued, immutable grant with a bounded lifetime.

    This contract carries authority; it is deliberately not a JWT signer or
    verifier.  Issuers must add their cryptographic envelope at the transport
    layer, while every consumer still validates this inner binding and TTL.
    """

    schema_version: Literal["execution-grant/v1"]
    grant_id: GrantIdentifier
    task_id: StrictInt = Field(gt=0)
    run_id: StrictInt = Field(gt=0)
    trace_id: GrantIdentifier
    invocation_id: GrantIdentifier
    source_snapshot_id: StrictInt = Field(gt=0)
    route_decision_id: GrantIdentifier
    policy_decision_id: GrantIdentifier
    registry_snapshot_id: GrantIdentifier
    classification: ClassificationLevel
    auth_assurance: AuthAssurance
    target: ExecutionTarget
    manifest_revision: GrantIdentifier | None = None
    allowed_capabilities: tuple[GrantToken, ...]
    allowed_scopes: tuple[GrantToken, ...]
    issued_at: AwareDatetime
    expires_at: AwareDatetime
    session_id: GrantIdentifier | None = None

    @field_validator("allowed_capabilities", "allowed_scopes")
    @classmethod
    def _grant_lists_are_unique(cls, value: tuple[str, ...], info) -> tuple[str, ...]:
        return validate_unique_tokens(value, field_name=info.field_name or "grant_values")

    @model_validator(mode="after")
    def _grant_time_and_binding_invariants(self) -> ExecutionGrant:
        if self.expires_at <= self.issued_at:
            raise ValueError("expires_at 必須晚於 issued_at")
        ttl = self.expires_at - self.issued_at
        if ttl > MAX_EXECUTION_GRANT_TTL:
            raise ValueError(
                f"ExecutionGrant TTL 不得超過 {int(MAX_EXECUTION_GRANT_TTL.total_seconds())} 秒"
            )
        if ttl < timedelta(seconds=1):
            raise ValueError("ExecutionGrant TTL 必須至少 1 秒")

        if self.target.kind is InvocationTargetKind.AGENT:
            if self.manifest_revision is None:
                raise ValueError("agent target 必須綁定 manifest_revision")
        elif self.manifest_revision is not None:
            raise ValueError("非 agent target 不得攜帶 manifest_revision")

        model_binding = self.target.model_binding
        if (
            model_binding is not None
            and model_binding.classification_ceiling is not None
            and model_binding.classification_ceiling < self.classification
        ):
            raise ValueError("grant classification 不得超過 model binding ceiling")
        return self

    @property
    def ttl_seconds(self) -> int:
        """Return the validated lifetime without re-parsing wire timestamps."""

        return int((self.expires_at - self.issued_at).total_seconds())

    @staticmethod
    def _require_aware(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("consumer time 必須帶時區")
        return value

    def is_active_at(self, at: datetime) -> bool:
        """Return whether the grant is active at an explicit aware instant.

        The interval is half-open: ``issued_at <= at < expires_at``.  No
        system clock is read, which keeps parsing and replay deterministic.
        """

        instant = self._require_aware(at)
        return self.issued_at <= instant < self.expires_at

    def assert_active_at(self, at: datetime) -> None:
        """Reject a grant before its issue time or at/after its expiry."""

        instant = self._require_aware(at)
        if instant < self.issued_at:
            raise ValueError("ExecutionGrant 尚未生效")
        if instant >= self.expires_at:
            raise ValueError("ExecutionGrant 已過期")


__all__ = [
    "EXECUTION_GRANT_SCHEMA_VERSION",
    "MAX_EXECUTION_GRANT_TTL",
    "ExecutionGrant",
    "ExecutionTarget",
]
