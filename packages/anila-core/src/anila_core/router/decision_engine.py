"""Structured, fail-closed routing decision engine.

The routing provider is untrusted output.  It may propose only a validated
``RouteDecision`` and only from the deterministic candidate set supplied by
``CapabilityFilter``.  Legacy ``DISPATCH:`` text and markdown are explicitly
rejected; they are not an authority or a compatibility fallback here.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from anila_contracts import RouteDecision
from anila_contracts.routing import RouteConstraints, RouteType

from .candidate_filter import CandidateFilterResult, RegistryEntry, RegistrySnapshot
from .request_context import RequestContext


_LEGACY_DISPATCH_RE = re.compile(r"\bdispatch\s*:", re.IGNORECASE)
_MARKDOWN_RE = re.compile(r"```|^\s*#{1,6}\s", re.MULTILINE)
_INJECTION_PATTERNS = (
    re.compile(
        r"ignore\s+(?:(?:all|any|the)\s+)?(?:previous|prior|all|any)?\s*"
        r"(?:instructions?|rules?)",
        re.I,
    ),
    re.compile(r"忽略(?:所有|先前|之前).{0,16}(?:規則|指示)", re.I),
    re.compile(r"(?:system|developer)\s*(?:message|prompt)\s*[:：]", re.I),
    re.compile(r"you\s+are\s+now\b", re.I),
    re.compile(r"<\|(?:system|developer|assistant|im_start|im_end)\|>", re.I),
    re.compile(r"\b(?:jailbreak|prompt\s+injection|override\s+policy)\b", re.I),
)


def _contains_injection(value: Any) -> bool:
    if isinstance(value, str):
        return any(pattern.search(value) for pattern in _INJECTION_PATTERNS)
    if isinstance(value, Mapping):
        return any(
            _contains_injection(key) or _contains_injection(item) for key, item in value.items()
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_contains_injection(item) for item in value)
    return False


def _contains_legacy_dispatch(value: Any) -> bool:
    if isinstance(value, str):
        return bool(_LEGACY_DISPATCH_RE.search(value))
    if isinstance(value, Mapping):
        return any(
            _contains_legacy_dispatch(key) or _contains_legacy_dispatch(item)
            for key, item in value.items()
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_contains_legacy_dispatch(item) for item in value)
    return False


def _extract_candidates(
    candidates: CandidateFilterResult | Sequence[RegistryEntry] | None,
) -> tuple[RegistryEntry, ...]:
    if candidates is None:
        return ()
    if isinstance(candidates, CandidateFilterResult):
        return candidates.candidates
    return tuple(candidates)


@dataclass(frozen=True)
class DecisionResult:
    """Typed outcome of parsing and validating provider output."""

    decision: RouteDecision | None
    route_type: RouteType | None
    action: str
    dispatch_allowed: bool
    reason_codes: tuple[str, ...]
    safe_message: str

    @property
    def valid(self) -> bool:
        return self.decision is not None

    @property
    def is_dispatch(self) -> bool:
        return self.dispatch_allowed and self.route_type is RouteType.SINGLE_AGENT

    @property
    def reasons(self) -> tuple[str, ...]:
        return self.reason_codes

    @property
    def selected_agent_id(self) -> str | None:
        return self.decision.selected_agent_id if self.decision is not None else None


class DecisionEngine:
    """Validate and constrain one structured routing provider response."""

    def __init__(self, *, min_confidence: float = 0.70) -> None:
        if not 0.0 <= min_confidence <= 1.0:
            raise ValueError("min_confidence 必須介於 0 與 1")
        self.min_confidence = min_confidence

    @staticmethod
    def _invalid(reason: str, *, action: str = "clarify") -> DecisionResult:
        return DecisionResult(
            decision=None,
            route_type=RouteType.CLARIFY if action == "clarify" else RouteType.DENY,
            action=action,
            dispatch_allowed=False,
            reason_codes=(reason,),
            safe_message="目前無法安全決定派工，請補充資訊或稍後再試。",
        )

    @staticmethod
    def _decode(provider_output: object) -> object:
        if isinstance(provider_output, RouteDecision):
            # Even a prevalidated object is provider output at this boundary;
            # scan its canonical wire projection for control/prompt injection.
            return provider_output.model_dump(mode="json", exclude_none=False)
        if isinstance(provider_output, bytes):
            try:
                provider_output = provider_output.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError("INVALID_UTF8") from exc
        if isinstance(provider_output, str):
            text = provider_output.strip()
            if not text:
                raise ValueError("INVALID_JSON")
            if _LEGACY_DISPATCH_RE.search(text):
                raise ValueError("LEGACY_DISPATCH_UNSUPPORTED")
            if _MARKDOWN_RE.search(text):
                raise ValueError("MARKDOWN_OUTPUT")
            try:
                return json.loads(text)
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValueError("INVALID_JSON") from exc
        if isinstance(provider_output, Mapping):
            return dict(provider_output)
        raise ValueError("INVALID_JSON")

    def decide(
        self,
        provider_output: object,
        candidates: CandidateFilterResult | Sequence[RegistryEntry] | None = None,
        snapshot: RegistrySnapshot | None = None,
        *,
        context: RequestContext | None = None,
        now: datetime | None = None,
    ) -> DecisionResult:
        """Return a typed result without calling any model or Agent.

        ``candidates`` may be the ``CandidateFilterResult`` directly or a
        plain sequence of immutable entries for adapter/test convenience.
        """

        try:
            decoded = self._decode(provider_output)
        except ValueError as exc:
            return self._invalid(str(exc))

        if _contains_legacy_dispatch(decoded):
            return self._invalid("LEGACY_DISPATCH_UNSUPPORTED")
        if _contains_injection(decoded):
            return self._invalid("PROMPT_INJECTION_OUTPUT", action="deny")
        try:
            decision = RouteDecision.model_validate(decoded)
        except Exception:
            return self._invalid("INVALID_ROUTE_DECISION")

        if decision.route_type is RouteType.SINGLE_AGENT and snapshot is None:
            return self._invalid("SNAPSHOT_MISSING")
        if snapshot is not None:
            if not snapshot.is_fresh(now=now):
                return self._invalid("SNAPSHOT_STALE")
            if decision.registry_snapshot_id != snapshot.snapshot_id:
                return self._invalid("SNAPSHOT_MISMATCH")
        elif isinstance(candidates, CandidateFilterResult) and candidates.snapshot_id is None:
            return self._invalid("SNAPSHOT_MISSING")

        if decision.route_type is RouteType.MULTI_AGENT_PLAN:
            # R3 does not activate plans.  Leaving this as an opaque contract
            # field is useful for version negotiation, but execution is denied.
            return self._invalid("MULTI_AGENT_UNSUPPORTED", action="deny")

        filtered = _extract_candidates(candidates)
        candidate_map = {entry.agent_id: entry for entry in filtered}
        unknown_candidates = [
            agent_id for agent_id in decision.candidate_agent_ids if agent_id not in candidate_map
        ]
        if unknown_candidates:
            return self._invalid("UNKNOWN_AGENT")

        if decision.route_type is RouteType.SINGLE_AGENT:
            if not filtered:
                return self._invalid("NO_ELIGIBLE_CANDIDATES")
            selected = decision.selected_agent_id
            if selected is None or selected not in candidate_map:
                return self._invalid("UNKNOWN_AGENT")
            if context is not None and not set(context.required_capabilities).issubset(
                set(decision.required_capabilities)
            ):
                return self._invalid("CAPABILITY_MISMATCH")
            if decision.confidence < self.min_confidence:
                return self._invalid("LOW_CONFIDENCE")

            if context is not None:
                # Provider limits are untrusted.  Clamp them to server
                # ceilings before the policy gate sees the route.
                max_steps = min(decision.constraints.max_steps, context.max_steps)
                timeout_ms = min(decision.constraints.timeout_ms, context.timeout_ms)
                if max_steps < 1 or timeout_ms < 1:
                    return self._invalid("CONSTRAINT_EXCEEDS_SERVER_CEILING")
                if (
                    max_steps != decision.constraints.max_steps
                    or timeout_ms != decision.constraints.timeout_ms
                ):
                    decision = decision.model_copy(
                        update={
                            "constraints": RouteConstraints(
                                max_steps=max_steps,
                                timeout_ms=timeout_ms,
                            )
                        }
                    )
            return DecisionResult(
                decision=decision,
                route_type=RouteType.SINGLE_AGENT,
                action="single_agent",
                dispatch_allowed=True,
                reason_codes=tuple(decision.reason_codes),
                safe_message="已通過結構化路由驗證。",
            )

        # direct/clarify/deny never call a downstream Agent.  Empty candidate
        # lists are valid for direct answers; non-empty lists were checked above
        # against the deterministic filtered set.
        if (
            decision.confidence < self.min_confidence
            and decision.route_type is RouteType.DIRECT_ANSWER
        ):
            return self._invalid("LOW_CONFIDENCE")
        action = decision.route_type.value
        return DecisionResult(
            decision=decision,
            route_type=decision.route_type,
            action=action,
            dispatch_allowed=False,
            reason_codes=tuple(decision.reason_codes),
            safe_message="路由決策不需要下游 Agent。",
        )

    evaluate = decide


__all__ = ["DecisionEngine", "DecisionResult"]
