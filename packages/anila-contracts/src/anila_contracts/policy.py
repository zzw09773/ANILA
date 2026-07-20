"""Policy-gate result contract for the Gate 5 Router/CSP boundary."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import StrictBool, field_validator, model_validator

from ._v2 import IdentifierField, StrictContractModel, TokenField, validate_unique_tokens
from .classification import ClassificationLevel

POLICY_GATE_SCHEMA_VERSION = "policy-gate/v1"

PolicyIdentifier = Annotated[str, IdentifierField]
PolicyToken = Annotated[str, TokenField]


class PolicyGateResult(StrictContractModel):
    """Immutable, explainable result of the Router-side policy gate.

    ``allowed`` is intentionally separate from ``approval_required``.  A
    request awaiting human approval is not allowed yet; treating those two
    states as equivalent is a fail-open bypass.  CSP must re-evaluate this
    result at the final dispatch boundary.
    """

    schema_version: Literal["policy-gate/v1"]
    allowed: StrictBool
    decision_id: PolicyIdentifier
    route_decision_id: PolicyIdentifier
    registry_snapshot_id: PolicyIdentifier
    target_agent_id: PolicyIdentifier | None = None
    effective_classification: ClassificationLevel
    required_scopes: tuple[PolicyToken, ...]
    obligations: tuple[PolicyToken, ...]
    approval_required: StrictBool
    reason_codes: tuple[PolicyToken, ...]

    @field_validator("required_scopes", "obligations", "reason_codes")
    @classmethod
    def _lists_are_unique(cls, value: tuple[str, ...], info) -> tuple[str, ...]:
        return validate_unique_tokens(value, field_name=info.field_name or "policy_values")

    @model_validator(mode="after")
    def _authority_chain_and_approval_are_coherent(self) -> PolicyGateResult:
        if self.allowed and self.approval_required:
            raise ValueError("allowed=true 不得同時 approval_required=true")
        if self.allowed and not self.reason_codes:
            raise ValueError("allowed=true 必須附至少一個 reason_code")
        return self


__all__ = ["POLICY_GATE_SCHEMA_VERSION", "PolicyGateResult"]
