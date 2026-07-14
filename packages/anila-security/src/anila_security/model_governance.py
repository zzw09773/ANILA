"""Thin, dependency-light contracts for Gate 5 model governance.

This module intentionally contains data contracts only.  It does not perform
runtime admission, make network calls, or reconcile usage rows.  Those
responsibilities belong to the CSP/runtime layers that will consume the
verified authority in a later Gate 5 change.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any

GOVERNANCE_INVENTORY_SCHEMA = "anila.gate5.model-governance.inventory.v1"
GOVERNANCE_PROFILE_SCHEMA = "anila.gate5.model-governance.profile.v1"
INFERENCE_SCANNER_SCHEMA = "anila.gate5.inference-scanner.v1"
INFERENCE_SCANNER_SOURCE_ROOTS = ("packages", "services", "infra/models/src")
INFERENCE_SCANNER_EXCLUDED_PATH_PARTS = (
    ".git",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "examples",
    "example",
    "templates",
    "test",
    "tests",
)
INFERENCE_SCANNER_PATTERN_VERSION = "2026-07-15"
GATEWAY_ID = "csp-model-gateway"
GATEWAY_NETWORK = "csp-model-gateway"

CLASSIFICATION_ORDER = ("無機密", "營業秘密", "機密", "極機密", "絕對機密")
CLASSIFICATION_LEVELS = frozenset(CLASSIFICATION_ORDER)
REQUIRED_APPROVER_ROLES = frozenset({"system_owner", "data_owner", "security", "operations"})
SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
HEX_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{1,127}$")

INVENTORY_FIELDS = {
    "schema_version",
    "inventory_id",
    "inventory_version",
    "scanner",
    "gateway",
    "callsites",
}
SCANNER_FIELDS = {
    "schema_version",
    "source_roots",
    "excluded_path_parts",
    "pattern_version",
}
GATEWAY_FIELDS = {"id", "owner", "service", "unique", "raw_endpoint_policy"}
PROFILE_FIELDS = {
    "schema_version",
    "profile_id",
    "profile_version",
    "profile_content_sha256",
    "inventory_sha256",
    "enabled",
    "enabled_callsites",
    "disabled_callsites",
    "callsite_bindings",
    "model_artifacts",
    "deployments",
    "egress_policy",
    "valid_from",
    "valid_until",
    "approvers",
    "signatures",
}
BINDING_FIELDS = {
    "callsite_id",
    "gateway_id",
    "classification_ceiling",
    "usage_sink",
    "audit_sink",
    "agent_scope",
    "model_artifact_id",
    "deployment_id",
}
EGRESS_FIELDS = {"gateway_id", "raw_endpoint_denied", "allowed_network"}


class ModelGovernanceError(ValueError):
    """Raised when a model-governance contract is malformed."""


def canonical_json(value: Any) -> bytes:
    """Return the one canonical JSON representation used for profile hashes."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_hex(value: Any) -> str:
    """Hash a JSON value using :func:`canonical_json`."""

    return hashlib.sha256(canonical_json(value)).hexdigest()


def parse_rfc3339(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ModelGovernanceError(f"{field} must be RFC3339")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ModelGovernanceError(f"{field} must be RFC3339") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ModelGovernanceError(f"{field} must include timezone")
    return parsed.astimezone(timezone.utc)


def require_identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER_RE.fullmatch(value):
        raise ModelGovernanceError(f"{field} must be an identifier")
    return value


def require_sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise ModelGovernanceError(f"{field} must be sha256:<64 lowercase hex>")
    return value


def parse_classification_level(value: Any, field: str = "classification") -> str:
    """Parse the canonical five-level wire values without a local enum fork."""

    if not isinstance(value, str):
        enum_value = getattr(value, "value", None)
        if isinstance(enum_value, str):
            value = enum_value
    if not isinstance(value, str):
        raise ModelGovernanceError(f"{field} must be a canonical classification level")
    if value not in CLASSIFICATION_LEVELS:
        raise ModelGovernanceError(f"{field} is invalid")
    return value


def classification_rank(value: Any, field: str = "classification") -> int:
    """Return the canonical rank shared with ``anila-contracts`` tests."""

    parsed = parse_classification_level(value, field)
    return CLASSIFICATION_ORDER.index(parsed)


def require_hex_sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or not HEX_SHA256_RE.fullmatch(value):
        raise ModelGovernanceError(f"{field} must be 64 lowercase hex characters")
    return value


def require_string(value: Any, field: str, *, max_length: int = 512) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        or len(value) > max_length
    ):
        raise ModelGovernanceError(f"{field} must be a non-empty trimmed string")
    return value


def require_unique_strings(value: Any, field: str, *, allow_empty: bool = True) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ModelGovernanceError(f"{field} must be a list")
    if not allow_empty and not value:
        raise ModelGovernanceError(f"{field} must not be empty")
    items = tuple(require_string(item, f"{field}[]", max_length=256) for item in value)
    if len(items) != len(set(items)):
        raise ModelGovernanceError(f"{field} contains duplicates")
    return items


