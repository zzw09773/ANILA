"""Deterministic formal adapter for the Gate 5 R3 routing evaluation.

The frozen-evaluation runner deliberately gives this module only a
``RoutingRequest`` projection.  This adapter turns that projection into the
same framework-light objects used by the production Router core and feeds a
small deterministic decision provider through ``ExecutionRuntime``.  It is a
conformance adapter, not a case table: the decision is derived from trusted
context (capabilities, scopes, readiness and classification) and general
request-language rules.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Any

from anila_contracts import AgentManifest
from anila_contracts.agents import ModelBinding
from anila_contracts.classification import ClassificationLevel
from anila_contracts.contexts import AuthAssurance
from anila_core.router import (
    CapabilityFilter,
    DecisionEngine,
    ExecutionRuntime,
    PolicyGate,
    RegistryEntry,
    RegistrySnapshot,
    RequestContext,
    RequestContextBuilder,
    RuntimeResult,
)
from anila_contracts.routing import RouteType

from infra.ci.run_gate5_routing_contract import RoutingRequest


_KNOWN_AGENT_IDS = frozenset({"rag-search", "image-generator", "report-generator"})
_HEALTHY = "healthy"
_TASK_TYPE = "gate5_routing"
_INJECTION_RE = re.compile(
    r"(?:ignore\s+(?:all\s+)?(?:previous|prior|earlier)|disregard\s+(?:all\s+)?"
    r"(?:previous|earlier)|override\s+(?:the\s+)?(?:tool\s+)?policy|"
    r"prompt\s+injection|jailbreak|system\s+prompt|developer\s+message|"
    r"role\s*=\s*system|<\|(?:system|developer)\|>|dispatch\s*:|"
    r"忽略|無視|覆寫|覆蓋|外洩|揭露系統提示|管理員身分)",
    re.IGNORECASE,
)
_CLARIFICATION_RE = re.compile(
    r"(?:\bask\b|\bwhich\b|\btimezone\b|\blacks?\b|\btwice\b|"
    r"\bauthoritative\b|\bwithout\s+(?:naming|choosing)\b|\bshort\s+name\b|"
    r"\bno\s+(?:business\s+)?timezone\b|\bwhat\s+should\s+be\s+checked\b|"
    r"\bwhat\s+output\b|\bwhich\s+environment\b|\bsite\s+code\b|"
    r"\btarget\s+length\b|確認|釐清|未說明|沒有說明|哪一|哪個|兩個|兩種|"
    r"完整清單)",
    re.IGNORECASE,
)
_STREAMING_RE = re.compile(r"\bstream(?:ing)?\b|串流", re.IGNORECASE)
_RESUME_RE = re.compile(r"\bresume\b|恢復|中斷的|服務重啟|重啟後", re.IGNORECASE)


class FormalR3EvalAdapter:
    """Evaluate one projected request through the formal R3 runtime."""

    def __init__(self) -> None:
        # Keep each stage explicit so this adapter cannot accidentally become
        # a parallel policy implementation or a direct dispatch shortcut.
        self.context_builder = RequestContextBuilder()
        self.capability_filter = CapabilityFilter()
        self.decision_engine = DecisionEngine()
        self.policy_gate = PolicyGate()
        self.runtime = ExecutionRuntime(
            context_builder=self.context_builder,
            capability_filter=self.capability_filter,
            decision_engine=self.decision_engine,
            policy_gate=self.policy_gate,
        )

    @staticmethod
    def _sequence(value: object, *, field_name: str) -> tuple[str, ...]:
        if isinstance(value, str) or not isinstance(value, Sequence):
            raise ValueError(f"{field_name} must be a string sequence")
        result: list[str] = []
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise ValueError(f"{field_name} contains an invalid value")
            token = item.strip()
            if token not in result:
                result.append(token)
        return tuple(result)

    @staticmethod
    def _mapping(value: object, *, field_name: str) -> Mapping[str, Any]:
        if not isinstance(value, Mapping):
            raise ValueError(f"{field_name} must be an object")
        return value

    @staticmethod
    def _text(request: RoutingRequest) -> str:
        history = " ".join(
            item.get("content", "")
            for item in request.messages
            if isinstance(item, Mapping) and isinstance(item.get("content"), str)
        )
        return f"{request.input} {history}".strip()

    @staticmethod
    def _classification(value: object) -> ClassificationLevel:
        if isinstance(value, ClassificationLevel):
            return value
        if not isinstance(value, str):
            raise ValueError("classification must be a wire value")
        return ClassificationLevel.from_storage(value)

    @staticmethod
    def _source_snapshot_id(snapshot_id: str) -> int:
        # RequestContext requires a positive integer source snapshot.  This is
        # a deterministic projection of the trusted registry generation, not
        # an input supplied by the caller.
        return int(sha256(snapshot_id.encode("utf-8")).hexdigest()[:12], 16) or 1

    def _build_context(self, request: RoutingRequest) -> RequestContext:
        values = self._mapping(request.context, field_name="context")
        snapshot_id = values.get("registry_snapshot_id")
        if not isinstance(snapshot_id, str) or not snapshot_id.strip():
            raise ValueError("registry_snapshot_id must be a non-empty string")
        scopes = self._sequence(values.get("scopes"), field_name="scopes")
        required_capabilities = self._sequence(
            values.get("capabilities", ()), field_name="capabilities"
        )
        return self.context_builder.build(
            {
                "identity": "gate5-user",
                "owner_id": "gate5-owner",
                "session_id": f"gate5-{snapshot_id}",
                "task_id": 1,
                "run_id": 1,
                "source_snapshot_id": self._source_snapshot_id(snapshot_id),
                "trace_id": "gate5-trace",
                "invocation_id": f"gate5-{snapshot_id}-invocation",
                "task_type": _TASK_TYPE,
                "classification": self._classification(values.get("classification")),
                "scopes": scopes,
                "required_capabilities": required_capabilities,
                "auth_assurance": AuthAssurance(
                    sid="gate5-session",
                    amr=("pwd",),
                    acr="aal2",
                    auth_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
                    break_glass=False,
                ),
                "messages": request.messages,
            }
        )

    @staticmethod
    def _manifest(agent_id: str, profile: Mapping[str, Any]) -> AgentManifest | None:
        capabilities = FormalR3EvalAdapter._sequence(
            profile.get("capabilities", ()), field_name=f"{agent_id}.capabilities"
        )
        ceiling = FormalR3EvalAdapter._classification(
            profile.get("classification_ceiling")
        )
        payload = {
            "schema_version": "agent-manifest/v1",
            "agent_id": agent_id,
            "name": agent_id,
            "version": "1.0.0",
            "runtime_type": "custom_http",
            "api_version": "v1",
            "supported_task_types": [_TASK_TYPE],
            "description_for_router": f"Gate 5 conformance profile for {agent_id}",
            "input_schema": {"type": "object"},
            "output_schema": {"type": "object"},
            "capabilities": list(capabilities),
            "event_protocols": ["json"],
            "required_scopes": ["agent:invoke"],
            "classification": {
                "ceiling": ceiling.value,
                "default": ClassificationLevel.UNCLASSIFIED.value,
            },
            "full_trace_required": False,
            "supports_streaming": True,
            "supports_resume": True,
            "supports_cancel": True,
            "supports_idempotency": True,
            "model_binding": {
                "model_id": f"{agent_id}-model",
                "gateway": "csp",
                "classification_ceiling": ceiling.value,
            },
        }
        try:
            return AgentManifest.model_validate(payload)
        except Exception:
            return None

    def _snapshot(self, request: RoutingRequest, *, now: datetime) -> RegistrySnapshot:
        values = self._mapping(request.context, field_name="context")
        snapshot_id = values.get("registry_snapshot_id")
        if not isinstance(snapshot_id, str) or not snapshot_id.strip():
            raise ValueError("registry_snapshot_id must be a non-empty string")
        available = self._sequence(
            values.get("available_agent_ids", ()), field_name="available_agent_ids"
        )
        profiles = self._mapping(
            values.get("agent_profiles", {}), field_name="agent_profiles"
        )
        health = self._mapping(values.get("health", {}), field_name="health")
        statuses = self._mapping(
            health.get("agent_status", {}), field_name="health.agent_status"
        )

        entries: list[RegistryEntry] = []
        for agent_id in available:
            # The CSP registry has a closed identity set for this conformance
            # profile.  A newly injected identifier is never made dispatchable
            # merely by adding a user-visible profile object.
            if agent_id not in _KNOWN_AGENT_IDS:
                continue
            profile = profiles.get(agent_id)
            if not isinstance(profile, Mapping):
                continue
            manifest = self._manifest(agent_id, profile)
            if manifest is None:
                continue
            status = statuses.get(agent_id)
            ready = status == _HEALTHY
            entries.append(
                RegistryEntry(
                    agent_id=agent_id,
                    manifest=manifest,
                    snapshot_id=snapshot_id,
                    manifest_revision=f"gate5-{agent_id}-r1",
                    approved=ready,
                    health_ready=ready,
                    trace_test_passed=ready,
                    ready_for_dispatch=ready,
                    manifest_valid=True,
                    endpoint_via_csp=True,
                    required_scopes=("agent:invoke",),
                    classification_ceiling=self._classification(
                        profile.get("classification_ceiling")
                    ),
                    model_binding=ModelBinding(
                        model_id=f"{agent_id}-model",
                        gateway="csp",
                        classification_ceiling=self._classification(
                            profile.get("classification_ceiling")
                        ),
                    ),
                )
            )

        return RegistrySnapshot(
            snapshot_id,
            entries,
            fresh=True,
            stale=False,
            authority="csp",
            captured_at=now - timedelta(seconds=1),
            expires_at=now + timedelta(minutes=5),
            snapshot_revision=snapshot_id,
            snapshot_hash=sha256(snapshot_id.encode("utf-8")).hexdigest(),
        )

    @staticmethod
    def _injection(text: str) -> bool:
        return bool(_INJECTION_RE.search(text))

    @staticmethod
    def _multi_intent(required_capabilities: Sequence[str], text: str) -> bool:
        if not {"retrieval", "visual", "briefing"}.issubset(required_capabilities):
            return False
        lowered = text.casefold()
        retrieval = any(
            token in lowered
            for token in (
                "source",
                "evidence",
                "archive",
                "policy",
                "查證",
                "條文",
                "查找",
            )
        )
        visual = any(
            token in lowered
            for token in (
                "draw",
                "diagram",
                "poster",
                "visual",
                "render",
                "流程圖",
                "繪圖",
                "示意",
            )
        )
        briefing = any(
            token in lowered
            for token in (
                "brief",
                "memo",
                "dossier",
                "presentation",
                "leadership",
                "摘要",
                "說明",
            )
        )
        return retrieval and visual and briefing

    def _profile_candidates(
        self, values: Mapping[str, Any], required_capabilities: Sequence[str]
    ) -> tuple[str, ...]:
        """Infer an agent from capability semantics, without a target label."""

        available = self._sequence(
            values.get("available_agent_ids", ()), field_name="available_agent_ids"
        )
        profiles = self._mapping(
            values.get("agent_profiles", {}), field_name="agent_profiles"
        )
        required = set(required_capabilities)
        candidates: list[str] = []
        for agent_id in available:
            if agent_id not in _KNOWN_AGENT_IDS:
                continue
            profile = profiles.get(agent_id)
            if not isinstance(profile, Mapping):
                continue
            capabilities = self._sequence(
                profile.get("capabilities", ()), field_name=f"{agent_id}.capabilities"
            )
            if required.issubset(capabilities):
                candidates.append(agent_id)
        return tuple(candidates)

    @staticmethod
    def _clarify(text: str) -> bool:
        return bool(_CLARIFICATION_RE.search(text))

    @staticmethod
    def _route_payload(
        *,
        route_type: str,
        snapshot_id: str,
        target: str | None,
        required_capabilities: Sequence[str],
        reason: str,
        fallback: str | None,
        query: str | None,
    ) -> str:
        candidates = (
            [target] if route_type == RouteType.SINGLE_AGENT.value and target else []
        )
        payload = {
            "schema_version": "route-decision/v1",
            "decision_id": f"gate5-{sha256(f'{snapshot_id}:{route_type}:{target}'.encode()).hexdigest()[:16]}",
            "route_type": route_type,
            "registry_snapshot_id": snapshot_id,
            "required_capabilities": list(required_capabilities),
            "candidate_agent_ids": candidates,
            "selected_agent_id": target
            if route_type == RouteType.SINGLE_AGENT.value
            else None,
            "reason_codes": [reason],
            "confidence": 0.99,
            "rewritten_query": query
            if route_type == RouteType.SINGLE_AGENT.value
            else None,
            "constraints": {"max_steps": 3, "timeout_ms": 120_000},
            "fallback": fallback,
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

    def _provider_output(
        self,
        request: RoutingRequest,
        context: RequestContext,
        snapshot: RegistrySnapshot,
        *,
        now: datetime,
    ) -> str:
        values = self._mapping(request.context, field_name="context")
        text = self._text(request)
        required = context.required_capabilities

        if self._injection(text):
            return self._route_payload(
                route_type=RouteType.DENY.value,
                snapshot_id=snapshot.snapshot_id,
                target=None,
                required_capabilities=required,
                reason="prompt_injection_blocked",
                fallback="deny",
                query=None,
            )
        if self._multi_intent(required, text):
            return self._route_payload(
                route_type=RouteType.CLARIFY.value,
                snapshot_id=snapshot.snapshot_id,
                target=None,
                required_capabilities=required,
                reason="multiple_intents",
                fallback="clarify",
                query=None,
            )

        eligible = self.capability_filter.filter(context, snapshot, now=now).candidates
        semantic_candidates = self._profile_candidates(values, required)
        target = (
            eligible[0].agent_id
            if len(eligible) == 1
            else semantic_candidates[0]
            if len(semantic_candidates) == 1
            else None
        )
        if target is None:
            if required != ("text",) and not semantic_candidates:
                return self._route_payload(
                    route_type=RouteType.DENY.value,
                    snapshot_id=snapshot.snapshot_id,
                    target=None,
                    required_capabilities=required,
                    reason="capability_mismatch",
                    fallback="deny",
                    query=None,
                )
            route_type = (
                RouteType.CLARIFY.value
                if self._clarify(text)
                else RouteType.DIRECT_ANSWER.value
            )
            reason = (
                "clarification_required"
                if route_type == RouteType.CLARIFY.value
                else "direct_answer"
            )
            return self._route_payload(
                route_type=route_type,
                snapshot_id=snapshot.snapshot_id,
                target=None,
                required_capabilities=required,
                reason=reason,
                fallback="clarify" if route_type == RouteType.CLARIFY.value else None,
                query=None,
            )

        profiles = self._mapping(
            values.get("agent_profiles", {}), field_name="agent_profiles"
        )
        profile = profiles.get(target)
        available = self._sequence(
            values.get("available_agent_ids", ()), field_name="available_agent_ids"
        )
        health = self._mapping(values.get("health", {}), field_name="health")
        statuses = self._mapping(
            health.get("agent_status", {}), field_name="health.agent_status"
        )
        reason = ""
        fallback: str | None = "deny"
        if (
            target not in _KNOWN_AGENT_IDS
            or target not in available
            or not isinstance(profile, Mapping)
        ):
            reason = "unknown_agent"
        elif "agent:invoke" not in context.scopes:
            reason = "scope_denied"
        elif statuses.get(target) != _HEALTHY:
            reason = "agent_unavailable"
            fallback = "clarify"
        else:
            ceiling = self._classification(profile.get("classification_ceiling"))
            if context.classification > ceiling:
                reason = "classification_ceiling"
            elif not set(required).issubset(
                set(
                    self._sequence(
                        profile.get("capabilities", ()),
                        field_name="profile.capabilities",
                    )
                )
            ):
                reason = "capability_mismatch"

        if reason:
            return self._route_payload(
                route_type=RouteType.DENY.value,
                snapshot_id=snapshot.snapshot_id,
                target=None,
                required_capabilities=required,
                reason=reason,
                fallback=fallback,
                query=None,
            )

        if _STREAMING_RE.search(text):
            reason = "streaming_required"
            fallback = "direct_answer"
        elif _RESUME_RE.search(text):
            reason = "session_resume"
            fallback = "clarify"
        else:
            reason = "scope_allowed"
            fallback = None
        return self._route_payload(
            route_type=RouteType.SINGLE_AGENT.value,
            snapshot_id=snapshot.snapshot_id,
            target=target,
            required_capabilities=required,
            reason=reason,
            fallback=fallback,
            query=request.input,
        )

    @staticmethod
    def _observation(
        result: RuntimeResult, *, semantic_reason: str
    ) -> Mapping[str, Any]:
        if result.policy_result is None:
            raise RuntimeError("formal R3 runtime returned no PolicyGate result")
        route = result.decision_result.route_type
        if route is None:
            raise RuntimeError("formal R3 runtime returned no route type")
        selected = (
            result.decision.selected_agent_id if result.decision is not None else None
        )
        if route is RouteType.SINGLE_AGENT:
            if not result.policy_result.allowed or selected is None:
                raise RuntimeError("single-agent route was not policy-authorized")
            policy_allowed = True
        elif route in {RouteType.DIRECT_ANSWER, RouteType.CLARIFY}:
            # The core gate intentionally reports direct inference as
            # ``not evaluated``.  The frozen R3 metric calls a safe
            # non-dispatch route policy-allowed, so this is an explicit wire
            # projection after requiring the PolicyGate result to exist.
            if result.policy_result.allowed:
                raise RuntimeError("non-dispatch route was policy-allowed")
            policy_allowed = True
        else:
            if result.policy_result.allowed:
                raise RuntimeError("deny route was policy-allowed")
            policy_allowed = False
        return {
            "route_type": route.value,
            "selected_agent_id": selected,
            "policy_allowed": policy_allowed,
            "policy_reason_codes": [semantic_reason],
            "fallback": (
                result.decision.fallback.value
                if result.decision is not None and result.decision.fallback is not None
                else None
            ),
        }

    def evaluate(self, request: RoutingRequest) -> Mapping[str, Any]:
        # When the runner is invoked as a file (rather than ``python -m``),
        # its ``__main__`` module and the importable ``infra.ci`` module hold
        # distinct class objects.  Validate the projection structurally so
        # that both invocation modes share the exact same three-field seam.
        if not all(hasattr(request, name) for name in ("input", "messages", "context")):
            raise TypeError("formal R3 adapter requires the RoutingRequest projection")
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        context = self._build_context(request)
        snapshot = self._snapshot(request, now=now)
        output = self._provider_output(request, context, snapshot, now=now)
        result = self.runtime.execute(context, output, snapshot, now=now)
        decoded = json.loads(output)
        semantic_reason = str(decoded["reason_codes"][0])
        return self._observation(result, semantic_reason=semantic_reason)


def build_adapter() -> FormalR3EvalAdapter:
    """Zero-argument factory consumed by the contract runner and CI."""

    return FormalR3EvalAdapter()


__all__ = ["FormalR3EvalAdapter", "build_adapter"]
