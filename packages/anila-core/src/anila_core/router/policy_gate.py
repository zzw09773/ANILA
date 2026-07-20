"""Pure Router-side policy gate for one structured route decision."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from anila_contracts import PolicyGateResult, RouteDecision
from anila_contracts.agents import ModelBinding
from anila_contracts.classification import ClassificationLevel
from anila_contracts.contexts import AuthAssurance
from anila_contracts.routing import RouteType

from .candidate_filter import RegistryEntry, RegistrySnapshot
from .request_context import RequestContext


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


@dataclass(frozen=True)
class ExecutionGrantInput:
    """Unsigned input for CSP's final ExecutionGrant gate.

    The Router exposes the binding facts it checked, but deliberately does not
    sign or mint an :class:`anila_contracts.ExecutionGrant`.
    """

    identity: str
    owner_id: str
    session_id: str
    task_id: int
    run_id: int
    source_snapshot_id: int
    trace_id: str
    invocation_id: str
    route_decision_id: str
    policy_decision_id: str
    registry_snapshot_id: str
    target_agent_id: str
    classification: ClassificationLevel
    auth_assurance: AuthAssurance
    allowed_capabilities: tuple[str, ...]
    allowed_scopes: tuple[str, ...]
    manifest_revision: str
    model_binding: ModelBinding

    @property
    def target(self) -> str:
        return self.target_agent_id


class PolicyGate:
    """Fail-closed policy checks over a validated route and CSP snapshot."""

    def __init__(self, *, require_csp_model_binding: bool = True) -> None:
        self.require_csp_model_binding = require_csp_model_binding

    @staticmethod
    def _result(
        *,
        allowed: bool,
        decision: RouteDecision,
        context: RequestContext,
        snapshot_id: str,
        target_agent_id: str | None,
        required_scopes: tuple[str, ...] = (),
        obligations: tuple[str, ...] = (),
        approval_required: bool = False,
        reason_codes: tuple[str, ...],
    ) -> PolicyGateResult:
        return PolicyGateResult(
            schema_version="policy-gate/v1",
            allowed=allowed,
            decision_id=f"pg-{decision.decision_id}",
            route_decision_id=decision.decision_id,
            registry_snapshot_id=snapshot_id,
            target_agent_id=target_agent_id,
            effective_classification=context.classification,
            required_scopes=required_scopes,
            obligations=obligations,
            approval_required=approval_required,
            reason_codes=_unique(reason_codes),
        )

    def evaluate(
        self,
        decision: RouteDecision,
        context: RequestContext,
        snapshot: RegistrySnapshot | None,
        entry: RegistryEntry | None = None,
        *,
        now: datetime | None = None,
    ) -> PolicyGateResult:
        """Evaluate a route without network calls or mutable side effects."""

        snapshot_id = decision.registry_snapshot_id

        if decision.route_type is RouteType.DIRECT_ANSWER:
            # R3 has no model-governance input for direct inference yet.  Keep
            # the route side-effect free but do not report policy allow.
            return self._result(
                allowed=False,
                decision=decision,
                context=context,
                snapshot_id=snapshot_id,
                target_agent_id=None,
                reason_codes=("DIRECT_MODEL_POLICY_NOT_EVALUATED",),
            )

        if decision.route_type is not RouteType.SINGLE_AGENT:
            return self._result(
                allowed=False,
                decision=decision,
                context=context,
                snapshot_id=snapshot_id,
                target_agent_id=None,
                approval_required=False,
                reason_codes=(
                    "ROUTE_NOT_DISPATCHABLE",
                    "MULTI_AGENT_UNSUPPORTED"
                    if decision.route_type is RouteType.MULTI_AGENT_PLAN
                    else decision.route_type.value.upper(),
                ),
            )

        if snapshot is None:
            return self._result(
                allowed=False,
                decision=decision,
                context=context,
                snapshot_id=snapshot_id,
                target_agent_id=decision.selected_agent_id,
                reason_codes=("SNAPSHOT_MISSING",),
            )
        if not snapshot.is_fresh(now=now):
            return self._result(
                allowed=False,
                decision=decision,
                context=context,
                snapshot_id=snapshot_id,
                target_agent_id=decision.selected_agent_id,
                reason_codes=("SNAPSHOT_STALE",),
            )
        if snapshot.snapshot_id != decision.registry_snapshot_id:
            return self._result(
                allowed=False,
                decision=decision,
                context=context,
                snapshot_id=snapshot.snapshot_id,
                target_agent_id=decision.selected_agent_id,
                reason_codes=("SNAPSHOT_MISMATCH",),
            )
        if entry is None or decision.selected_agent_id != entry.agent_id:
            return self._result(
                allowed=False,
                decision=decision,
                context=context,
                snapshot_id=snapshot.snapshot_id,
                target_agent_id=decision.selected_agent_id,
                reason_codes=("UNKNOWN_AGENT",),
            )
        snapshot_entry = snapshot.entry_by_id.get(entry.agent_id)
        if snapshot_entry is None or snapshot_entry != entry:
            return self._result(
                allowed=False,
                decision=decision,
                context=context,
                snapshot_id=snapshot.snapshot_id,
                target_agent_id=entry.agent_id,
                reason_codes=("ENTRY_NOT_IN_SNAPSHOT",),
            )
        if entry.snapshot_id != snapshot.snapshot_id:
            return self._result(
                allowed=False,
                decision=decision,
                context=context,
                snapshot_id=snapshot.snapshot_id,
                target_agent_id=entry.agent_id,
                reason_codes=("ENTRY_SNAPSHOT_MISMATCH",),
            )
        if entry.manifest is None:
            return self._result(
                allowed=False,
                decision=decision,
                context=context,
                snapshot_id=snapshot.snapshot_id,
                target_agent_id=entry.agent_id,
                reason_codes=("MANIFEST_MISSING",),
            )
        if not entry.manifest_valid:
            return self._result(
                allowed=False,
                decision=decision,
                context=context,
                snapshot_id=snapshot.snapshot_id,
                target_agent_id=entry.agent_id,
                reason_codes=("MANIFEST_INVALID",),
            )
        if not entry.ready_for_dispatch:
            return self._result(
                allowed=False,
                decision=decision,
                context=context,
                snapshot_id=snapshot.snapshot_id,
                target_agent_id=entry.agent_id,
                reason_codes=("AGENT_NOT_READY",),
            )
        if not entry.approved:
            return self._result(
                allowed=False,
                decision=decision,
                context=context,
                snapshot_id=snapshot.snapshot_id,
                target_agent_id=entry.agent_id,
                reason_codes=("AGENT_NOT_APPROVED",),
            )
        if not entry.health_ready:
            return self._result(
                allowed=False,
                decision=decision,
                context=context,
                snapshot_id=snapshot.snapshot_id,
                target_agent_id=entry.agent_id,
                reason_codes=("AGENT_UNHEALTHY",),
            )
        if not entry.trace_test_passed:
            return self._result(
                allowed=False,
                decision=decision,
                context=context,
                snapshot_id=snapshot.snapshot_id,
                target_agent_id=entry.agent_id,
                reason_codes=("TRACE_TEST_NOT_READY",),
            )
        if self.require_csp_model_binding and not entry.has_csp_model_binding:
            return self._result(
                allowed=False,
                decision=decision,
                context=context,
                snapshot_id=snapshot.snapshot_id,
                target_agent_id=entry.agent_id,
                reason_codes=("MODEL_BINDING_MISSING",),
            )
        if entry.manifest_revision is None or not entry.manifest_revision.strip():
            return self._result(
                allowed=False,
                decision=decision,
                context=context,
                snapshot_id=snapshot.snapshot_id,
                target_agent_id=entry.agent_id,
                reason_codes=("MANIFEST_REVISION_MISSING",),
            )
        if entry.classification_ceiling is None:
            return self._result(
                allowed=False,
                decision=decision,
                context=context,
                snapshot_id=snapshot.snapshot_id,
                target_agent_id=entry.agent_id,
                reason_codes=("CLASSIFICATION_CEILING_MISSING",),
            )
        if context.task_type not in set(entry.effective_task_types):
            return self._result(
                allowed=False,
                decision=decision,
                context=context,
                snapshot_id=snapshot.snapshot_id,
                target_agent_id=entry.agent_id,
                reason_codes=("TASK_TYPE_UNSUPPORTED",),
            )

        missing_scopes = tuple(
            scope for scope in entry.required_scopes if scope not in context.scopes
        )
        if missing_scopes:
            return self._result(
                allowed=False,
                decision=decision,
                context=context,
                snapshot_id=snapshot.snapshot_id,
                target_agent_id=entry.agent_id,
                required_scopes=entry.required_scopes,
                reason_codes=("INSUFFICIENT_SCOPE",),
            )
        if context.classification > entry.effective_classification_ceiling:
            return self._result(
                allowed=False,
                decision=decision,
                context=context,
                snapshot_id=snapshot.snapshot_id,
                target_agent_id=entry.agent_id,
                required_scopes=entry.required_scopes,
                reason_codes=("CLASSIFICATION_EXCEEDS_CEILING",),
            )
        if not set(decision.required_capabilities).issubset(set(entry.capabilities)):
            return self._result(
                allowed=False,
                decision=decision,
                context=context,
                snapshot_id=snapshot.snapshot_id,
                target_agent_id=entry.agent_id,
                required_scopes=entry.required_scopes,
                reason_codes=("CAPABILITY_MISMATCH",),
            )
        if decision.constraints.max_steps > context.max_steps:
            return self._result(
                allowed=False,
                decision=decision,
                context=context,
                snapshot_id=snapshot.snapshot_id,
                target_agent_id=entry.agent_id,
                required_scopes=entry.required_scopes,
                reason_codes=("MAX_STEPS_EXCEEDS_SERVER_CEILING",),
            )
        if decision.constraints.timeout_ms > context.timeout_ms:
            return self._result(
                allowed=False,
                decision=decision,
                context=context,
                snapshot_id=snapshot.snapshot_id,
                target_agent_id=entry.agent_id,
                required_scopes=entry.required_scopes,
                reason_codes=("TIMEOUT_EXCEEDS_SERVER_CEILING",),
            )
        if not entry.endpoint_via_csp:
            return self._result(
                allowed=False,
                decision=decision,
                context=context,
                snapshot_id=snapshot.snapshot_id,
                target_agent_id=entry.agent_id,
                required_scopes=entry.required_scopes,
                reason_codes=("UNTRUSTED_ENDPOINT",),
            )

        # Obligations are CSP-supplied snapshot data.  In particular, an
        # Agent manifest cannot self-assert FULL_TRACE or another policy duty.
        obligations = list(entry.required_obligations)
        if entry.approval_required:
            return self._result(
                allowed=False,
                decision=decision,
                context=context,
                snapshot_id=snapshot.snapshot_id,
                target_agent_id=entry.agent_id,
                required_scopes=entry.required_scopes,
                obligations=tuple(obligations),
                approval_required=True,
                reason_codes=("APPROVAL_REQUIRED",),
            )
        return self._result(
            allowed=True,
            decision=decision,
            context=context,
            snapshot_id=snapshot.snapshot_id,
            target_agent_id=entry.agent_id,
            required_scopes=entry.required_scopes,
            obligations=tuple(obligations),
            reason_codes=("WITHIN_AGENT_CEILING", "SNAPSHOT_MATCH", "READY_FOR_DISPATCH"),
        )

    check = evaluate
    gate = evaluate

    @staticmethod
    def grant_input(
        decision: RouteDecision,
        result: PolicyGateResult,
        context: RequestContext,
        entry: RegistryEntry,
    ) -> ExecutionGrantInput:
        """Build unsigned input for CSP's final grant enforcement."""

        if not result.allowed:
            raise ValueError("只有 allowed 且 target 綁定的 policy result 可建立 grant input")
        if result.route_decision_id != decision.decision_id:
            raise ValueError("grant input route_decision_id 不一致")
        if result.decision_id != f"pg-{decision.decision_id}":
            raise ValueError("grant input policy_decision_id 不一致")
        if result.registry_snapshot_id != decision.registry_snapshot_id:
            raise ValueError("grant input registry_snapshot_id 不一致")
        if result.target_agent_id != entry.agent_id:
            raise ValueError("grant input target_agent_id 不一致")
        if decision.route_type is not RouteType.SINGLE_AGENT:
            raise ValueError("只有 single_agent route 可建立 grant input")
        if decision.selected_agent_id != entry.agent_id:
            raise ValueError("grant input selected_agent_id 不一致")
        if entry.agent_id not in decision.candidate_agent_ids:
            raise ValueError("grant input target 不在 route candidates")
        if entry.snapshot_id != result.registry_snapshot_id:
            raise ValueError("grant input entry snapshot 不一致")
        if entry.manifest_revision is None or not entry.manifest_revision.strip():
            raise ValueError("grant input 缺少 manifest_revision")
        if not entry.has_csp_model_binding or not isinstance(entry.model_binding, ModelBinding):
            raise ValueError("grant input 缺少 CSP model binding")
        if result.effective_classification != context.classification:
            raise ValueError("grant input classification 不一致")
        if result.required_scopes != entry.required_scopes:
            raise ValueError("grant input scopes 不一致")
        return ExecutionGrantInput(
            identity=context.identity,
            owner_id=context.owner_id,
            session_id=context.session_id,
            task_id=context.task_id,
            run_id=context.run_id,
            source_snapshot_id=context.source_snapshot_id,
            trace_id=context.trace_id,
            invocation_id=context.invocation_id,
            route_decision_id=decision.decision_id,
            policy_decision_id=result.decision_id,
            registry_snapshot_id=result.registry_snapshot_id,
            target_agent_id=entry.agent_id,
            classification=result.effective_classification,
            auth_assurance=context.auth_assurance,
            allowed_capabilities=tuple(decision.required_capabilities),
            allowed_scopes=result.required_scopes,
            manifest_revision=entry.manifest_revision,
            model_binding=entry.model_binding,
        )


__all__ = ["ExecutionGrantInput", "PolicyGate"]