@dataclass(frozen=True, slots=True)
class ModelArtifact:
    """Immutable model-weight identity and license decision."""

    artifact_id: str
    model_family: str
    digest: str
    revision: str
    license_id: str
    license_approved: bool
    license_approval_artifact_id: str | None
    legal_approver_ids: tuple[str, ...]

    @classmethod
    def from_dict(
        cls, value: Mapping[str, Any], *, field: str = "model_artifact"
    ) -> "ModelArtifact":
        if not isinstance(value, Mapping):
            raise ModelGovernanceError(f"{field} must be an object")
        expected = {
            "artifact_id",
            "model_family",
            "digest",
            "revision",
            "license_id",
            "license_approved",
            "license_approval_artifact_id",
            "legal_approver_ids",
        }
        if set(value) != expected:
            raise ModelGovernanceError(f"{field} has unknown or missing fields")
        approved = value["license_approved"]
        if not isinstance(approved, bool):
            raise ModelGovernanceError(f"{field}.license_approved must be boolean")
        approval_id = value["license_approval_artifact_id"]
        if approval_id is not None:
            approval_id = require_identifier(approval_id, f"{field}.license_approval_artifact_id")
        return cls(
            artifact_id=require_identifier(value["artifact_id"], f"{field}.artifact_id"),
            model_family=require_string(value["model_family"], f"{field}.model_family"),
            digest=require_sha256(value["digest"], f"{field}.digest"),
            revision=require_string(value["revision"], f"{field}.revision", max_length=256),
            license_id=require_string(value["license_id"], f"{field}.license_id", max_length=256),
            license_approved=approved,
            license_approval_artifact_id=approval_id,
            legal_approver_ids=require_unique_strings(
                value["legal_approver_ids"], f"{field}.legal_approver_ids"
            ),
        )


@dataclass(frozen=True, slots=True)
class Deployment:
    """Immutable executable image plus GPU/readiness evidence."""

    deployment_id: str
    artifact_id: str
    image_digest: str
    gpu_vendor: str
    gpu_count: int
    gpu_memory_gib: int
    gpu_compute_capability: str
    health_url: str
    readiness_url: str
    readiness_freshness_seconds: int
    last_health_check: datetime
    healthy: bool
    ready: bool

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, field: str = "deployment") -> "Deployment":
        if not isinstance(value, Mapping):
            raise ModelGovernanceError(f"{field} must be an object")
        expected = {
            "deployment_id",
            "artifact_id",
            "image_digest",
            "gpu_topology",
            "health_readiness",
        }
        if set(value) != expected:
            raise ModelGovernanceError(f"{field} has unknown or missing fields")
        topology = value["gpu_topology"]
        health = value["health_readiness"]
        if not isinstance(topology, Mapping) or set(topology) != {
            "vendor",
            "count",
            "memory_gib",
            "compute_capability",
        }:
            raise ModelGovernanceError(f"{field}.gpu_topology has unknown or missing fields")
        if not isinstance(health, Mapping) or set(health) != {
            "health_url",
            "readiness_url",
            "freshness_seconds",
            "last_check",
            "healthy",
            "ready",
        }:
            raise ModelGovernanceError(f"{field}.health_readiness has unknown or missing fields")
        count = topology["count"]
        memory = topology["memory_gib"]
        freshness = health["freshness_seconds"]
        if any(
            isinstance(item, bool) or not isinstance(item, int)
            for item in (count, memory, freshness)
        ):
            raise ModelGovernanceError(f"{field} GPU/readiness numeric fields must be integers")
        if count <= 0 or memory <= 0 or freshness <= 0:
            raise ModelGovernanceError(f"{field} GPU/readiness numeric fields must be positive")
        if not isinstance(health["healthy"], bool) or not isinstance(health["ready"], bool):
            raise ModelGovernanceError(f"{field}.health_readiness healthy/ready must be boolean")
        return cls(
            deployment_id=require_identifier(value["deployment_id"], f"{field}.deployment_id"),
            artifact_id=require_identifier(value["artifact_id"], f"{field}.artifact_id"),
            image_digest=require_sha256(value["image_digest"], f"{field}.image_digest"),
            gpu_vendor=require_string(
                topology["vendor"], f"{field}.gpu_topology.vendor", max_length=128
            ),
            gpu_count=count,
            gpu_memory_gib=memory,
            gpu_compute_capability=require_string(
                topology["compute_capability"],
                f"{field}.gpu_topology.compute_capability",
                max_length=128,
            ),
            health_url=require_string(health["health_url"], f"{field}.health_readiness.health_url"),
            readiness_url=require_string(
                health["readiness_url"], f"{field}.health_readiness.readiness_url"
            ),
            readiness_freshness_seconds=freshness,
            last_health_check=parse_rfc3339(
                health["last_check"], f"{field}.health_readiness.last_check"
            ),
            healthy=health["healthy"],
            ready=health["ready"],
        )


