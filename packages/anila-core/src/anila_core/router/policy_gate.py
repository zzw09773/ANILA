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


@dataclass(frozen=True)
class DirectModelGovernance:
    """Trusted governance facts for the Router 直答(DIRECT_ANSWER)主模型。

    DIRECT_ANSWER 使用 Router 自身設定的主模型直接產生文字回答。此為 CSP／
    部署提供的受信任治理事實(不是路由模型的輸出),PolicyGate 依此做等同
    ``enforce_model_ceiling`` 的分類上限評估:``allow if classification <=
    classification_ceiling``。缺少或無效的治理輸入一律 fail-closed 拒絕;
    真正的出向呼叫仍會在 CSP model gateway 再受 ``enforce_model_ceiling``
    強制(defense in depth),此處為 Router 端等價前置閘。

    ``context_window`` 是同一份受信任投影帶來的**容量事實**(來自
    ``model_registry.context_window``)。它不參與任何授權判斷,純粹讓 Router 能
    依部署模型的真實容量算 token 預算(見 :mod:`anila_core.router.token_budget`)。
    之所以掛在這裡而不另開一條通道:Router 對「當前主模型的可信事實」本來就只有
    這一個 CSP 治理 seam,多開一條只會多一份會漂移的快取。未登記時是 ``None``,
    容量相關的降級由 token_budget 決定,絕不影響分類上限的 fail-closed 行為。
    """

    model_id: str
    gateway: str
    classification_ceiling: ClassificationLevel | None
    context_window: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.model_id, str) or not self.model_id.strip():
            raise ValueError("direct model governance 缺少 model_id")
        if not isinstance(self.gateway, str) or not self.gateway.strip():
            raise ValueError("direct model governance 缺少 gateway")
        if self.classification_ceiling is not None and not isinstance(
            self.classification_ceiling, ClassificationLevel
        ):
            raise TypeError("direct model classification_ceiling 必須是 ClassificationLevel")
        if self.context_window is not None and (
            isinstance(self.context_window, bool)
            or not isinstance(self.context_window, int)
            or self.context_window <= 0
        ):
            raise ValueError("direct model context_window 必須是正整數或 None")

    @property
    def has_csp_model_binding(self) -> bool:
        """直答模型是否綁定唯一的 CSP model gateway。"""

        return self.gateway.strip().casefold() == "csp"


class PolicyGate:
    """Fail-closed policy checks over a validated route and CSP snapshot."""

    def __init__(
        self,
        *,
        require_csp_model_binding: bool = True,
        direct_model_governance: DirectModelGovernance | None = None,
    ) -> None:
        self.require_csp_model_binding = require_csp_model_binding
        # R7:直答的模型治理輸入。None 代表沒有受信任的治理輸入 → 直答
        # 一律 fail-closed 拒絕(維持舊有的保守姿態,只是換成可解釋的原因)。
        self.direct_model_governance = direct_model_governance

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
            return self._evaluate_direct_answer(decision, context, snapshot_id)

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

    def _evaluate_direct_answer(
        self,
        decision: RouteDecision,
        context: RequestContext,
        snapshot_id: str,
    ) -> PolicyGateResult:
        """R7:直答的模型治理閘,語意等同 ``enforce_model_ceiling``。

        沒有受信任的治理輸入、非 CSP gateway、缺少分類上限、或請求分類超過
        上限時一律 fail-closed 拒絕;僅在分類 <= 直答模型分類上限時 allow。
        DIRECT_ANSWER 不派工給下游 Agent,故不綁定 target/scope/grant。
        """

        governance = self.direct_model_governance
        if governance is None:
            return self._result(
                allowed=False,
                decision=decision,
                context=context,
                snapshot_id=snapshot_id,
                target_agent_id=None,
                reason_codes=("DIRECT_MODEL_GOVERNANCE_UNAVAILABLE",),
            )
        if self.require_csp_model_binding and not governance.has_csp_model_binding:
            return self._result(
                allowed=False,
                decision=decision,
                context=context,
                snapshot_id=snapshot_id,
                target_agent_id=None,
                reason_codes=("DIRECT_MODEL_BINDING_MISSING",),
            )
        if governance.classification_ceiling is None:
            return self._result(
                allowed=False,
                decision=decision,
                context=context,
                snapshot_id=snapshot_id,
                target_agent_id=None,
                reason_codes=("DIRECT_MODEL_CLASSIFICATION_CEILING_MISSING",),
            )
        if context.classification > governance.classification_ceiling:
            return self._result(
                allowed=False,
                decision=decision,
                context=context,
                snapshot_id=snapshot_id,
                target_agent_id=None,
                reason_codes=("DIRECT_MODEL_CLASSIFICATION_EXCEEDS_CEILING",),
            )
        return self._result(
            allowed=True,
            decision=decision,
            context=context,
            snapshot_id=snapshot_id,
            target_agent_id=None,
            reason_codes=("DIRECT_MODEL_POLICY_EVALUATED", "DIRECT_MODEL_WITHIN_CEILING"),
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


__all__ = ["DirectModelGovernance", "ExecutionGrantInput", "PolicyGate"]
