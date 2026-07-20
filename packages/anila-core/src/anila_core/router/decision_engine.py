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
from anila_contracts.routing import RouteConstraints, RouteFallback, RouteType
from anila_security.model_governance import classification_rank

from .candidate_filter import (
    CandidateFilterResult,
    RegistryEntry,
    RegistrySnapshot,
    candidate_eligibility_reasons,
)
from .injection_detection import MAX_REQUEST_CONTENT_SCAN_CHARS, contains_injection
from .request_context import RequestContext


_LEGACY_DISPATCH_RE = re.compile(r"\bdispatch\s*:", re.IGNORECASE)
_MARKDOWN_RE = re.compile(r"```|^\s*#{1,6}\s", re.MULTILINE)
_PLAIN_TEXT_CAPABILITIES = frozenset({"text"})


def available_registry_entries(
    snapshot: RegistrySnapshot | None,
    context: RequestContext | None = None,
    *,
    include_classification: bool = True,
    eligible_except_scope: bool = False,
) -> tuple[RegistryEntry, ...]:
    """Return the canonical CandidateFilter-eligible registry projection.

    With no request context this is the structural registry projection used by
    the routing prompt and E3 profile matching.  E2 supplies a context and
    disables only the classification comparison while it evaluates the
    ceilings; every other CandidateFilter predicate remains shared and active.
    E3 may additionally request the context-aware projection with scope
    checking deferred to its per-profile complete-scope test.
    """

    if snapshot is None:
        return ()
    return tuple(
        entry
        for entry in snapshot.entries
        if not candidate_eligibility_reasons(
            entry,
            context,
            snapshot,
            include_classification=include_classification,
            eligible_except_scope=eligible_except_scope,
        )
    )


def _contains_injection(value: Any) -> bool:
    return contains_injection(value)


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


def _request_has_injection(
    context: RequestContext | None,
    request_content: str | None,
) -> bool:
    """Scan only untrusted request content, never authority or model fields."""

    if isinstance(request_content, str):
        if len(request_content) > MAX_REQUEST_CONTENT_SCAN_CHARS:
            return True
        return _contains_injection(request_content)
    if context is None:
        return False
    if any(item.role == "user" and _contains_injection(item.content) for item in context.history):
        return True
    return bool(context.history_summary and _contains_injection(context.history_summary))


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

    @staticmethod
    def _forced_deny(decision: RouteDecision, reason: str) -> DecisionResult:
        """Return a redacted deny projection for a de-escalated route."""

        denied = decision.model_copy(
            update={
                "route_type": RouteType.DENY,
                "candidate_agent_ids": (),
                "selected_agent_id": None,
                "rewritten_query": None,
                "reason_codes": (reason,),
                "fallback": RouteFallback.DENY,
                "execution_plan": None,
            }
        )
        return DecisionResult(
            decision=denied,
            route_type=RouteType.DENY,
            action="deny",
            dispatch_allowed=False,
            reason_codes=(reason,),
            safe_message="目前無法安全決定派工，請補充資訊或稍後再試。",
        )

    @staticmethod
    def _classification_exceeds_every_ceiling(
        context: RequestContext | None,
        snapshot: RegistrySnapshot | None,
    ) -> bool:
        if context is None or snapshot is None:
            return False
        ceilings = tuple(
            entry.classification_ceiling
            for entry in available_registry_entries(
                snapshot,
                context,
                include_classification=False,
            )
            if entry.classification_ceiling is not None
        )
        if not ceilings:
            return False
        request_rank = classification_rank(context.classification)
        return all(request_rank > classification_rank(ceiling) for ceiling in ceilings)

    @staticmethod
    def _missing_specialized_scopes(
        context: RequestContext | None,
        snapshot: RegistrySnapshot | None,
        *,
        selected_agent_id: str | None = None,
    ) -> tuple[str, ...]:
        """Return missing scopes for the selected or a viable profile.

        An unknown capability is specialized by default.  When it has no
        advertised matching profile, the absence of an authoritative scope
        contract is itself a denial; it cannot pass merely because the
        baseline ``agent:invoke`` scope is present.  A selected route checks
        only that selected profile.  A non-selected route passes when at least
        one matching profile's complete CSP-owned ``required_scopes`` set is
        satisfied; unrelated profiles never contribute a false-denying scope.
        """

        if context is None or snapshot is None:
            return ()
        required = set(context.required_capabilities)
        if not required.difference(_PLAIN_TEXT_CAPABILITIES):
            return ()

        eligible = available_registry_entries(
            snapshot,
            context,
            include_classification=True,
            eligible_except_scope=True,
        )
        eligible_ids = {entry.agent_id for entry in eligible}
        if selected_agent_id is not None and selected_agent_id not in eligible_ids:
            return ("INELIGIBLE_SELECTED_AGENT",)

        matching = tuple(entry for entry in eligible if required.issubset(set(entry.capabilities)))
        if not matching:
            return ("UNADVERTISED_CAPABILITY",)
        if selected_agent_id is not None:
            selected = next(
                (entry for entry in matching if entry.agent_id == selected_agent_id),
                None,
            )
            if selected is not None:
                return tuple(sorted(set(selected.required_scopes).difference(context.scopes)))
        if any(set(entry.required_scopes).issubset(set(context.scopes)) for entry in matching):
            return ()
        return ("MISSING_REQUIRED_SCOPES",)

    def _enforce_fail_closed(
        self,
        decision: RouteDecision,
        context: RequestContext | None,
        snapshot: RegistrySnapshot | None,
        request_content: str | None,
    ) -> DecisionResult | None:
        """Apply ordered de-escalation rules after model parsing only."""

        # A deny is already the lowest-authority outcome.  Enforcement may
        # never turn it into another route or replace its provider reason.
        if decision.route_type is RouteType.DENY:
            return DecisionResult(
                decision=decision,
                route_type=RouteType.DENY,
                action="deny",
                dispatch_allowed=False,
                reason_codes=tuple(decision.reason_codes),
                safe_message="目前無法安全決定派工，請補充資訊或稍後再試。",
            )
        if _request_has_injection(context, request_content):
            return self._forced_deny(decision, "INJECTION_INPUT_DENIED")
        if self._classification_exceeds_every_ceiling(context, snapshot):
            return self._forced_deny(decision, "CLASSIFICATION_CEILING_DENIED")
        if self._missing_specialized_scopes(
            context,
            snapshot,
            selected_agent_id=decision.selected_agent_id,
        ):
            return self._forced_deny(decision, "SCOPE_CAPABILITY_DENIED")
        return None

    def decide(
        self,
        provider_output: object,
        candidates: CandidateFilterResult | Sequence[RegistryEntry] | None = None,
        snapshot: RegistrySnapshot | None = None,
        *,
        context: RequestContext | None = None,
        request_content: str | None = None,
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

        enforced = self._enforce_fail_closed(
            decision,
            context,
            snapshot,
            request_content,
        )
        if enforced is not None:
            return enforced

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


__all__ = ["DecisionEngine", "DecisionResult", "available_registry_entries"]