@dataclass(frozen=True, slots=True)
class InferenceCallsite:
    """One source-level inference sink in the signed inventory."""

    callsite_id: str
    owner: str
    service: str
    source: str
    symbol: str
    kind: str
    enabled: bool
    gateway_id: str | None
    raw_endpoint: bool
    classification_ceiling: str
    usage_sink: str | None
    audit_sink: str | None
    agent_scope: tuple[str, ...]
    sink_kinds: tuple[str, ...]

    @property
    def classification_level(self) -> str:
        """Return the ceiling through the canonical contracts enum."""

        return parse_classification_level(self.classification_ceiling)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, field: str = "callsite") -> "InferenceCallsite":
        if not isinstance(value, Mapping):
            raise ModelGovernanceError(f"{field} must be an object")
        expected = {
            "id",
            "owner",
            "service",
            "source",
            "symbol",
            "kind",
            "enabled",
            "gateway_id",
            "raw_endpoint",
            "classification_ceiling",
            "usage_sink",
            "audit_sink",
            "agent_scope",
            "sink_kinds",
        }
        if set(value) != expected:
            raise ModelGovernanceError(f"{field} has unknown or missing fields")
        enabled = value["enabled"]
        raw_endpoint = value["raw_endpoint"]
        if not isinstance(enabled, bool) or not isinstance(raw_endpoint, bool):
            raise ModelGovernanceError(f"{field}.enabled/raw_endpoint must be boolean")
        gateway = value["gateway_id"]
        if gateway is not None:
            gateway = require_identifier(gateway, f"{field}.gateway_id")
        usage = value["usage_sink"]
        audit = value["audit_sink"]
        if usage is not None:
            usage = require_string(usage, f"{field}.usage_sink", max_length=256)
        if audit is not None:
            audit = require_string(audit, f"{field}.audit_sink", max_length=256)
        sink_kinds = require_unique_strings(
            value["sink_kinds"], f"{field}.sink_kinds", allow_empty=False
        )
        classification = value["classification_ceiling"]
        parse_classification_level(classification, f"{field}.classification_ceiling")
        return cls(
            callsite_id=require_identifier(value["id"], f"{field}.id"),
            owner=require_string(value["owner"], f"{field}.owner", max_length=256),
            service=require_string(value["service"], f"{field}.service", max_length=256),
            source=require_string(value["source"], f"{field}.source", max_length=512),
            symbol=require_string(value["symbol"], f"{field}.symbol", max_length=256),
            kind=require_string(value["kind"], f"{field}.kind", max_length=128),
            enabled=enabled,
            gateway_id=gateway,
            raw_endpoint=raw_endpoint,
            classification_ceiling=classification,
            usage_sink=usage,
            audit_sink=audit,
            agent_scope=require_unique_strings(value["agent_scope"], f"{field}.agent_scope"),
            sink_kinds=sink_kinds,
        )


def validate_profile_approver_shape(value: Any) -> tuple[dict[str, str], ...]:
    if not isinstance(value, list):
        raise ModelGovernanceError("approvers must be a list")
    result: list[dict[str, str]] = []
    seen_roles: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, Mapping) or set(item) != {"role", "subject"}:
            raise ModelGovernanceError(f"approvers[{index}] has unknown or missing fields")
        role = require_string(item["role"], f"approvers[{index}].role", max_length=64)
        subject = require_string(item["subject"], f"approvers[{index}].subject", max_length=256)
        if role in seen_roles:
            raise ModelGovernanceError(f"approvers contains duplicate role {role}")
        seen_roles.add(role)
        result.append({"role": role, "subject": subject})
    return tuple(result)


@dataclass(frozen=True, slots=True)
class CallsiteBinding:
    """One immutable profile admission binding for an inventory callsite."""

    callsite_id: str
    gateway_id: str | None
    classification_ceiling: str
    usage_sink: str | None
    audit_sink: str | None
    agent_scope: tuple[str, ...]
    model_artifact_id: str
    deployment_id: str

    @classmethod
    def from_dict(
        cls, value: Mapping[str, Any], *, field: str = "callsite_binding"
    ) -> "CallsiteBinding":
        if not isinstance(value, Mapping) or set(value) != BINDING_FIELDS:
            raise ModelGovernanceError(f"{field} has unknown or missing fields")
        gateway_value = value["gateway_id"]
        gateway = (
            None
            if gateway_value is None
            else require_identifier(gateway_value, f"{field}.gateway_id")
        )
        usage_value = value["usage_sink"]
        usage = (
            None
            if usage_value is None
            else require_string(usage_value, f"{field}.usage_sink", max_length=256)
        )
        audit_value = value["audit_sink"]
        audit = (
            None
            if audit_value is None
            else require_string(audit_value, f"{field}.audit_sink", max_length=256)
        )
        ceiling = parse_classification_level(
            value["classification_ceiling"], f"{field}.classification_ceiling"
        )
        return cls(
            callsite_id=require_identifier(value["callsite_id"], f"{field}.callsite_id"),
            gateway_id=gateway,
            classification_ceiling=ceiling,
            usage_sink=usage,
            audit_sink=audit,
            agent_scope=require_unique_strings(value["agent_scope"], f"{field}.agent_scope"),
            model_artifact_id=require_identifier(
                value["model_artifact_id"], f"{field}.model_artifact_id"
            ),
            deployment_id=require_identifier(value["deployment_id"], f"{field}.deployment_id"),
        )

    @property
    def classification_level(self) -> str:
        return parse_classification_level(self.classification_ceiling)


