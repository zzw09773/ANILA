"""CSP internal transport schemas for ExecutionGrant minting.

The contract in :mod:`anila_contracts.grants` is intentionally only the
inner, transport-neutral grant.  This module describes the CSP-only request
which presents the Router's signed-decision evidence and the response which
returns CSP's signed envelope.  Neither model carries a service credential;
the caller identity is resolved from the internal service-token dependency
and the dedicated caller-user header.
"""

from __future__ import annotations

from app.schemas.base import ApiResponseModel

from typing import Literal

from pydantic import ConfigDict, Field, StrictInt, StrictStr, BaseModel

from anila_contracts import ExecutionGrant, PolicyGateResult, RouteDecision
from anila_contracts.agents import ModelBinding
from anila_contracts.classification import ClassificationLevel
from anila_contracts.contexts import AuthAssurance


class ExecutionGrantMintRequest(BaseModel):
    """Evidence presented by the named Router to CSP's mint gate.

    Every authority-bearing object is parsed as its canonical Gate 5
    contract.  The service still recomputes the current Task, AuthSession and
    caller-scoped Agent registry before signing; these fields are evidence,
    not authority by themselves.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_default=True,
    )

    task_id: StrictInt = Field(gt=0)
    run_id: StrictInt = Field(gt=0)
    source_snapshot_id: StrictInt = Field(gt=0)
    trace_id: StrictStr = Field(min_length=1, max_length=255)
    invocation_id: StrictStr = Field(min_length=1, max_length=255)
    session_id: StrictStr = Field(min_length=1, max_length=255)

    registry_snapshot_id: StrictStr = Field(min_length=1, max_length=255)
    registry_snapshot_revision: StrictStr = Field(min_length=1, max_length=255)
    registry_snapshot_hash: StrictStr = Field(min_length=1, max_length=255)
    target_agent_id: StrictStr = Field(min_length=1, max_length=255)
    manifest_revision: StrictStr = Field(min_length=1, max_length=255)
    manifest_sha256: StrictStr = Field(min_length=1, max_length=255)

    classification: ClassificationLevel
    auth_assurance: AuthAssurance
    model_binding: ModelBinding
    allowed_capabilities: tuple[StrictStr, ...] = ()
    allowed_scopes: tuple[StrictStr, ...] = ()

    route_decision: RouteDecision
    policy_result: PolicyGateResult

    # CSP deliberately imposes a shorter transport lifetime than the generic
    # five-minute ExecutionGrant contract.
    ttl_seconds: StrictInt = Field(default=60, gt=0, le=60)


class ExecutionGrantMintResponse(ApiResponseModel):
    """CSP-issued signed transport envelope plus its parsed inner grant."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["execution-grant-envelope/v1"]
    token: StrictStr = Field(min_length=1)
    token_type: Literal["Bearer"] = "Bearer"
    grant: ExecutionGrant


__all__ = ["ExecutionGrantMintRequest", "ExecutionGrantMintResponse"]
