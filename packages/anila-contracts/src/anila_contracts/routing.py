"""Structured routing decision contract for the Gate 5 Router runtime."""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from ._v2 import (
    MAX_QUERY_LENGTH,
    IdentifierField,
    JsonObject,
    StrictContractModel,
    StrictInteger,
    StrictNumber,
    TokenField,
    ensure_text,
    validate_json_object,
    validate_unique_tokens,
)

ROUTE_DECISION_SCHEMA_VERSION = "route-decision/v1"

RouteToken = Annotated[str, TokenField]
RouteIdentifier = Annotated[str, IdentifierField]


class RouteType(str, Enum):
    """Closed set of route outcomes.

    ``multi_agent_plan`` is represented for forward-compatible wire
    negotiation only.  R1 does not execute plans; the runtime gate remains
    single-agent/direct-answer by policy.
    """

    DIRECT_ANSWER = "direct_answer"
    SINGLE_AGENT = "single_agent"
    MULTI_AGENT_PLAN = "multi_agent_plan"
    CLARIFY = "clarify"
    DENY = "deny"


class RouteFallback(str, Enum):
    """Safe fallback outcomes; a fallback may never create a plan."""

    DIRECT_ANSWER = "direct_answer"
    CLARIFY = "clarify"
    DENY = "deny"


class RouteConstraints(StrictContractModel):
    """Server-controlled ceilings carried with a route decision."""

    max_steps: StrictInteger = Field(ge=1, le=1000)
    timeout_ms: StrictInteger = Field(ge=1, le=3_600_000)


class RouteDecision(StrictContractModel):
    """Canonical structured decision replacing the legacy ``DISPATCH:`` text.

    The model validates the relationship between selected and candidate
    agents, but it does not call an agent or execute a multi-agent plan.
    Policy enforcement belongs to ``PolicyGateResult`` and CSP's final gate.
    """

    schema_version: Literal["route-decision/v1"]
    decision_id: RouteIdentifier
    route_type: RouteType
    registry_snapshot_id: RouteIdentifier
    required_capabilities: tuple[RouteToken, ...]
    candidate_agent_ids: tuple[RouteIdentifier, ...]
    selected_agent_id: RouteIdentifier | None = None
    reason_codes: tuple[RouteToken, ...]
    confidence: StrictNumber = Field(ge=0.0, le=1.0)
    rewritten_query: str | None = Field(default=None, max_length=MAX_QUERY_LENGTH)
    constraints: RouteConstraints
    execution_plan: JsonObject | None = None
    fallback: RouteFallback | None = None

    @field_validator("required_capabilities", "reason_codes")
    @classmethod
    def _tokens_are_unique(
        cls, value: tuple[str, ...], info
    ) -> tuple[str, ...]:
        return validate_unique_tokens(value, field_name=info.field_name or "tokens")

    @field_validator("candidate_agent_ids")
    @classmethod
    def _candidates_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return validate_unique_tokens(value, field_name="candidate_agent_ids")

    @field_validator("rewritten_query")
    @classmethod
    def _rewritten_query_is_safe(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return ensure_text(value, field_name="rewritten_query", max_length=MAX_QUERY_LENGTH)

    @field_validator("execution_plan")
    @classmethod
    def _execution_plan_is_frozen(cls, value: JsonObject | None):
        return None if value is None else validate_json_object(value, field_name="execution_plan")

    @model_validator(mode="after")
    def _route_identity_is_coherent(self) -> RouteDecision:
        selected = self.selected_agent_id
        if self.route_type is RouteType.SINGLE_AGENT:
            if selected is None:
                raise ValueError("route_type=single_agent 必須指定 selected_agent_id")
            if not self.candidate_agent_ids:
                raise ValueError("route_type=single_agent 必須有 candidate_agent_ids")
            if selected not in self.candidate_agent_ids:
                raise ValueError("selected_agent_id 必須屬於 candidate_agent_ids")
            if self.rewritten_query is None:
                raise ValueError("route_type=single_agent 必須指定 rewritten_query")
        elif selected is not None:
            # In particular, a denied/clarification decision can never smuggle
            # an agent identifier downstream through an otherwise valid JSON.
            raise ValueError("route_type 非 single_agent 不得指定 selected_agent_id")

        if self.route_type is RouteType.MULTI_AGENT_PLAN:
            if not self.candidate_agent_ids:
                raise ValueError("route_type=multi_agent_plan 必須有候選 Agent")
            # The field is intentionally opaque in R1: later gates may define
            # a plan schema, but no R1 consumer is allowed to execute it.
        elif self.execution_plan is not None:
            raise ValueError("execution_plan 只有 multi_agent_plan 可攜帶")

        return self


__all__ = [
    "ROUTE_DECISION_SCHEMA_VERSION",
    "RouteConstraints",
    "RouteDecision",
    "RouteFallback",
    "RouteType",
]