@dataclass(frozen=True, slots=True)
class ModelGovernanceAuthorization:
    """The verified, runtime-consumable result of an authority decision."""

    callsite_id: str
    classification: str
    gateway_id: str
    model_artifact: ModelArtifact
    deployment: Deployment
    usage_sink: str
    audit_sink: str
    agent_scope: tuple[str, ...]


def inventory_content_sha256(inventory: Mapping[str, Any]) -> str:
    """Return the digest bound into a profile for one inventory payload."""

    return hashlib.sha256(canonical_json(inventory)).hexdigest()


def profile_content_sha256(profile: Mapping[str, Any]) -> str:
    """Hash profile content without its self-referential hash/signatures."""

    unsigned = dict(profile)
    unsigned.pop("profile_content_sha256", None)
    unsigned.pop("signatures", None)
    return hashlib.sha256(canonical_json(unsigned)).hexdigest()


def validate_inventory_payload(
    inventory: Mapping[str, Any],
) -> dict[str, InferenceCallsite]:
    """Validate inventory semantics shared by the static checker and runtime."""

    if not isinstance(inventory, Mapping) or set(inventory) != INVENTORY_FIELDS:
        raise ModelGovernanceError("inventory has unknown or missing fields")
    if inventory.get("schema_version") != GOVERNANCE_INVENTORY_SCHEMA:
        raise ModelGovernanceError("unknown model-governance inventory schema")
    require_identifier(inventory.get("inventory_id"), "inventory_id")
    require_string(inventory.get("inventory_version"), "inventory_version", max_length=64)

    scanner = inventory.get("scanner")
    if not isinstance(scanner, Mapping) or set(scanner) != SCANNER_FIELDS:
        raise ModelGovernanceError("scanner contract is malformed")
    if scanner.get("schema_version") != INFERENCE_SCANNER_SCHEMA:
        raise ModelGovernanceError("unknown inference scanner schema")
    if scanner.get("source_roots") != list(INFERENCE_SCANNER_SOURCE_ROOTS):
        raise ModelGovernanceError("scanner source roots drifted")
    if scanner.get("excluded_path_parts") != sorted(INFERENCE_SCANNER_EXCLUDED_PATH_PARTS):
        raise ModelGovernanceError("scanner exclusions drifted")
    if scanner.get("pattern_version") != INFERENCE_SCANNER_PATTERN_VERSION:
        raise ModelGovernanceError("scanner pattern version is not pinned")

    gateway = inventory.get("gateway")
    if not isinstance(gateway, Mapping) or set(gateway) != GATEWAY_FIELDS:
        raise ModelGovernanceError("gateway contract is malformed")
    if (
        gateway.get("id") != GATEWAY_ID
        or gateway.get("unique") is not True
        or gateway.get("raw_endpoint_policy") != "deny"
    ):
        raise ModelGovernanceError("inventory must pin one deny-by-default CSP gateway")
    require_string(gateway.get("owner"), "gateway.owner")
    require_string(gateway.get("service"), "gateway.service")

    raw_calls = inventory.get("callsites")
    if not isinstance(raw_calls, list) or not raw_calls:
        raise ModelGovernanceError("inventory callsites must be non-empty")
    by_id: dict[str, InferenceCallsite] = {}
    for index, raw in enumerate(raw_calls):
        call = InferenceCallsite.from_dict(raw, field=f"callsites[{index}]")
        if call.callsite_id in by_id:
            raise ModelGovernanceError(f"duplicate callsite {call.callsite_id}")
        # Repository inventory is an evidence list.  Admission is controlled
        # only by a verified profile binding, never by this source fixture.
        if call.enabled:
            raise ModelGovernanceError(
                f"repository inventory cannot pre-enable callsite {call.callsite_id}"
            )
        if call.raw_endpoint and call.gateway_id == GATEWAY_ID:
            raise ModelGovernanceError(
                f"raw endpoint callsite cannot claim CSP gateway: {call.callsite_id}"
            )
        by_id[call.callsite_id] = call
    return by_id


