"""CSP-owned Agent dispatch readiness authority.

An Agent can only describe its capabilities.  Approval, health freshness,
trace-test evidence, model state, classification and endpoint safety are all
control-plane facts computed here.  Keep this module dependency-light so the
same predicate can be used by the internal registry projection and the final
proxy sink.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, cast

from anila_contracts import AgentManifest, Classification
from anila_security import UnsafeEndpointError, validate_outbound_url
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.config import settings
from app.models.model_registry import ModelRegistry


# Stable, non-sensitive diagnostics returned in the internal registry view.
NOT_APPROVED = "NOT_APPROVED"
AGENT_INACTIVE = "AGENT_INACTIVE"
HEALTH_UNKNOWN = "HEALTH_UNKNOWN"
HEALTH_UNHEALTHY = "HEALTH_UNHEALTHY"
HEALTH_STALE = "HEALTH_STALE"
MANIFEST_MISSING = "MANIFEST_MISSING"
MANIFEST_INVALID = "MANIFEST_INVALID"
MANIFEST_HASH_MISSING = "MANIFEST_HASH_MISSING"
MANIFEST_HASH_MISMATCH = "MANIFEST_HASH_MISMATCH"
MANIFEST_REVISION_MISSING = "MANIFEST_REVISION_MISSING"
MANIFEST_REVISION_MISMATCH = "MANIFEST_REVISION_MISMATCH"
MANIFEST_AGENT_ID_MISMATCH = "MANIFEST_AGENT_ID_MISMATCH"
MANIFEST_RUNTIME_TYPE_MISMATCH = "MANIFEST_RUNTIME_TYPE_MISMATCH"
MANIFEST_API_VERSION_MISMATCH = "MANIFEST_API_VERSION_MISMATCH"
MANIFEST_AGENT_VERSION_MISMATCH = "MANIFEST_AGENT_VERSION_MISMATCH"
AUDIT_LEVEL_NOT_FULL_TRACE = "AUDIT_LEVEL_NOT_FULL_TRACE"
TRACE_CALLBACK_POSTURE_INVALID = "TRACE_CALLBACK_POSTURE_INVALID"
TRACE_PROTOCOL_INVALID = "TRACE_PROTOCOL_INVALID"
TRACE_TEST_MISSING = "TRACE_TEST_MISSING"
TRACE_TEST_STALE = "TRACE_TEST_STALE"
TRACE_TEST_FINGERPRINT_MISSING = "TRACE_TEST_FINGERPRINT_MISSING"
TRACE_TEST_FINGERPRINT_MISMATCH = "TRACE_TEST_FINGERPRINT_MISMATCH"
CLASSIFICATION_CEILING_INVALID = "CLASSIFICATION_CEILING_INVALID"
CLASSIFICATION_CEILING_MISSING = "CLASSIFICATION_CEILING_MISSING"
CLASSIFICATION_DEFAULT_INVALID = "CLASSIFICATION_DEFAULT_INVALID"
CLASSIFICATION_DEFAULT_MISMATCH = "CLASSIFICATION_DEFAULT_MISMATCH"
CLASSIFICATION_DEFAULT_EXCEEDS_CEILING = "CLASSIFICATION_DEFAULT_EXCEEDS_CEILING"
REQUEST_CLASSIFICATION_EXCEEDS_CEILING = "REQUEST_CLASSIFICATION_EXCEEDS_CEILING"
BASE_MODEL_MISSING = "BASE_MODEL_MISSING"
BASE_MODEL_INACTIVE = "BASE_MODEL_INACTIVE"
BASE_MODEL_HEALTH_UNKNOWN = "BASE_MODEL_HEALTH_UNKNOWN"
BASE_MODEL_HEALTH_UNHEALTHY = "BASE_MODEL_HEALTH_UNHEALTHY"
BASE_MODEL_HEALTH_STALE = "BASE_MODEL_HEALTH_STALE"
BASE_MODEL_CEILING_INVALID = "BASE_MODEL_CEILING_INVALID"
MODEL_BINDING_INVALID = "MODEL_BINDING_INVALID"
ENDPOINT_SSRF_DENIED = "ENDPOINT_SSRF_DENIED"
SNAPSHOT_REVISION_MISSING = "SNAPSHOT_REVISION_MISSING"
SNAPSHOT_REVISION_MISMATCH = "SNAPSHOT_REVISION_MISMATCH"
LEGACY_DISPATCH_DISABLED = "LEGACY_DISPATCH_DISABLED"


def _utc(value: datetime | None) -> datetime | None:
    if value is None or not isinstance(value, datetime):
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _now(value: datetime | None = None) -> datetime:
    return _utc(value) or datetime.now(timezone.utc)


def _fresh(value: datetime | None, *, ttl_seconds: int, now: datetime) -> bool:
    stamp = _utc(value)
    if stamp is None:
        return False
    # Future timestamps are accepted within a small clock-skew window.  A
    # wildly future value is not freshness evidence and is rejected.
    if stamp > now + timedelta(minutes=5):
        return False
    return now - stamp <= timedelta(seconds=ttl_seconds)


def canonical_manifest_json(manifest: AgentManifest) -> dict[str, Any]:
    """Return the canonical JSON-mode representation used for hashing."""

    return cast(dict[str, Any], manifest.model_dump(mode="json"))


def manifest_sha256(manifest: AgentManifest | Mapping[str, Any]) -> str:
    """Hash a validated manifest with deterministic UTF-8 JSON."""

    payload = (
        canonical_manifest_json(manifest)
        if isinstance(manifest, AgentManifest)
        else dict(manifest)
    )
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def manifest_revision(manifest: AgentManifest | Mapping[str, Any]) -> str:
    """Return the CSP content revision, independent of semantic ``version``.

    A manifest may keep the same semantic version while changing a capability,
    scope, model binding or schema.  The revision therefore binds the complete
    canonical JSON payload and uses the shared identifier alphabet's ``sha256:``
    prefix rather than trusting the Agent-authored version string.
    """

    return f"sha256:{manifest_sha256(manifest)}"


def _base_model(agent: Any, db: Session | None) -> ModelRegistry | None:
    model_id = getattr(agent, "base_model_id", None)
    if model_id is None:
        return None
    if db is not None and model_id is not None:
        # The row is re-read from the governance session even when the Agent
        # relationship was eager-loaded by the registry projection.  The
        # final sink holds this same session's row lock, so using a stale
        # relationship here would reopen a model health/ceiling TOCTOU gap.
        try:
            model = (
                db.query(ModelRegistry)
                .filter(ModelRegistry.id == model_id)
                .populate_existing()
                .one_or_none()
            )
        except Exception:
            # Readiness remains fail-closed if a non-standard test/session
            # object cannot issue the refresh query.
            model = None
        if model is not None:
            return model
    # A detached object (e.g. a focused unit test) may not have a governance
    # session; only then is its relationship usable as a best-effort read.
    return getattr(agent, "base_model", None) if db is None else None


def governance_fingerprint(agent: Any, *, base_model: ModelRegistry | None = None) -> str:
    """Fingerprint all mutable facts that trace-test evidence depends on."""

    model = base_model or getattr(agent, "base_model", None)
    runtime_config = getattr(agent, "runtime_config", None)
    if runtime_config is None:
        runtime_config_hash = None
    else:
        runtime_config_hash = hashlib.sha256(
            json.dumps(
                runtime_config,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    material = {
        "manifest_sha256": getattr(agent, "manifest_sha256", None),
        "manifest_revision": getattr(agent, "manifest_revision", None),
        "is_active": getattr(agent, "is_active", None),
        "endpoint_url": getattr(agent, "endpoint_url", None),
        "api_version": getattr(agent, "api_version", None),
        "base_model_id": getattr(agent, "base_model_id", None),
        "bound_collection_id": getattr(agent, "bound_collection_id", None),
        "runtime_type": getattr(agent, "runtime_type", None),
        "agent_version": getattr(agent, "agent_version", None),
        "classification_ceiling": getattr(agent, "classification_ceiling", None),
        "default_classification_level": getattr(
            agent, "default_classification_level", None
        ),
        "audit_level": getattr(agent, "audit_level", None),
        "trace_callback_mode": getattr(agent, "trace_callback_mode", None),
        "requires_encryption": getattr(agent, "requires_encryption", None),
        "runtime_config_sha256": runtime_config_hash,
        # Model health timestamps/status are evaluated independently by the
        # readiness predicate.  Probe UPDATEs must not invalidate a successful
        # trace-test; durable model governance fields remain bound here.
        "base_model_id_from_row": getattr(model, "id", None),
        "base_model_name": getattr(model, "name", None),
        "base_model_endpoint_url": getattr(model, "endpoint_url", None),
        "base_model_api_version": getattr(model, "api_version", None),
        "base_model_protocol": getattr(model, "protocol", None),
        "base_model_is_active": getattr(model, "is_active", None),
        "base_model_classification_ceiling": getattr(
            model, "classification_ceiling", None
        ),
        "base_model_supports_streaming": getattr(model, "supports_streaming", None),
        "base_model_supports_json_schema": getattr(
            model, "supports_json_schema", None
        ),
        "base_model_supports_tools": getattr(model, "supports_tools", None),
        "base_model_is_internal": getattr(model, "is_internal", None),
    }
    # Keep this deterministic for ORM values and enum-like test doubles while
    # never serialising arbitrary object reprs that could contain addresses or
    # secrets.  The model fields above are scalar DB values in production.
    for key, value in tuple(material.items()):
        if isinstance(value, datetime):
            material[key] = value.isoformat()
        elif value is not None:
            enum_value = getattr(value, "value", None)
            if isinstance(enum_value, (str, int, float, bool)):
                material[key] = enum_value
            elif not isinstance(value, (str, int, float, bool)):
                material[key] = str(value)
    raw = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def invalidate_trace_evidence(agent: Any, *, reset_approval: bool = True) -> None:
    """Clear evidence whenever a dispatch-governing field changes."""

    agent.trace_test_passed_at = None
    agent.trace_test_report = None
    agent.trace_test_governance_fingerprint = None
    if reset_approval and getattr(agent, "approval_status", None) == "approved":
        agent.approval_status = "pending_connection_test"
        agent.approved_by = None
        agent.approved_at = None


@dataclass(frozen=True)
class AgentReadiness:
    """Immutable decision plus safe reason codes for one Agent."""

    ready_for_dispatch: bool
    reason_codes: tuple[str, ...]
    manifest: AgentManifest | None = None
    manifest_json: dict[str, Any] | None = None
    computed_manifest_sha256: str | None = None
    computed_governance_fingerprint: str | None = None

    @property
    def reasons(self) -> list[str]:
        return list(self.reason_codes)


def _append(reasons: list[str], code: str) -> None:
    if code not in reasons:
        reasons.append(code)


def evaluate_agent_readiness(
    agent: Any,
    *,
    db: Session | None = None,
    requested_classification: str | Classification | None = None,
    expected_manifest_revision: str | None = None,
    expected_manifest_sha256: str | None = None,
    now: datetime | None = None,
) -> AgentReadiness:
    """Evaluate every CSP dispatch predicate without making network calls.

    ``now`` is injectable for deterministic tests; production callers leave it
    unset and the function uses UTC wall clock only for freshness comparisons.
    """

    current = _now(now)
    reasons: list[str] = []

    if not bool(getattr(agent, "is_active", False)) or getattr(
        agent, "approval_status", None
    ) == "disabled":
        _append(reasons, AGENT_INACTIVE)
    if getattr(agent, "approval_status", None) != "approved":
        _append(reasons, NOT_APPROVED)

    health = str(getattr(agent, "health_status", "") or "").lower()
    if health != "healthy":
        _append(reasons, HEALTH_UNKNOWN if health in {"", "unknown"} else HEALTH_UNHEALTHY)
    if not _fresh(
        getattr(agent, "health_checked_at", None),
        ttl_seconds=settings.AGENT_HEALTH_FRESHNESS_SECONDS,
        now=current,
    ):
        _append(reasons, HEALTH_STALE)

    raw_manifest = getattr(agent, "manifest_json", None)
    parsed: AgentManifest | None = None
    normalized_manifest: dict[str, Any] | None = None
    computed_hash: str | None = None
    if not isinstance(raw_manifest, Mapping):
        _append(reasons, MANIFEST_MISSING)
    else:
        try:
            parsed = AgentManifest.model_validate(dict(raw_manifest))
            normalized_manifest = canonical_manifest_json(parsed)
            computed_hash = manifest_sha256(parsed)
        except (ValidationError, TypeError, ValueError):
            _append(reasons, MANIFEST_INVALID)

    if parsed is not None:
        identity = str(getattr(agent, "id", ""))
        if parsed.agent_id not in {identity, str(getattr(agent, "name", ""))}:
            _append(reasons, MANIFEST_AGENT_ID_MISMATCH)
        manifest_runtime_type = getattr(parsed.runtime_type, "value", parsed.runtime_type)
        if manifest_runtime_type != getattr(agent, "runtime_type", None):
            _append(reasons, MANIFEST_RUNTIME_TYPE_MISMATCH)
        if parsed.api_version != getattr(agent, "api_version", None):
            _append(reasons, MANIFEST_API_VERSION_MISMATCH)
        if parsed.version != getattr(agent, "agent_version", None):
            _append(reasons, MANIFEST_AGENT_VERSION_MISMATCH)
        stored_hash = getattr(agent, "manifest_sha256", None)
        if not stored_hash:
            _append(reasons, MANIFEST_HASH_MISSING)
        elif stored_hash != computed_hash:
            _append(reasons, MANIFEST_HASH_MISMATCH)
        stored_revision = getattr(agent, "manifest_revision", None)
        if not stored_revision:
            _append(reasons, MANIFEST_REVISION_MISSING)
        elif stored_revision != manifest_revision(parsed):
            _append(reasons, MANIFEST_REVISION_MISMATCH)
        if expected_manifest_revision is not None and stored_revision != expected_manifest_revision:
            _append(reasons, MANIFEST_REVISION_MISMATCH)
        if expected_manifest_sha256 is not None and stored_hash != expected_manifest_sha256:
            _append(reasons, MANIFEST_HASH_MISMATCH)

        if not parsed.full_trace_required:
            _append(reasons, AUDIT_LEVEL_NOT_FULL_TRACE)
        if "step-event/v1" not in parsed.event_protocols:
            _append(reasons, TRACE_PROTOCOL_INVALID)
        binding = parsed.model_binding
        if binding is None or binding.gateway != "csp":
            _append(reasons, MODEL_BINDING_INVALID)
        elif (
            not isinstance(binding.model_id, int)
            or getattr(agent, "base_model_id", None) != binding.model_id
        ):
            _append(reasons, MODEL_BINDING_INVALID)
        if (
            parsed.base_model_id is not None
            and parsed.base_model_id != getattr(agent, "base_model_id", None)
        ):
            _append(reasons, MODEL_BINDING_INVALID)

    if getattr(agent, "audit_level", None) != "full_trace":
        _append(reasons, AUDIT_LEVEL_NOT_FULL_TRACE)
    if getattr(agent, "trace_callback_mode", None) not in {"sse_and_post", "sse", "post"}:
        _append(reasons, TRACE_CALLBACK_POSTURE_INVALID)

    evidence_time = getattr(agent, "trace_test_passed_at", None)
    report = getattr(agent, "trace_test_report", None)
    if evidence_time is None or not isinstance(report, Mapping) or report.get("passed") is not True:
        _append(reasons, TRACE_TEST_MISSING)
    else:
        if not _fresh(
            evidence_time,
            ttl_seconds=settings.AGENT_TRACE_TEST_FRESHNESS_SECONDS,
            now=current,
        ):
            _append(reasons, TRACE_TEST_STALE)
        checked_at = report.get("checked_at")
        if isinstance(checked_at, str):
            try:
                checked_at = datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
            except ValueError:
                checked_at = None
        if not _fresh(
            checked_at,
            ttl_seconds=settings.AGENT_TRACE_TEST_FRESHNESS_SECONDS,
            now=current,
        ):
            _append(reasons, TRACE_TEST_STALE)

    model = _base_model(agent, db)
    if model is None:
        _append(reasons, BASE_MODEL_MISSING)
    else:
        if (
            parsed is not None
            and parsed.model_binding is not None
            and isinstance(parsed.model_binding.model_id, int)
            and getattr(model, "id", None) != parsed.model_binding.model_id
        ):
            _append(reasons, MODEL_BINDING_INVALID)
        if not bool(getattr(model, "is_active", False)):
            _append(reasons, BASE_MODEL_INACTIVE)
        model_health = str(getattr(model, "health_status", "") or "").lower()
        if model_health != "healthy":
            _append(
                reasons,
                BASE_MODEL_HEALTH_UNKNOWN
                if model_health in {"", "unknown"}
                else BASE_MODEL_HEALTH_UNHEALTHY,
            )
        if not _fresh(
            getattr(model, "health_checked_at", None),
            ttl_seconds=settings.MODEL_HEALTH_FRESHNESS_SECONDS,
            now=current,
        ):
            _append(reasons, BASE_MODEL_HEALTH_STALE)

    raw_ceiling = getattr(agent, "classification_ceiling", None)
    if raw_ceiling is None or (
        isinstance(raw_ceiling, str) and not raw_ceiling.strip()
    ):
        ceiling = None
        _append(reasons, CLASSIFICATION_CEILING_MISSING)
    else:
        try:
            ceiling = Classification.from_storage(str(raw_ceiling))
        except (TypeError, ValueError):
            ceiling = None
            _append(reasons, CLASSIFICATION_CEILING_INVALID)

    manifest_default = manifest_ceiling = None
    if parsed is not None:
        try:
            manifest_default = parsed.classification.default
            manifest_ceiling = parsed.classification.ceiling
        except (TypeError, ValueError):
            _append(reasons, CLASSIFICATION_DEFAULT_INVALID)

    try:
        stored_default = Classification.from_storage(
            str(getattr(agent, "default_classification_level", ""))
        )
    except (TypeError, ValueError):
        stored_default = None
        _append(reasons, CLASSIFICATION_DEFAULT_INVALID)

    if (
        manifest_default is not None
        and stored_default is not None
        and manifest_default != stored_default
    ):
        _append(reasons, CLASSIFICATION_DEFAULT_MISMATCH)
    effective_default = stored_default or manifest_default
    ceilings = [item for item in (ceiling, manifest_ceiling) if item is not None]
    if model is not None:
        try:
            ceilings.append(
                Classification.from_storage(str(getattr(model, "classification_ceiling", "")))
            )
        except (TypeError, ValueError):
            _append(reasons, BASE_MODEL_CEILING_INVALID)
    if effective_default is not None and ceilings and any(
        effective_default > item for item in ceilings
    ):
        _append(reasons, CLASSIFICATION_DEFAULT_EXCEEDS_CEILING)
    if requested_classification is not None:
        try:
            requested = (
                requested_classification
                if isinstance(requested_classification, Classification)
                else Classification.from_storage(str(requested_classification))
            )
            if ceilings and any(requested > item for item in ceilings):
                _append(reasons, REQUEST_CLASSIFICATION_EXCEEDS_CEILING)
        except (TypeError, ValueError):
            _append(reasons, REQUEST_CLASSIFICATION_EXCEEDS_CEILING)
    elif effective_default is None:
        _append(reasons, CLASSIFICATION_DEFAULT_INVALID)

    try:
        validate_outbound_url(str(getattr(agent, "endpoint_url", "")), endpoint_kind="agent")
    except (UnsafeEndpointError, TypeError, ValueError):
        _append(reasons, ENDPOINT_SSRF_DENIED)
    except Exception:
        # A guard implementation failure is a denial, never an implicit allow.
        _append(reasons, ENDPOINT_SSRF_DENIED)

    fingerprint = governance_fingerprint(agent, base_model=model)
    if not getattr(agent, "trace_test_governance_fingerprint", None):
        _append(reasons, TRACE_TEST_FINGERPRINT_MISSING)
    elif agent.trace_test_governance_fingerprint != fingerprint:
        _append(reasons, TRACE_TEST_FINGERPRINT_MISMATCH)
    if isinstance(report, Mapping):
        report_fingerprint = report.get("governance_fingerprint")
        if report_fingerprint != fingerprint:
            _append(reasons, TRACE_TEST_FINGERPRINT_MISMATCH)

    return AgentReadiness(
        ready_for_dispatch=not reasons,
        reason_codes=tuple(reasons),
        manifest=parsed,
        manifest_json=normalized_manifest,
        computed_manifest_sha256=computed_hash,
        computed_governance_fingerprint=fingerprint,
    )


__all__ = [
    "AgentReadiness",
    "AGENT_INACTIVE",
    "canonical_manifest_json",
    "evaluate_agent_readiness",
    "governance_fingerprint",
    "invalidate_trace_evidence",
    "manifest_revision",
    "manifest_sha256",
    "MANIFEST_AGENT_VERSION_MISMATCH",
    "MANIFEST_API_VERSION_MISMATCH",
    "MANIFEST_RUNTIME_TYPE_MISMATCH",
]
