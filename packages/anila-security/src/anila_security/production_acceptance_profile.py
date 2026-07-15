"""Gate 6 production-acceptance (P0) profile contract.

The profile is deliberately a small, dependency-light authority boundary.  It
does not decide production values for an operator; it only makes every value
explicit, bounds values that have a meaningful safety boundary, and refuses to
construct an authority unless the complete five-party signature set verifies.

``production-acceptance-profile.disabled-template.json`` is an evidence-free
authoring aid.  It is intentionally not an alternate profile mode: the
runtime verifier rejects it by default.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from anila_security.model_governance import CLASSIFICATION_LEVELS, classification_rank


PRODUCTION_ACCEPTANCE_SCHEMA = "anila.gate6.production-acceptance.v1"
REQUIRED_PRODUCTION_SIGNER_ROLES = frozenset(
    {"system_owner", "data_owner", "pki_owner", "security", "operations"}
)
PRODUCTION_SIGNER_ROLES = REQUIRED_PRODUCTION_SIGNER_ROLES

_PROFILE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_MAX_PROFILE_DAYS = 365
_MAX_OBSERVATION_DAYS = 365
_MAX_DURATION_SECONDS = 365 * 24 * 60 * 60
_MIN_OBSERVATION_SECONDS = 7 * 24 * 60 * 60
_MAX_RTO_RPO_SECONDS = 365 * 24 * 60 * 60
_MAX_REVOCATION_SECONDS = 7 * 24 * 60 * 60
_MAX_P5_SAMPLE_N = 100_000_000

_PROFILE_FIELDS = frozenset(
    {
        "schema_version",
        "profile_id",
        "profile_version",
        "enabled",
        "template_only",
        "approval_status",
        "production_topology",
        "enabled_features",
        "disabled_features",
        "data_classification_ceiling",
        "rto_rpo",
        "slo_thresholds",
        "load_profile",
        "observation_window",
        "workflow_matrix",
        "p5_sample_n",
        "enabled_inference_callsite_inventory",
        "revocation_sla",
        "pki_policy",
        "severity_taxonomy",
        "finding_acceptance_rule",
        "revalidation_impact_matrix",
        "valid_from",
        "valid_until",
        "signer_roles",
        "profile_content_sha256",
        "signatures",
    }
)
_TOPOLOGY_FIELDS = frozenset(
    {
        "topology_id",
        "environment",
        "regions",
        "network_zones",
        "services",
        "egress_policy",
        "artifact_hashes",
    }
)
_RTO_RPO_FIELDS = frozenset({"rto_seconds", "rpo_seconds"})
_SLO_FIELDS = frozenset(
    {
        "ingestion_p99_ms",
        "dispatch_success_rate",
        "queue_age_seconds",
        "stuck_job_count",
        "artifact_download_success_rate",
        "auth_error_rate",
    }
)
_LOAD_FIELDS = frozenset(
    {"profile_id", "concurrency", "requests_per_second", "duration_seconds", "workflow_ids"}
)
_OBSERVATION_FIELDS = frozenset({"start", "end", "minimum_duration_seconds"})
_CALLSITE_INVENTORY_FIELDS = frozenset(
    {"schema_version", "version", "sha256", "callsite_ids"}
)
_REVOCATION_FIELDS = frozenset({"token_seconds", "card_seconds"})
_PKI_FIELDS = frozenset(
    {"stale_after_seconds", "offline_behavior", "missing_behavior", "refresh_failure_behavior"}
)
_SEVERITY_FIELDS = frozenset({"sev1", "sev2"})
_SEVERITY_ENTRY_FIELDS = frozenset(
    {"definition", "ack_seconds", "mitigate_seconds"}
)
_FINDING_FIELDS = frozenset(
    {
        "critical",
        "high",
        "medium",
        "low",
        "blocked_categories",
        "conditional_requirements",
    }
)
_IMPACT_FIELDS = frozenset({"version", "sha256"})


class ProductionAcceptanceProfileError(ValueError):
    """Raised when a Gate 6 P0 profile is incomplete or unsafe."""


@dataclass(frozen=True, slots=True)
class VerifiedProductionAcceptanceProfile:
    """Immutable result of verifying an enabled production profile."""

    profile_id: str
    profile_version: str
    profile_content_sha256: str
    inventory_version: str
    inventory_sha256: str
    data_classification_ceiling: str
    signer_roles: tuple[str, ...]
    valid_from: datetime
    valid_until: datetime
    observation_start: datetime
    observation_end: datetime
    profile: Mapping[str, Any]

    @property
    def enabled(self) -> bool:
        """A verified result is always enabled (disabled templates are rejected)."""

        return True


def canonical_json(value: Any) -> bytes:
    """Return the one canonical JSON byte representation used for hashes/signatures."""

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ProductionAcceptanceProfileError("value is not canonical JSON") from exc


def sha256_hex(value: Any) -> str:
    """Hash a JSON value using :func:`canonical_json`."""

    return hashlib.sha256(canonical_json(value)).hexdigest()


def production_profile_content_sha256(profile: Mapping[str, Any]) -> str:
    """Hash profile content while excluding the self-referential hash/signatures."""

    if not isinstance(profile, Mapping):
        raise ProductionAcceptanceProfileError("profile must be an object")
    unsigned = dict(profile)
    unsigned.pop("profile_content_sha256", None)
    unsigned.pop("signatures", None)
    return hashlib.sha256(canonical_json(unsigned)).hexdigest()


# A descriptive alias makes call sites explicit without breaking the common
# ``profile_content_sha256`` terminology used by the Gate 5 contract.
profile_content_sha256 = production_profile_content_sha256


def _read_json(path: str | Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProductionAcceptanceProfileError(f"cannot read {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise ProductionAcceptanceProfileError(f"{label} root must be an object")
    return value


def _require_string(value: Any, field: str, *, max_length: int = 512) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > max_length
    ):
        raise ProductionAcceptanceProfileError(f"{field} must be a non-empty trimmed string")
    return value


def _require_identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
        raise ProductionAcceptanceProfileError(f"{field} must be an identifier")
    return value


def _require_profile_id(value: Any) -> str:
    if not isinstance(value, str) or not _PROFILE_ID_RE.fullmatch(value):
        raise ProductionAcceptanceProfileError("profile_id must be an identifier")
    return value


def _require_hash(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ProductionAcceptanceProfileError(f"{field} must be 64 lowercase hex characters")
    return value


def _require_exact_object(value: Any, expected: frozenset[str], field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ProductionAcceptanceProfileError(f"{field} has unknown or missing fields")
    return value


def _require_unique_strings(
    value: Any,
    field: str,
    *,
    allow_empty: bool = False,
    max_length: int = 256,
) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ProductionAcceptanceProfileError(f"{field} must be a list")
    if not allow_empty and not value:
        raise ProductionAcceptanceProfileError(f"{field} must not be empty")
    result = tuple(_require_string(item, f"{field}[]", max_length=max_length) for item in value)
    if len(result) != len(set(result)):
        raise ProductionAcceptanceProfileError(f"{field} contains duplicates")
    return result


def _require_bounded_int(
    value: Any, field: str, *, minimum: int, maximum: int
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProductionAcceptanceProfileError(f"{field} must be an integer")
    if not minimum <= value <= maximum:
        raise ProductionAcceptanceProfileError(f"{field} outside allowed range")
    return value


def _require_bounded_number(
    value: Any, field: str, *, minimum: float, maximum: float, minimum_inclusive: bool = True
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProductionAcceptanceProfileError(f"{field} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise ProductionAcceptanceProfileError(f"{field} must be finite")
    if minimum_inclusive:
        valid = minimum <= number <= maximum
    else:
        valid = minimum < number <= maximum
    if not valid:
        raise ProductionAcceptanceProfileError(f"{field} outside allowed range")
    return number


def _parse_rfc3339(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ProductionAcceptanceProfileError(f"{field} must be RFC3339 with timezone")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProductionAcceptanceProfileError(f"{field} must be RFC3339 with timezone") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProductionAcceptanceProfileError(f"{field} must include timezone")
    return parsed.astimezone(timezone.utc)


def _normalise_now(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ProductionAcceptanceProfileError("now must include timezone")
    return current.astimezone(timezone.utc)


def _validate_profile_window(profile: Mapping[str, Any], *, now: datetime) -> tuple[datetime, datetime]:
    valid_from = _parse_rfc3339(profile["valid_from"], "valid_from")
    valid_until = _parse_rfc3339(profile["valid_until"], "valid_until")
    if valid_until <= valid_from:
        raise ProductionAcceptanceProfileError("valid_until must be after valid_from")
    if (valid_until - valid_from).total_seconds() > _MAX_PROFILE_DAYS * 86400:
        raise ProductionAcceptanceProfileError("profile validity interval is too long")
    if not valid_from <= now < valid_until:
        raise ProductionAcceptanceProfileError("production profile is not currently effective")
    return valid_from, valid_until


def _validate_topology(value: Any) -> None:
    topology = _require_exact_object(value, _TOPOLOGY_FIELDS, "production_topology")
    _require_identifier(topology["topology_id"], "production_topology.topology_id")
    _require_identifier(topology["environment"], "production_topology.environment")
    _require_unique_strings(topology["regions"], "production_topology.regions")
    _require_unique_strings(topology["network_zones"], "production_topology.network_zones")
    _require_unique_strings(topology["services"], "production_topology.services")
    if topology["egress_policy"] != "csp_only":
        raise ProductionAcceptanceProfileError(
            "production_topology.egress_policy must be csp_only"
        )
    artifact_hashes = topology["artifact_hashes"]
    if not isinstance(artifact_hashes, Mapping) or not artifact_hashes:
        raise ProductionAcceptanceProfileError(
            "production_topology.artifact_hashes must be a non-empty object"
        )
    for name, digest in artifact_hashes.items():
        _require_identifier(name, "production_topology.artifact_hashes key")
        _require_hash(digest, f"production_topology.artifact_hashes[{name!r}]")


def _validate_features(profile: Mapping[str, Any]) -> None:
    enabled = _require_unique_strings(profile["enabled_features"], "enabled_features")
    disabled = _require_unique_strings(
        profile["disabled_features"], "disabled_features", allow_empty=True
    )
    if set(enabled) & set(disabled):
        raise ProductionAcceptanceProfileError("enabled_features and disabled_features overlap")


def _validate_rto_rpo(value: Any) -> None:
    values = _require_exact_object(value, _RTO_RPO_FIELDS, "rto_rpo")
    _require_bounded_int(
        values["rto_seconds"], "rto_rpo.rto_seconds", minimum=1, maximum=_MAX_RTO_RPO_SECONDS
    )
    _require_bounded_int(
        values["rpo_seconds"], "rto_rpo.rpo_seconds", minimum=1, maximum=_MAX_RTO_RPO_SECONDS
    )


def _validate_slos(value: Any) -> None:
    values = _require_exact_object(value, _SLO_FIELDS, "slo_thresholds")
    _require_bounded_int(values["ingestion_p99_ms"], "slo_thresholds.ingestion_p99_ms", minimum=1, maximum=86_400_000)
    _require_bounded_number(
        values["dispatch_success_rate"],
        "slo_thresholds.dispatch_success_rate",
        minimum=0,
        maximum=1,
        minimum_inclusive=False,
    )
    _require_bounded_int(values["queue_age_seconds"], "slo_thresholds.queue_age_seconds", minimum=0, maximum=_MAX_DURATION_SECONDS)
    _require_bounded_int(values["stuck_job_count"], "slo_thresholds.stuck_job_count", minimum=0, maximum=_MAX_P5_SAMPLE_N)
    _require_bounded_number(
        values["artifact_download_success_rate"],
        "slo_thresholds.artifact_download_success_rate",
        minimum=0,
        maximum=1,
        minimum_inclusive=False,
    )
    _require_bounded_number(
        values["auth_error_rate"],
        "slo_thresholds.auth_error_rate",
        minimum=0,
        maximum=1,
    )


def _validate_load_profile(value: Any) -> None:
    values = _require_exact_object(value, _LOAD_FIELDS, "load_profile")
    _require_identifier(values["profile_id"], "load_profile.profile_id")
    _require_bounded_int(values["concurrency"], "load_profile.concurrency", minimum=1, maximum=1_000_000)
    _require_bounded_number(
        values["requests_per_second"],
        "load_profile.requests_per_second",
        minimum=0,
        maximum=1_000_000,
        minimum_inclusive=False,
    )
    _require_bounded_int(values["duration_seconds"], "load_profile.duration_seconds", minimum=1, maximum=_MAX_DURATION_SECONDS)
    _require_unique_strings(values["workflow_ids"], "load_profile.workflow_ids")


def _validate_observation_window(value: Any) -> tuple[datetime, datetime]:
    values = _require_exact_object(value, _OBSERVATION_FIELDS, "observation_window")
    start = _parse_rfc3339(values["start"], "observation_window.start")
    end = _parse_rfc3339(values["end"], "observation_window.end")
    minimum = _require_bounded_int(
        values["minimum_duration_seconds"],
        "observation_window.minimum_duration_seconds",
        minimum=_MIN_OBSERVATION_SECONDS,
        maximum=_MAX_OBSERVATION_DAYS * 86400,
    )
    elapsed = (end - start).total_seconds()
    if elapsed <= 0:
        raise ProductionAcceptanceProfileError("observation_window.end must be after start")
    if elapsed < _MIN_OBSERVATION_SECONDS:
        raise ProductionAcceptanceProfileError(
            "observation_window must cover at least seven days"
        )
    if elapsed < minimum:
        raise ProductionAcceptanceProfileError(
            "observation_window is shorter than minimum_duration_seconds"
        )
    if elapsed > _MAX_OBSERVATION_DAYS * 86400:
        raise ProductionAcceptanceProfileError("observation_window is too long")
    return start, end


def _validate_workflow_matrix(value: Any, *, ceiling: str) -> set[str]:
    if not isinstance(value, list) or not value:
        raise ProductionAcceptanceProfileError("workflow_matrix must be a non-empty list")
    expected = frozenset(
        {"workflow_id", "auth_method", "classification", "compartment", "positive_fixture", "negative_fixture"}
    )
    seen: set[tuple[str, str, str, str]] = set()
    workflow_ids: set[str] = set()
    for index, row in enumerate(value):
        item = _require_exact_object(row, expected, f"workflow_matrix[{index}]")
        workflow_id = _require_identifier(item["workflow_id"], f"workflow_matrix[{index}].workflow_id")
        auth_method = _require_identifier(item["auth_method"], f"workflow_matrix[{index}].auth_method")
        classification = item["classification"]
        if classification not in CLASSIFICATION_LEVELS:
            raise ProductionAcceptanceProfileError(
                f"workflow_matrix[{index}].classification is invalid"
            )
        if classification_rank(classification) > classification_rank(ceiling):
            raise ProductionAcceptanceProfileError(
                f"workflow_matrix[{index}].classification exceeds data ceiling"
            )
        compartment = _require_identifier(item["compartment"], f"workflow_matrix[{index}].compartment")
        _require_identifier(item["positive_fixture"], f"workflow_matrix[{index}].positive_fixture")
        _require_identifier(item["negative_fixture"], f"workflow_matrix[{index}].negative_fixture")
        key = (workflow_id, auth_method, classification, compartment)
        if key in seen:
            raise ProductionAcceptanceProfileError("workflow_matrix contains duplicate combinations")
        seen.add(key)
        workflow_ids.add(workflow_id)
    return workflow_ids


def _validate_callsite_inventory(
    value: Any,
    *,
    expected_hash: str | None,
    expected_version: str | None,
) -> tuple[str, str]:
    inventory = _require_exact_object(
        value, _CALLSITE_INVENTORY_FIELDS, "enabled_inference_callsite_inventory"
    )
    schema_version = _require_string(
        inventory["schema_version"],
        "enabled_inference_callsite_inventory.schema_version",
        max_length=128,
    )
    version = _require_string(
        inventory["version"], "enabled_inference_callsite_inventory.version", max_length=128
    )
    digest = _require_hash(inventory["sha256"], "enabled_inference_callsite_inventory.sha256")
    if not schema_version.startswith("anila."):
        raise ProductionAcceptanceProfileError(
            "enabled_inference_callsite_inventory.schema_version is not an ANILA schema"
        )
    callsite_ids = _require_unique_strings(
        inventory["callsite_ids"],
        "enabled_inference_callsite_inventory.callsite_ids",
    )
    for callsite_id in callsite_ids:
        _require_identifier(callsite_id, "enabled_inference_callsite_inventory.callsite_ids[]")
    if expected_hash is not None and digest != _require_hash(expected_hash, "expected_inventory_sha256"):
        raise ProductionAcceptanceProfileError("enabled inference callsite inventory hash mismatch")
    if expected_version is not None and version != _require_string(
        expected_version, "expected_inventory_version", max_length=128
    ):
        raise ProductionAcceptanceProfileError("enabled inference callsite inventory version mismatch")
    return version, digest


def _validate_revocation(value: Any) -> None:
    values = _require_exact_object(value, _REVOCATION_FIELDS, "revocation_sla")
    _require_bounded_int(values["token_seconds"], "revocation_sla.token_seconds", minimum=1, maximum=_MAX_REVOCATION_SECONDS)
    _require_bounded_int(values["card_seconds"], "revocation_sla.card_seconds", minimum=1, maximum=_MAX_REVOCATION_SECONDS)


def _validate_pki_policy(value: Any) -> None:
    values = _require_exact_object(value, _PKI_FIELDS, "pki_policy")
    _require_bounded_int(values["stale_after_seconds"], "pki_policy.stale_after_seconds", minimum=1, maximum=_MAX_DURATION_SECONDS)
    for field in ("offline_behavior", "missing_behavior", "refresh_failure_behavior"):
        if values[field] != "fail_closed":
            raise ProductionAcceptanceProfileError(f"pki_policy.{field} must be fail_closed")


def _validate_severity_taxonomy(value: Any) -> None:
    taxonomy = _require_exact_object(value, _SEVERITY_FIELDS, "severity_taxonomy")
    for severity in ("sev1", "sev2"):
        entry = _require_exact_object(
            taxonomy[severity], _SEVERITY_ENTRY_FIELDS, f"severity_taxonomy.{severity}"
        )
        _require_unique_strings(
            entry["definition"],
            f"severity_taxonomy.{severity}.definition",
        )
        _require_bounded_int(
            entry["ack_seconds"],
            f"severity_taxonomy.{severity}.ack_seconds",
            minimum=1,
            maximum=_MAX_DURATION_SECONDS,
        )
        _require_bounded_int(
            entry["mitigate_seconds"],
            f"severity_taxonomy.{severity}.mitigate_seconds",
            minimum=1,
            maximum=_MAX_DURATION_SECONDS,
        )


def _validate_finding_rule(value: Any) -> None:
    rule = _require_exact_object(value, _FINDING_FIELDS, "finding_acceptance_rule")
    allowed = {"reject", "conditional_accept", "accept"}
    for severity in ("critical", "high", "medium", "low"):
        decision = rule[severity]
        if decision not in allowed:
            raise ProductionAcceptanceProfileError(
                f"finding_acceptance_rule.{severity} is invalid"
            )
    if rule["critical"] != "reject" or rule["high"] != "reject":
        raise ProductionAcceptanceProfileError(
            "critical/high findings must be rejected, not risk-accepted"
        )
    if rule["medium"] != "conditional_accept":
        raise ProductionAcceptanceProfileError(
            "medium findings require conditional_accept"
        )
    blocked = set(
        _require_unique_strings(
            rule["blocked_categories"],
            "finding_acceptance_rule.blocked_categories",
        )
    )
    if blocked != {"auth", "classification", "egress"}:
        raise ProductionAcceptanceProfileError(
            "finding_acceptance_rule must block auth, classification, and egress bypasses"
        )
    requirements = set(
        _require_unique_strings(
            rule["conditional_requirements"],
            "finding_acceptance_rule.conditional_requirements",
        )
    )
    if requirements != {"owner", "expiry", "compensating_control", "independent_signoff"}:
        raise ProductionAcceptanceProfileError(
            "conditional finding acceptance must require owner, expiry, "
            "compensating_control, and independent_signoff"
        )


def _validate_impact_matrix(value: Any) -> None:
    matrix = _require_exact_object(value, _IMPACT_FIELDS, "revalidation_impact_matrix")
    _require_string(matrix["version"], "revalidation_impact_matrix.version", max_length=128)
    _require_hash(matrix["sha256"], "revalidation_impact_matrix.sha256")


def _validate_enabled_profile(
    profile: Mapping[str, Any],
    *,
    now: datetime,
    expected_inventory_sha256: str | None,
    expected_inventory_version: str | None,
) -> tuple[str, str, datetime, datetime, datetime, datetime]:
    if profile["enabled"] is not True:
        raise ProductionAcceptanceProfileError("enabled production profile must set enabled=true")
    if profile["template_only"] is not False:
        raise ProductionAcceptanceProfileError("production profile cannot be template_only")
    if profile["approval_status"] != "approved":
        raise ProductionAcceptanceProfileError(
            "enabled production profile must set approval_status=approved"
        )
    _validate_topology(profile["production_topology"])
    _validate_features(profile)
    ceiling = profile["data_classification_ceiling"]
    if ceiling not in CLASSIFICATION_LEVELS:
        raise ProductionAcceptanceProfileError("data_classification_ceiling is invalid")
    _validate_rto_rpo(profile["rto_rpo"])
    _validate_slos(profile["slo_thresholds"])
    _validate_load_profile(profile["load_profile"])
    observation_start, observation_end = _validate_observation_window(profile["observation_window"])
    matrix_workflows = _validate_workflow_matrix(profile["workflow_matrix"], ceiling=ceiling)
    load_workflows = set(profile["load_profile"]["workflow_ids"])
    if not load_workflows <= matrix_workflows:
        raise ProductionAcceptanceProfileError(
            "workflow_matrix must cover every load_profile workflow_id"
        )
    _require_bounded_int(profile["p5_sample_n"], "p5_sample_n", minimum=1, maximum=_MAX_P5_SAMPLE_N)
    inventory_version, inventory_hash = _validate_callsite_inventory(
        profile["enabled_inference_callsite_inventory"],
        expected_hash=expected_inventory_sha256,
        expected_version=expected_inventory_version,
    )
    _validate_revocation(profile["revocation_sla"])
    _validate_pki_policy(profile["pki_policy"])
    _validate_severity_taxonomy(profile["severity_taxonomy"])
    _validate_finding_rule(profile["finding_acceptance_rule"])
    _validate_impact_matrix(profile["revalidation_impact_matrix"])
    valid_from, valid_until = _validate_profile_window(profile, now=now)
    if not valid_from <= observation_start < observation_end <= valid_until:
        raise ProductionAcceptanceProfileError(
            "observation_window must be contained by profile validity window"
        )
    signer_roles = _require_unique_strings(profile["signer_roles"], "signer_roles")
    if set(signer_roles) != REQUIRED_PRODUCTION_SIGNER_ROLES:
        raise ProductionAcceptanceProfileError(
            "signer_roles must contain exactly the five required roles"
        )
    _require_hash(profile["profile_content_sha256"], "profile_content_sha256")
    if not isinstance(profile["signatures"], list):
        raise ProductionAcceptanceProfileError("signatures must be a list")
    return (
        inventory_version,
        inventory_hash,
        valid_from,
        valid_until,
        observation_start,
        observation_end,
    )


def validate_production_acceptance_profile(
    profile: Mapping[str, Any],
    *,
    now: datetime | None = None,
    expected_inventory_sha256: str | None = None,
    expected_inventory_version: str | None = None,
    allow_disabled_template: bool = False,
) -> None:
    """Validate the machine-readable P0 shape without verifying signatures.

    ``allow_disabled_template`` exists only for authoring/tooling checks.  It
    must never be enabled by runtime admission callers; the signed verifier
    always leaves it false.
    """

    if not isinstance(profile, Mapping) or set(profile) != _PROFILE_FIELDS:
        raise ProductionAcceptanceProfileError("profile has unknown or missing fields")
    if profile["schema_version"] != PRODUCTION_ACCEPTANCE_SCHEMA:
        raise ProductionAcceptanceProfileError("unknown production-acceptance profile schema")
    _require_profile_id(profile["profile_id"])
    _require_string(profile["profile_version"], "profile_version", max_length=128)
    if not isinstance(profile["enabled"], bool) or not isinstance(profile["template_only"], bool):
        raise ProductionAcceptanceProfileError("enabled/template_only must be boolean")
    status = profile["approval_status"]
    if status not in {"approved", "disabled_template"}:
        raise ProductionAcceptanceProfileError("approval_status is invalid")
    if profile["enabled"] is False:
        if profile["template_only"] is not True or status != "disabled_template":
            raise ProductionAcceptanceProfileError(
                "disabled profile must explicitly declare template_only and disabled_template"
            )
        if not allow_disabled_template:
            raise ProductionAcceptanceProfileError(
                "disabled template is not a production approval"
            )
        return
    _validate_enabled_profile(
        profile,
        now=_normalise_now(now),
        expected_inventory_sha256=expected_inventory_sha256,
        expected_inventory_version=expected_inventory_version,
    )


def _verify_signatures(profile: Mapping[str, Any], trust_store: Mapping[str, Any]) -> None:
    if not isinstance(trust_store, Mapping) or set(trust_store) != {"trusted_signers"}:
        raise ProductionAcceptanceProfileError("trust store has unknown or missing fields")
    trusted = trust_store["trusted_signers"]
    if not isinstance(trusted, Mapping) or set(trusted) != REQUIRED_PRODUCTION_SIGNER_ROLES:
        raise ProductionAcceptanceProfileError(
            "trust store must contain exactly the five required signer roles"
        )
    signatures = profile["signatures"]
    if not isinstance(signatures, list) or len(signatures) != len(REQUIRED_PRODUCTION_SIGNER_ROLES):
        raise ProductionAcceptanceProfileError(
            "exactly five signatures are required for production approval"
        )
    payload = dict(profile)
    payload.pop("signatures", None)
    seen: set[str] = set()
    fingerprints: set[str] = set()
    for index, entry in enumerate(signatures):
        if not isinstance(entry, Mapping) or set(entry) != {"role", "signature"}:
            raise ProductionAcceptanceProfileError(f"signatures[{index}] has unknown or missing fields")
        role = entry["role"]
        if role not in REQUIRED_PRODUCTION_SIGNER_ROLES or role in seen:
            raise ProductionAcceptanceProfileError(f"invalid or duplicate signer role: {role!r}")
        pem = trusted[role]
        if not isinstance(pem, str):
            raise ProductionAcceptanceProfileError(f"trusted key for {role} is invalid")
        signature_text = entry["signature"]
        if not isinstance(signature_text, str):
            raise ProductionAcceptanceProfileError(f"signature for {role} must be base64 text")
        try:
            key = serialization.load_pem_public_key(pem.encode("ascii"))
            if not isinstance(key, Ed25519PublicKey):
                raise TypeError("trusted key is not Ed25519")
            fingerprint = hashlib.sha256(
                key.public_bytes(
                    serialization.Encoding.DER,
                    serialization.PublicFormat.SubjectPublicKeyInfo,
                )
            ).hexdigest()
            if fingerprint in fingerprints:
                raise TypeError("trusted signer roles must use distinct keys")
            signature = base64.b64decode(signature_text, validate=True)
            if len(signature) != 64:
                raise ValueError("Ed25519 signature must be 64 bytes")
            key.verify(signature, canonical_json(payload))
        except Exception as exc:  # noqa: BLE001 - signature policy must fail closed
            raise ProductionAcceptanceProfileError(
                f"invalid signature for {role}: {exc}"
            ) from exc
        seen.add(role)
        fingerprints.add(fingerprint)
    if seen != REQUIRED_PRODUCTION_SIGNER_ROLES:
        raise ProductionAcceptanceProfileError(
            f"missing signer roles: {sorted(REQUIRED_PRODUCTION_SIGNER_ROLES - seen)}"
        )


def verify_production_acceptance_profile(
    profile: Mapping[str, Any],
    trust_store: Mapping[str, Any],
    *,
    now: datetime | None = None,
    expected_inventory_sha256: str | None = None,
    expected_inventory_version: str | None = None,
) -> VerifiedProductionAcceptanceProfile:
    """Verify an enabled, five-party signed production acceptance profile."""

    if not isinstance(profile, Mapping):
        raise ProductionAcceptanceProfileError("profile must be an object")
    # This deliberately rejects disabled templates before any attempt to
    # construct a runtime authority.  There is no runtime escape hatch.
    validate_production_acceptance_profile(
        profile,
        now=now,
        expected_inventory_sha256=expected_inventory_sha256,
        expected_inventory_version=expected_inventory_version,
        allow_disabled_template=False,
    )
    expected_hash = production_profile_content_sha256(profile)
    if profile["profile_content_sha256"] != expected_hash:
        raise ProductionAcceptanceProfileError("profile_content_sha256 mismatch")
    _verify_signatures(profile, trust_store)
    inventory = profile["enabled_inference_callsite_inventory"]
    valid_from = _parse_rfc3339(profile["valid_from"], "valid_from")
    valid_until = _parse_rfc3339(profile["valid_until"], "valid_until")
    observation = profile["observation_window"]
    observation_start = _parse_rfc3339(observation["start"], "observation_window.start")
    observation_end = _parse_rfc3339(observation["end"], "observation_window.end")
    return VerifiedProductionAcceptanceProfile(
        profile_id=str(profile["profile_id"]),
        profile_version=str(profile["profile_version"]),
        profile_content_sha256=str(profile["profile_content_sha256"]),
        inventory_version=str(inventory["version"]),
        inventory_sha256=str(inventory["sha256"]),
        data_classification_ceiling=str(profile["data_classification_ceiling"]),
        signer_roles=tuple(profile["signer_roles"]),
        valid_from=valid_from,
        valid_until=valid_until,
        observation_start=observation_start,
        observation_end=observation_end,
        profile=MappingProxyType(dict(profile)),
    )


def verify_signed_production_acceptance_profile(
    profile_path: str | Path,
    trust_store_path: str | Path,
    *,
    inventory_path: str | Path | None = None,
    now: datetime | None = None,
    expected_inventory_sha256: str | None = None,
    expected_inventory_version: str | None = None,
) -> VerifiedProductionAcceptanceProfile:
    """Read and verify a signed profile using an out-of-band trust store.

    ``inventory_path`` is optional for callers that already know the expected
    digest/version.  When supplied, the digest is computed from the same
    canonical JSON bytes used by this module and is always checked.
    """

    profile = _read_json(profile_path, label="profile")
    trust_store = _read_json(trust_store_path, label="trust store")
    if inventory_path is not None:
        inventory = _read_json(inventory_path, label="inventory")
        expected_inventory_sha256 = sha256_hex(inventory)
        candidate_version = inventory.get("version", inventory.get("inventory_version"))
        if not isinstance(candidate_version, str) or not candidate_version.strip():
            raise ProductionAcceptanceProfileError("inventory must contain version or inventory_version")
        expected_inventory_version = candidate_version
    return verify_production_acceptance_profile(
        profile,
        trust_store,
        now=now,
        expected_inventory_sha256=expected_inventory_sha256,
        expected_inventory_version=expected_inventory_version,
    )


# Compatibility spelling for callers that mirror the Gate 2 function name.
verify_signed_production_profile = verify_signed_production_acceptance_profile


__all__ = [
    "PRODUCTION_ACCEPTANCE_SCHEMA",
    "PRODUCTION_SIGNER_ROLES",
    "REQUIRED_PRODUCTION_SIGNER_ROLES",
    "ProductionAcceptanceProfileError",
    "VerifiedProductionAcceptanceProfile",
    "canonical_json",
    "production_profile_content_sha256",
    "profile_content_sha256",
    "sha256_hex",
    "validate_production_acceptance_profile",
    "verify_production_acceptance_profile",
    "verify_signed_production_acceptance_profile",
    "verify_signed_production_profile",
]