def validate_profile_shape(profile: Mapping[str, Any]) -> None:
    if not isinstance(profile, Mapping) or set(profile) != PROFILE_FIELDS:
        raise ModelGovernanceError("profile has unknown or missing fields")
    if profile.get("schema_version") != GOVERNANCE_PROFILE_SCHEMA:
        raise ModelGovernanceError("unknown model-governance profile schema")
    require_identifier(profile.get("profile_id"), "profile_id")
    require_string(profile.get("profile_version"), "profile_version", max_length=64)
    require_hex_sha256(profile.get("profile_content_sha256"), "profile_content_sha256")
    require_hex_sha256(profile.get("inventory_sha256"), "inventory_sha256")
    if not isinstance(profile.get("enabled"), bool):
        raise ModelGovernanceError("profile.enabled must be boolean")
    for field in ("enabled_callsites", "disabled_callsites"):
        require_unique_strings(profile.get(field), field)
    for field in ("callsite_bindings", "model_artifacts", "deployments"):
        if not isinstance(profile.get(field), list):
            raise ModelGovernanceError(f"{field} must be a list")
    egress = profile.get("egress_policy")
    if not isinstance(egress, Mapping) or set(egress) != EGRESS_FIELDS:
        raise ModelGovernanceError("egress_policy is malformed")
    if (
        egress.get("gateway_id") != GATEWAY_ID
        or egress.get("raw_endpoint_denied") is not True
        or egress.get("allowed_network") != GATEWAY_NETWORK
    ):
        raise ModelGovernanceError("egress policy must deny raw endpoints")
    validate_profile_approver_shape(profile.get("approvers"))
    if not isinstance(profile.get("signatures"), list):
        raise ModelGovernanceError("signatures must be a list")


def verify_profile_signatures(profile: Mapping[str, Any], trust_store: Mapping[str, Any]) -> None:
    """Verify all distinct required Ed25519 approver signatures."""

    try:
        import base64

        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as exc:  # pragma: no cover - dependency is required by this package
        raise ModelGovernanceError("cryptography is required for profile signatures") from exc
    trusted = trust_store.get("trusted_signers")
    if not isinstance(trusted, Mapping):
        raise ModelGovernanceError("trust store must contain trusted_signers")
    payload = dict(profile)
    signatures = payload.pop("signatures", None)
    if not isinstance(signatures, list):
        raise ModelGovernanceError("signatures must be a list")
    seen: set[str] = set()
    fingerprints: set[str] = set()
    for index, entry in enumerate(signatures):
        if not isinstance(entry, Mapping) or set(entry) != {"role", "signature"}:
            raise ModelGovernanceError(f"signatures[{index}] is malformed")
        role = entry["role"]
        if role not in REQUIRED_APPROVER_ROLES or role in seen:
            raise ModelGovernanceError(f"invalid/duplicate signer role: {role!r}")
        pem = trusted.get(role)
        if not isinstance(pem, str):
            raise ModelGovernanceError(f"missing trusted key for {role}")
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
            signature = entry["signature"]
            if not isinstance(signature, str):
                raise TypeError("signature must be base64 text")
            key.verify(base64.b64decode(signature, validate=True), canonical_json(payload))
        except Exception as exc:  # noqa: BLE001 - policy must fail closed
            raise ModelGovernanceError(f"invalid signature for {role}: {exc}") from exc
        seen.add(role)
        fingerprints.add(fingerprint)
    if seen != REQUIRED_APPROVER_ROLES:
        raise ModelGovernanceError(
            f"missing signer roles: {sorted(REQUIRED_APPROVER_ROLES - seen)}"
        )


def _validate_artifact(
    artifact: ModelArtifact,
) -> None:
    if not artifact.license_approved or not artifact.license_approval_artifact_id:
        raise ModelGovernanceError(
            f"model artifact {artifact.artifact_id} lacks approved license evidence"
        )
    if "flux.2-dev" in artifact.model_family.casefold() and "legal" not in {
        item.casefold() for item in artifact.legal_approver_ids
    }:
        raise ModelGovernanceError(
            "FLUX.2-dev production enablement requires a legal approval artifact"
        )


