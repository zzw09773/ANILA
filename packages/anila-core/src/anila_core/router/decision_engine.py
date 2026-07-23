"""Structured, fail-closed routing decision engine.

The routing provider is untrusted output.  It may propose only a validated
``RouteDecision`` and only from the deterministic candidate set supplied by
``CapabilityFilter``.  Legacy ``DISPATCH:`` text and markdown are explicitly
rejected; they are not an authority or a compatibility fallback here.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import re
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from anila_contracts import RouteDecision
from anila_contracts.routing import (
    ROUTE_DECISION_SCHEMA_VERSION,
    RouteConstraints,
    RouteFallback,
    RouteType,
)
from anila_security.model_governance import classification_rank

from .candidate_filter import (
    CandidateFilterResult,
    RegistryEntry,
    RegistrySnapshot,
    candidate_eligibility_reasons,
)
from .injection_detection import MAX_REQUEST_CONTENT_SCAN_CHARS, contains_injection
from .request_context import RequestContext


logger = logging.getLogger(__name__)

_LEGACY_DISPATCH_RE = re.compile(r"\bdispatch\s*:", re.IGNORECASE)
_MARKDOWN_RE = re.compile(r"```|^\s*#{1,6}\s", re.MULTILINE)
_PLAIN_TEXT_CAPABILITIES = frozenset({"text"})

# Judge parse/schema failures: honest fail-closed message + exactly one pipeline retry.
RETRYABLE_JUDGE_REASON_CODES = frozenset(
    {
        "INVALID_JSON",
        "MARKDOWN_OUTPUT",
        "INVALID_ROUTE_DECISION",
    }
)
_JUDGE_OUTPUT_INVALID_REASONS = RETRYABLE_JUDGE_REASON_CODES
_SECURITY_DENY_REASONS = frozenset(
    {
        "PROMPT_INJECTION_OUTPUT",
        "LEGACY_DISPATCH_UNSUPPORTED",
        "INJECTION_INPUT_DENIED",
        "CLASSIFICATION_CEILING_DENIED",
        "SCOPE_CAPABILITY_DENIED",
        "MULTI_AGENT_UNSUPPORTED",
    }
)

_SAFE_MESSAGE_JUDGE_INVALID = "路由判斷模型未能產生有效決策，已安全停止。請稍後再試。"
_SAFE_MESSAGE_DENY = "目前無法安全決定派工，請補充資訊或稍後再試。"
_SAFE_MESSAGE_CLARIFY_FAIL_CLOSED = "目前無法安全決定派工，請補充資訊或稍後再試。"
_LOG_SAMPLE_MAX_CHARS = 500
# Log samples run BEFORE schema validation, so nothing in the payload is
# proven server-owned yet. Posture: keys outside the structural whitelist are
# masked (a judge can stuff user content into key NAMES); values are kept only
# when they belong to a closed enum set derived from the real contracts enums;
# every other string (ids, reason codes, free text) is reduced to its length.
# Unparseable raw text logs length + digest only — never a verbatim prefix.
_SAFE_STRING_KEYS = frozenset(
    {
        "route_type",
        "action",
        "fallback",
        "status",
        "schema_version",
        "confidence",
        "registry_snapshot_id",
        "selected_agent_id",
        "candidate_agent_ids",
        "reason_codes",
        "rewritten_query",
        "execution_plan",
        "constraints",
        "decision_id",
        "required_capabilities",
    }
)
def _known_enum_values() -> dict[str, frozenset[str]]:
    route_values = frozenset(member.value for member in RouteType)
    fallback_values = frozenset(member.value for member in RouteFallback)
    return {
        "route_type": route_values,
        "action": route_values,
        "fallback": fallback_values,
        # Closed literal from the contracts, not a pattern: an open-ended
        # digit/token regex would retain digit-only or token-shaped secrets
        # stuffed under this key.
        "schema_version": frozenset({ROUTE_DECISION_SCHEMA_VERSION}),
    }

_judge_log_attempt: contextvars.ContextVar[int] = contextvars.ContextVar(
    "anila_judge_log_attempt", default=1
)
_judge_log_correlation_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "anila_judge_log_correlation_id", default=None
)


@contextmanager
def judge_validation_attempt(
    attempt: int, *, correlation_id: str | None = None
) -> Iterator[None]:
    """Bind attempt / correlation id for invalid-judge warning logs."""

    token_attempt = _judge_log_attempt.set(attempt)
    token_corr = _judge_log_correlation_id.set(correlation_id)
    try:
        yield
    finally:
        _judge_log_attempt.reset(token_attempt)
        _judge_log_correlation_id.reset(token_corr)


def _safe_message_for_invalid(reason: str, *, action: str) -> str:
    if reason in _JUDGE_OUTPUT_INVALID_REASONS:
        return _SAFE_MESSAGE_JUDGE_INVALID
    if action == "deny" or reason in _SECURITY_DENY_REASONS:
        return _SAFE_MESSAGE_DENY
    return _SAFE_MESSAGE_CLARIFY_FAIL_CLOSED


def _redact_strings(value: object, *, key: str | None = None) -> object:
    """Redact a pre-validation payload down to provably-safe structure.

    Nothing here has passed schema validation, so no value is proven
    server-owned: string values survive ONLY when their key maps to a closed
    set (contracts enums / the schema_version literal) and the value is a
    member. Everything else — ids, reason codes, free text — becomes its
    length. Keys outside the structural whitelist are masked too, because a
    judge can put user content in key names. Numbers/booleans/None stay;
    any other leaf type (bytes, sets, objects) is opaque — its repr/str may
    embed user content, so only type and length survive.
    """

    if isinstance(value, str):
        enum_values = _known_enum_values().get(key or "")
        if enum_values is not None and value in enum_values:
            return value
        return f"<redacted len={len(value)}>"
    if isinstance(value, Mapping):
        redacted: dict[str, object] = {}
        for index, (k, v) in enumerate(value.items()):
            key_name = k if isinstance(k, str) else repr(k)
            if key_name in _SAFE_STRING_KEYS:
                redacted[key_name] = _redact_strings(v, key=key_name)
            else:
                # Index keeps same-length masked keys from colliding and
                # silently dropping entries from the sample.
                masked = f"<redacted-key#{index} len={len(key_name)}>"
                redacted[masked] = _redact_strings(v, key=None)
        return redacted
    if isinstance(value, (list, tuple)):
        return [_redact_strings(v, key=key) for v in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    size = len(value) if isinstance(value, (bytes, bytearray)) else len(repr(value))
    return f"<redacted type={type(value).__name__} len={size}>"


def redact_judge_output_sample(provider_output: object, *, max_chars: int = _LOG_SAMPLE_MAX_CHARS) -> str:
    """Return a redacted sample for warning logs.

    Parsed payloads keep whitelisted key names, closed-enum values and
    scalars; every other key and string value is reduced to its length.
    Unparseable raw text is reduced to length + digest — no verbatim bytes.
    """

    payload: object = provider_output
    if isinstance(provider_output, bytes):
        try:
            payload = provider_output.decode("utf-8", errors="replace")
        except Exception:
            payload = repr(provider_output)
    if isinstance(payload, RouteDecision):
        payload = payload.model_dump(mode="json", exclude_none=False)
    if isinstance(payload, Mapping):
        redacted = _redact_strings(dict(payload))
        try:
            text = json.dumps(redacted, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            # Every leaf is JSON-safe after redaction, so this should be
            # unreachable — but never fall back to str()/repr(), which could
            # resurrect content the redaction just removed.
            return _fingerprint_raw_sample(repr(redacted))
        if len(text) > max_chars:
            return text[:max_chars] + f"...<truncated total={len(text)}>"
        return text
    if isinstance(payload, str):
        try:
            parsed = json.loads(payload)
        except (TypeError, json.JSONDecodeError):
            return _fingerprint_raw_sample(payload)
        if isinstance(parsed, Mapping):
            return redact_judge_output_sample(parsed, max_chars=max_chars)
        return _fingerprint_raw_sample(payload)
    return _fingerprint_raw_sample(repr(payload))


def _fingerprint_raw_sample(text: str) -> str:
    """Unparseable output: length + digest only.

    No verbatim prefix — a malformed judge reply may open with (or consist
    entirely of) echoed user content, and short replies would otherwise be
    logged whole. Correlate via the digest when a raw sample must be pulled
    from the upstream provider during an investigation.
    """

    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:12]
    return f"<raw len={len(text)} sha256={digest}>"


def log_invalid_judge_output(
    provider_output: object,
    reason: str,
    *,
    attempt: int | None = None,
    correlation_id: str | None = None,
) -> None:
    """Emit a redacted warning for a failed judge validation attempt."""

    attempt_no = _judge_log_attempt.get() if attempt is None else attempt
    corr = _judge_log_correlation_id.get() if correlation_id is None else correlation_id
    sample = redact_judge_output_sample(provider_output)
    logger.warning(
        "judge output validation failed reason=%s attempt=%s correlation_id=%s sample=%s",
        reason,
        attempt_no,
        corr or "-",
        sample,
    )


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
            safe_message=_safe_message_for_invalid(reason, action=action),
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
            safe_message=_SAFE_MESSAGE_DENY,
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
                safe_message=_SAFE_MESSAGE_DENY,
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

        def _reject(reason: str, *, action: str = "clarify") -> DecisionResult:
            result = self._invalid(reason, action=action)
            log_invalid_judge_output(provider_output, reason)
            return result

        try:
            decoded = self._decode(provider_output)
        except ValueError as exc:
            return _reject(str(exc))

        if _contains_legacy_dispatch(decoded):
            return _reject("LEGACY_DISPATCH_UNSUPPORTED")
        if _contains_injection(decoded):
            return _reject("PROMPT_INJECTION_OUTPUT", action="deny")
        try:
            decision = RouteDecision.model_validate(decoded)
        except Exception:
            return _reject("INVALID_ROUTE_DECISION")

        enforced = self._enforce_fail_closed(
            decision,
            context,
            snapshot,
            request_content,
        )
        if enforced is not None:
            return enforced

        if decision.route_type is RouteType.SINGLE_AGENT and snapshot is None:
            return _reject("SNAPSHOT_MISSING")
        if snapshot is not None:
            if not snapshot.is_fresh(now=now):
                return _reject("SNAPSHOT_STALE")
            if decision.registry_snapshot_id != snapshot.snapshot_id:
                return _reject("SNAPSHOT_MISMATCH")
        elif isinstance(candidates, CandidateFilterResult) and candidates.snapshot_id is None:
            return _reject("SNAPSHOT_MISSING")

        if decision.route_type is RouteType.MULTI_AGENT_PLAN:
            # R3 does not activate plans.  Leaving this as an opaque contract
            # field is useful for version negotiation, but execution is denied.
            return _reject("MULTI_AGENT_UNSUPPORTED", action="deny")

        filtered = _extract_candidates(candidates)
        candidate_map = {entry.agent_id: entry for entry in filtered}
        unknown_candidates = [
            agent_id for agent_id in decision.candidate_agent_ids if agent_id not in candidate_map
        ]
        if unknown_candidates:
            return _reject("UNKNOWN_AGENT")

        if decision.route_type is RouteType.SINGLE_AGENT:
            if not filtered:
                return _reject("NO_ELIGIBLE_CANDIDATES")
            selected = decision.selected_agent_id
            if selected is None or selected not in candidate_map:
                return _reject("UNKNOWN_AGENT")
            if context is not None and not set(context.required_capabilities).issubset(
                set(decision.required_capabilities)
            ):
                return _reject("CAPABILITY_MISMATCH")
            if decision.confidence < self.min_confidence:
                return _reject("LOW_CONFIDENCE")

            if context is not None:
                # Provider limits are untrusted.  Clamp them to server
                # ceilings before the policy gate sees the route.
                max_steps = min(decision.constraints.max_steps, context.max_steps)
                timeout_ms = min(decision.constraints.timeout_ms, context.timeout_ms)
                if max_steps < 1 or timeout_ms < 1:
                    return _reject("CONSTRAINT_EXCEEDS_SERVER_CEILING")
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
            return _reject("LOW_CONFIDENCE")
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


__all__ = [
    "DecisionEngine",
    "DecisionResult",
    "RETRYABLE_JUDGE_REASON_CODES",
    "available_registry_entries",
    "judge_validation_attempt",
    "log_invalid_judge_output",
    "redact_judge_output_sample",
]