def _normalise_now(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ModelGovernanceError("now must include timezone")
    return current.astimezone(timezone.utc)


def _verify_deployment_freshness(deployment: Deployment, *, now: datetime) -> None:
    if not deployment.healthy or not deployment.ready:
        raise ModelGovernanceError(f"deployment {deployment.deployment_id} is not healthy/ready")
    age = (now - deployment.last_health_check).total_seconds()
    if age < 0 or age > deployment.readiness_freshness_seconds:
        raise ModelGovernanceError(
            f"deployment {deployment.deployment_id} health/readiness evidence is stale"
        )


def _verified_profile_parts(
    *,
    profile: Mapping[str, Any],
    calls: Mapping[str, InferenceCallsite],
    now: datetime,
) -> tuple[
    bool,
    tuple[str, ...],
    tuple[str, ...],
    dict[str, CallsiteBinding],
    dict[str, ModelArtifact],
    dict[str, Deployment],
    datetime | None,
    datetime | None,
]:
    enabled_values = profile["enabled_callsites"]
    disabled_values = profile["disabled_callsites"]
    if not isinstance(enabled_values, list) or not isinstance(disabled_values, list):
        raise ModelGovernanceError("profile callsite lists are malformed")
    enabled = tuple(enabled_values)
    disabled = tuple(disabled_values)
    if set(enabled) & set(disabled) or set(enabled) | set(disabled) != set(calls):
        raise ModelGovernanceError("profile must partition every inventory callsite")

    profile_enabled = profile["enabled"]
    if not isinstance(profile_enabled, bool):
        raise ModelGovernanceError("profile.enabled must be boolean")
    if not profile_enabled:
        if enabled:
            raise ModelGovernanceError("disabled profile cannot enable callsites")
        if profile["callsite_bindings"] or profile["model_artifacts"] or profile["deployments"]:
            raise ModelGovernanceError("disabled profile cannot carry model authority")
        if profile["valid_from"] is not None or profile["valid_until"] is not None:
            raise ModelGovernanceError("disabled profile cannot carry validity")
        if profile["approvers"] or profile["signatures"]:
            raise ModelGovernanceError("disabled profile cannot carry approvers/signatures")
        return False, enabled, disabled, {}, {}, {}, None, None

    if not enabled:
        raise ModelGovernanceError("enabled profile has no enabled callsites")
    valid_from = parse_rfc3339(profile["valid_from"], "valid_from")
    valid_until = parse_rfc3339(profile["valid_until"], "valid_until")
    if valid_until <= valid_from or (valid_until - valid_from).total_seconds() > 365 * 86400:
        raise ModelGovernanceError("profile validity interval is invalid or too long")
    if not valid_from <= now < valid_until:
        raise ModelGovernanceError("production profile is not currently effective")
    approvers = validate_profile_approver_shape(profile["approvers"])
    if {entry["role"] for entry in approvers} != REQUIRED_APPROVER_ROLES:
        raise ModelGovernanceError("production profile approvers are incomplete")

    artifacts: dict[str, ModelArtifact] = {}
    for index, raw in enumerate(profile["model_artifacts"]):
        artifact = ModelArtifact.from_dict(raw, field=f"model_artifacts[{index}]")
        if artifact.artifact_id in artifacts:
            raise ModelGovernanceError(f"duplicate model artifact {artifact.artifact_id}")
        _validate_artifact(artifact)
        artifacts[artifact.artifact_id] = artifact

    deployments: dict[str, Deployment] = {}
    for index, raw in enumerate(profile["deployments"]):
        deployment = Deployment.from_dict(raw, field=f"deployments[{index}]")
        if deployment.deployment_id in deployments:
            raise ModelGovernanceError(f"duplicate deployment {deployment.deployment_id}")
        if deployment.artifact_id not in artifacts:
            raise ModelGovernanceError(
                f"deployment {deployment.deployment_id} references unknown artifact"
            )
        _verify_deployment_freshness(deployment, now=now)
        deployments[deployment.deployment_id] = deployment

    bindings: dict[str, CallsiteBinding] = {}
    for index, raw in enumerate(profile["callsite_bindings"]):
        binding = CallsiteBinding.from_dict(raw, field=f"callsite_bindings[{index}]")
        if binding.callsite_id in bindings:
            raise ModelGovernanceError(f"duplicate binding {binding.callsite_id}")
        if binding.callsite_id not in set(enabled):
            raise ModelGovernanceError(
                f"binding targets disabled/unknown callsite {binding.callsite_id}"
            )
        bindings[binding.callsite_id] = binding
    if set(bindings) != set(enabled):
        raise ModelGovernanceError("enabled callsites are missing exact bindings")

    for call_id, binding in bindings.items():
        call = calls[call_id]
        if call.raw_endpoint or call.gateway_id != GATEWAY_ID:
            raise ModelGovernanceError(f"raw/non-CSP callsite cannot be enabled: {call_id}")
        if binding.gateway_id != GATEWAY_ID:
            raise ModelGovernanceError(f"binding gateway is not the unique CSP gateway: {call_id}")
        if binding.classification_ceiling != call.classification_ceiling:
            raise ModelGovernanceError(f"classification ceiling drift for {call_id}")
        if binding.usage_sink != call.usage_sink or not binding.usage_sink:
            raise ModelGovernanceError(f"usage sink missing/drifted for {call_id}")
        if binding.audit_sink != call.audit_sink or not binding.audit_sink:
            raise ModelGovernanceError(f"audit sink missing/drifted for {call_id}")
        if binding.agent_scope != call.agent_scope:
            raise ModelGovernanceError(f"agent scope drift for {call_id}")
        admitted_artifact = artifacts.get(binding.model_artifact_id)
        admitted_deployment = deployments.get(binding.deployment_id)
        if admitted_artifact is None or admitted_deployment is None:
            raise ModelGovernanceError(f"binding {call_id} references unknown authority")
        if admitted_deployment.artifact_id != admitted_artifact.artifact_id:
            raise ModelGovernanceError(f"deployment/artifact mismatch for {call_id}")
    return (
        True,
        enabled,
        disabled,
        bindings,
        artifacts,
        deployments,
        valid_from,
        valid_until,
    )


_AUTHORITY_CONSTRUCTOR_TOKEN = object()


@dataclass(frozen=True, slots=True, init=False)
class VerifiedModelGovernanceAuthority:
    """Immutable model-governance authority built only from verified payloads."""

    profile_id: str
    profile_version: str
    profile_content_sha256: str
    inventory_sha256: str
    enabled: bool
    enabled_callsites: tuple[str, ...]
    disabled_callsites: tuple[str, ...]
    callsites: Mapping[str, InferenceCallsite]
    bindings: Mapping[str, CallsiteBinding]
    model_artifacts: Mapping[str, ModelArtifact]
    deployments: Mapping[str, Deployment]
    valid_from: datetime | None
    valid_until: datetime | None

    def __init__(
        self,
        *,
        _token: object,
        profile_id: str,
        profile_version: str,
        profile_content_sha256: str,
        inventory_sha256: str,
        enabled: bool,
        enabled_callsites: tuple[str, ...],
        disabled_callsites: tuple[str, ...],
        callsites: Mapping[str, InferenceCallsite],
        bindings: Mapping[str, CallsiteBinding],
        model_artifacts: Mapping[str, ModelArtifact],
        deployments: Mapping[str, Deployment],
        valid_from: datetime | None,
        valid_until: datetime | None,
    ) -> None:
        if _token is not _AUTHORITY_CONSTRUCTOR_TOKEN:
            raise ModelGovernanceError(
                "VerifiedModelGovernanceAuthority must be built from verified payloads"
            )
        object.__setattr__(self, "profile_id", profile_id)
        object.__setattr__(self, "profile_version", profile_version)
        object.__setattr__(self, "profile_content_sha256", profile_content_sha256)
        object.__setattr__(self, "inventory_sha256", inventory_sha256)
        object.__setattr__(self, "enabled", enabled)
        object.__setattr__(self, "enabled_callsites", tuple(enabled_callsites))
        object.__setattr__(self, "disabled_callsites", tuple(disabled_callsites))
        object.__setattr__(self, "callsites", MappingProxyType(dict(callsites)))
        object.__setattr__(self, "bindings", MappingProxyType(dict(bindings)))
        object.__setattr__(self, "model_artifacts", MappingProxyType(dict(model_artifacts)))
        object.__setattr__(self, "deployments", MappingProxyType(dict(deployments)))
        object.__setattr__(self, "valid_from", valid_from)
        object.__setattr__(self, "valid_until", valid_until)

    @classmethod
    def from_verified_payload(
        cls,
        *,
        inventory: Mapping[str, Any],
        profile: Mapping[str, Any],
        trust_store: Mapping[str, Any] | None = None,
        allow_disabled_template: bool = False,
        now: datetime | None = None,
    ) -> "VerifiedModelGovernanceAuthority":
        """Verify payloads, then return the only constructible authority object."""

        calls = validate_inventory_payload(inventory)
        validate_profile_shape(profile)
        inventory_digest = inventory_content_sha256(inventory)
        if profile["inventory_sha256"] != inventory_digest:
            raise ModelGovernanceError("profile inventory_sha256 mismatch")
        if profile["profile_content_sha256"] != profile_content_sha256(profile):
            raise ModelGovernanceError("profile_content_sha256 mismatch")
        current = _normalise_now(now)
        (
            enabled,
            enabled_callsites,
            disabled_callsites,
            bindings,
            artifacts,
            deployments,
            valid_from,
            valid_until,
        ) = _verified_profile_parts(profile=profile, calls=calls, now=current)
        if not enabled:
            if not allow_disabled_template:
                raise ModelGovernanceError("disabled template is evidence only, not an approval")
        else:
            if trust_store is None:
                raise ModelGovernanceError(
                    "enabled production profile requires an out-of-band trust store"
                )
            verify_profile_signatures(profile, trust_store)
        return cls(
            _token=_AUTHORITY_CONSTRUCTOR_TOKEN,
            profile_id=require_identifier(profile["profile_id"], "profile_id"),
            profile_version=require_string(
                profile["profile_version"], "profile_version", max_length=64
            ),
            profile_content_sha256=require_hex_sha256(
                profile["profile_content_sha256"], "profile_content_sha256"
            ),
            inventory_sha256=inventory_digest,
            enabled=enabled,
            enabled_callsites=enabled_callsites,
            disabled_callsites=disabled_callsites,
            callsites=calls,
            bindings=bindings,
            model_artifacts=artifacts,
            deployments=deployments,
            valid_from=valid_from,
            valid_until=valid_until,
        )

    def authorize(
        self,
        callsite_id: str,
        classification: Any,
        model_artifact_id: str,
        deployment_id: str,
        agent_scope: str | None = None,
        now: datetime | None = None,
        *,
        agent_id: str | None = None,
        artifact_digest: str | None = None,
        artifact_revision: str | None = None,
        deployment_image_digest: str | None = None,
    ) -> ModelGovernanceAuthorization:
        """Authorize one callsite with fail-closed binding and freshness checks."""

        current = _normalise_now(now)
        if not self.enabled:
            raise ModelGovernanceError("model-governance authority is disabled")
        if self.valid_from is None or self.valid_until is None:
            raise ModelGovernanceError("enabled authority has no validity interval")
        if not self.valid_from <= current < self.valid_until:
            raise ModelGovernanceError("model-governance authority is not currently effective")
        callsite = self.callsites.get(callsite_id)
        binding = self.bindings.get(callsite_id)
        if callsite is None or binding is None or callsite_id not in self.enabled_callsites:
            raise ModelGovernanceError(f"callsite {callsite_id!r} is not enabled")
        if callsite.raw_endpoint or callsite.gateway_id != GATEWAY_ID:
            raise ModelGovernanceError("callsite is not bound to the unique CSP gateway")
        if binding.gateway_id != GATEWAY_ID:
            raise ModelGovernanceError("binding is not bound to the unique CSP gateway")
        if not binding.usage_sink or not binding.audit_sink:
            raise ModelGovernanceError("usage and audit sinks are required")

        requested = parse_classification_level(classification)
        if classification_rank(requested) > classification_rank(binding.classification_level):
            raise ModelGovernanceError(
                f"classification {requested} exceeds callsite ceiling "
                f"{binding.classification_level}"
            )
        require_identifier(model_artifact_id, "model_artifact_id")
        require_identifier(deployment_id, "deployment_id")
        artifact = self.model_artifacts.get(model_artifact_id)
        deployment = self.deployments.get(deployment_id)
        if artifact is None or model_artifact_id != binding.model_artifact_id:
            raise ModelGovernanceError("model artifact is not admitted for this callsite")
        if deployment is None or deployment_id != binding.deployment_id:
            raise ModelGovernanceError("deployment is not admitted for this callsite")
        _validate_artifact(artifact)
        if artifact_digest is not None and artifact_digest != artifact.digest:
            raise ModelGovernanceError("model artifact digest mismatch")
        if artifact_revision is not None and artifact_revision != artifact.revision:
            raise ModelGovernanceError("model artifact revision mismatch")
        if deployment.artifact_id != artifact.artifact_id:
            raise ModelGovernanceError("deployment artifact binding mismatch")
        if (
            deployment_image_digest is not None
            and deployment_image_digest != deployment.image_digest
        ):
            raise ModelGovernanceError("deployment image digest mismatch")
        _verify_deployment_freshness(deployment, now=current)

        requested_agent = agent_id if agent_scope is None else agent_scope
        if agent_scope is not None and agent_id is not None and agent_scope != agent_id:
            raise ModelGovernanceError("agent scope arguments disagree")
        if binding.agent_scope:
            if requested_agent is None or requested_agent not in binding.agent_scope:
                raise ModelGovernanceError("agent is outside the admitted agent scope")
        elif requested_agent is not None:
            raise ModelGovernanceError("callsite has no admitted agent scope")
        return ModelGovernanceAuthorization(
            callsite_id=callsite_id,
            classification=requested,
            gateway_id=GATEWAY_ID,
            model_artifact=artifact,
            deployment=deployment,
            usage_sink=binding.usage_sink,
            audit_sink=binding.audit_sink,
            agent_scope=binding.agent_scope,
        )


__all__ = [
    "CLASSIFICATION_LEVELS",
    "CLASSIFICATION_ORDER",
    "CallsiteBinding",
    "GATEWAY_ID",
    "GOVERNANCE_INVENTORY_SCHEMA",
    "GOVERNANCE_PROFILE_SCHEMA",
    "INFERENCE_SCANNER_EXCLUDED_PATH_PARTS",
    "INFERENCE_SCANNER_PATTERN_VERSION",
    "INFERENCE_SCANNER_SCHEMA",
    "INFERENCE_SCANNER_SOURCE_ROOTS",
    "ModelArtifact",
    "ModelGovernanceError",
    "ModelGovernanceAuthorization",
    "Deployment",
    "InferenceCallsite",
    "REQUIRED_APPROVER_ROLES",
    "VerifiedModelGovernanceAuthority",
    "canonical_json",
    "classification_rank",
    "inventory_content_sha256",
    "parse_classification_level",
    "parse_rfc3339",
    "profile_content_sha256",
    "require_identifier",
    "require_hex_sha256",
    "require_sha256",
    "sha256_hex",
    "validate_inventory_payload",
    "validate_profile_shape",
    "validate_profile_approver_shape",
    "verify_profile_signatures",
]
