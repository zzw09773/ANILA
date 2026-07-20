"""Thin, dependency-light contracts for Gate 5 model governance.

This module intentionally contains data contracts only.  It does not perform
runtime admission, make network calls, or reconcile usage rows.  Those
responsibilities belong to the CSP/runtime layers that will consume the
verified authority in a later Gate 5 change.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit

GOVERNANCE_INVENTORY_SCHEMA_V1 = "anila.gate5.model-governance.inventory.v1"
GOVERNANCE_PROFILE_SCHEMA_V1 = "anila.gate5.model-governance.profile.v1"
GOVERNANCE_INVENTORY_SCHEMA_V2 = "anila.gate5.model-governance.inventory.v2"
GOVERNANCE_PROFILE_SCHEMA_V2 = "anila.gate5.model-governance.profile.v2"
# Current signed material must use v2.  The v1 constants remain named so the
# verifier can recognise old disabled evidence without admitting it.
GOVERNANCE_INVENTORY_SCHEMA = GOVERNANCE_INVENTORY_SCHEMA_V2
GOVERNANCE_PROFILE_SCHEMA = GOVERNANCE_PROFILE_SCHEMA_V2
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
AMBIGUOUS_NUMERIC_IPV4_COMPONENT_RE = re.compile(r"(?:[0-9]+|0[xX][0-9A-Fa-f]+)$")

PROVIDER_RESOLUTION_MODES = frozenset({"gateway_dynamic", "direct_raw"})
TRANSPORT_PORT_MODES = frozenset({"default", "explicit_nondefault"})
TRANSPORT_SCHEMES = frozenset({"http", "https", "grpc", "grpcs", "tcp", "tls"})
TRANSPORT_DEFAULT_PORTS = {
    "http": 80,
    "https": 443,
    "grpcs": 443,
}
# ``exact_ip`` is intentionally distinct from ``none``.  Both are valid for
# an IP literal, while a hostname must use production fail-closed DNS
# resolution when it is admitted as an external provider.
DNS_POLICIES = frozenset(
    {"production_fail_closed", "runtime_allowlist", "none", "exact_ip"}
)
PROVIDER_LOCALITIES = frozenset(
    {"external_governed", "internal_isolated", "internal_shim", "unclassified"}
)

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
PROFILE_FIELDS_V2 = PROFILE_FIELDS | {"provider_bindings"}
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
BINDING_FIELDS_V1 = frozenset(BINDING_FIELDS)
BINDING_FIELDS_V2 = {
    "callsite_id",
    "gateway_id",
    "classification_ceiling",
    "usage_sink",
    "audit_sink",
    "agent_scope",
    "provider_binding_ids",
}
PROVIDER_BINDING_FIELDS = {
    "provider_binding_id",
    "model_registry_id",
    "model_registry_name",
    "model_registry_revision",
    "provider_locality",
    "transport_target",
    "transport_target_sha256",
    "upstream_provider_locality",
    "upstream_transport_target",
    "upstream_transport_target_sha256",
    "egress_policy_id",
    "upstream_egress_policy_id",
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


class ProviderLocality(str, Enum):
    """Where the admitted provider actually executes.

    Locality is a property of the CSP model/provider row snapshot, never of a
    source callsite.  In particular, ``internal_shim`` is not equivalent to
    ``internal_isolated``: its upstream target and upstream locality are
    separately signed below.
    """

    EXTERNAL_GOVERNED = "external_governed"
    INTERNAL_ISOLATED = "internal_isolated"
    INTERNAL_SHIM = "internal_shim"
    UNCLASSIFIED = "unclassified"


def parse_provider_locality(value: Any, field: str = "provider_locality") -> str:
    if isinstance(value, ProviderLocality):
        value = value.value
    if not isinstance(value, str) or value not in PROVIDER_LOCALITIES:
        raise ModelGovernanceError(f"{field} is invalid")
    return value


def _normalise_dns_host(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ModelGovernanceError(f"{field} must be a canonical DNS host")
    if any(ord(char) < 0x21 or ord(char) == 0x7F for char in value):
        raise ModelGovernanceError(f"{field} contains ambiguous whitespace/control syntax")
    if "\\" in value or "%" in value:
        raise ModelGovernanceError(f"{field} contains ambiguous host syntax")
    try:
        ip = ipaddress.ip_address(value.strip("[]"))
    except ValueError:
        ip = None
    if ip is not None:
        return ip.compressed.lower()
    candidate = value[:-1] if value.endswith(".") else value
    # ``ipaddress`` deliberately rejects legacy IPv4 spellings such as
    # ``127.1``, ``0177.0.0.1`` and ``0x7f000001``.  Do not subsequently
    # relabel an all-numeric host as DNS: libc/socket implementations may
    # interpret it as an address, bypassing the external FQDN DNS contract.
    # Mixed-label names (for example ``model-127.vendor.example``) remain DNS.
    if candidate and all(
        AMBIGUOUS_NUMERIC_IPV4_COMPONENT_RE.fullmatch(label)
        for label in candidate.split(".")
    ):
        raise ModelGovernanceError(f"{field} contains ambiguous numeric IPv4 syntax")
    if not candidate or ".." in candidate or ":" in candidate:
        raise ModelGovernanceError(f"{field} must be a DNS host")
    try:
        labels = tuple(label.encode("idna").decode("ascii").lower() for label in candidate.split("."))
    except UnicodeError as exc:
        raise ModelGovernanceError(f"{field} is not valid IDNA") from exc
    if any(not label or len(label) > 63 for label in labels) or len(".".join(labels)) > 253:
        raise ModelGovernanceError(f"{field} is not a valid DNS host")
    if any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label) for label in labels):
        raise ModelGovernanceError(f"{field} is not a valid DNS host")
    return ".".join(labels)


def _format_transport_host(host: str) -> str:
    try:
        if ipaddress.ip_address(host).version == 6:
            return f"[{host}]"
    except ValueError:
        pass
    return host


def _host_is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _transport_canonical_string(
    *, scheme: str | None, host: str, port: int, port_mode: str, path: str
) -> str:
    host_text = _format_transport_host(host)
    if scheme is None:
        return f"{host_text}:{port}"
    default_port = TRANSPORT_DEFAULT_PORTS.get(scheme)
    port_text = "" if port_mode == "default" and port == default_port else f":{port}"
    return f"{scheme}://{host_text}{port_text}{path}"


def _parse_transport_target_string(
    value: Any,
    field: str,
    *,
    default_scheme: str | None = None,
) -> tuple[str | None, str, int, str, str]:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ModelGovernanceError(f"{field} must be a non-empty transport target")
    if any(ord(char) < 0x21 or ord(char) == 0x7F for char in value):
        raise ModelGovernanceError(f"{field} contains ambiguous whitespace/control syntax")
    if "\\" in value or "#" in value or "?" in value or "@" in value:
        raise ModelGovernanceError(f"{field} contains ambiguous URL syntax")
    has_scheme = "://" in value
    if has_scheme:
        try:
            parsed = urlsplit(value)
        except ValueError as exc:
            raise ModelGovernanceError(
                f"{field} has unsupported or ambiguous scheme"
            ) from exc
        scheme = parsed.scheme.casefold()
        if scheme not in TRANSPORT_SCHEMES or not parsed.netloc:
            raise ModelGovernanceError(f"{field} has unsupported or ambiguous scheme")
        if parsed.username is not None or parsed.password is not None:
            raise ModelGovernanceError(f"{field} must not contain userinfo")
        if parsed.query or parsed.fragment:
            raise ModelGovernanceError(f"{field} must not contain query or fragment")
        try:
            host_text = parsed.hostname
            explicit_port = parsed.port
        except ValueError as exc:
            raise ModelGovernanceError(f"{field} port must be numeric") from exc
        if host_text is None:
            raise ModelGovernanceError(f"{field} must contain a hostname")
        if explicit_port is None and parsed.netloc.endswith(":"):
            raise ModelGovernanceError(f"{field} port must be numeric")
        host = _normalise_dns_host(host_text, f"{field}.host")
        default_port = TRANSPORT_DEFAULT_PORTS.get(scheme)
        if explicit_port is None and default_port is None:
            raise ModelGovernanceError(f"{field} must include a numeric port")
        port = explicit_port if explicit_port is not None else default_port
        if port is None:  # defensive guard for future transport schemes
            raise ModelGovernanceError(f"{field} must include a numeric port")
        if not 1 <= port <= 65535:
            raise ModelGovernanceError(f"{field} port must be between 1 and 65535")
        port_mode = (
            "default"
            if default_port is not None and port == default_port
            else "explicit_nondefault"
        )
        path = parsed.path or ""
        if path and (not path.startswith("/") or "//" in path):
            raise ModelGovernanceError(f"{field} path is ambiguous")
        return scheme, host, port, port_mode, path

    # A bare FQDN has no protocol semantics by itself.  Only an explicit
    # external-provider context (or the equivalent production DNS policy used
    # by the CSP request adapters) may assign HTTPS/443 to it.  Generic
    # internal targets must continue to provide an exact host:port instead of
    # guessing a protocol.
    if default_scheme is not None:
        default_scheme = default_scheme.casefold()
        if default_scheme not in TRANSPORT_SCHEMES:
            raise ModelGovernanceError(f"{field}.default_scheme is invalid")
        if default_scheme not in {"https", "grpcs"}:
            raise ModelGovernanceError(
                f"{field}.default_scheme must have a safe default port"
            )

    # Bare Docker/Kubernetes/gRPC targets normally carry an explicit port.
    if value.startswith("//"):
        raise ModelGovernanceError(f"{field} must use a scheme for URL targets")
    try:
        parsed = urlsplit(f"//{value}")
    except ValueError as exc:
        raise ModelGovernanceError(f"{field} must be an exact host:port target") from exc
    if parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ModelGovernanceError(f"{field} must be an exact host:port target")
    host_text = parsed.hostname
    if host_text is None:
        raise ModelGovernanceError(f"{field} must contain a hostname")
    host = _normalise_dns_host(host_text, f"{field}.host")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ModelGovernanceError(f"{field} port must be numeric") from exc
    if port is None and parsed.netloc.endswith(":"):
        raise ModelGovernanceError(f"{field} port must be numeric")
    if port is None:
        if default_scheme is None or _host_is_ip_literal(host) or not _is_fqdn_hostname(host):
            raise ModelGovernanceError(f"{field} must include a numeric port")
        default_port = TRANSPORT_DEFAULT_PORTS[default_scheme]
        return default_scheme, host, default_port, "default", ""
    if not 1 <= port <= 65535:
        raise ModelGovernanceError(f"{field} must include a numeric port")
    return None, host, port, "explicit_nondefault", ""


@dataclass(frozen=True, slots=True)
class TransportTarget:
    """Canonical, hashable transport target used by provider bindings."""

    scheme: str | None
    host: str
    port: int
    port_mode: str
    path: str
    dns_policy: str

    @classmethod
    def parse(
        cls,
        value: Any,
        *,
        dns_policy: str = "none",
        field: str = "transport_target",
        default_scheme: str | None = None,
        provider_locality: str | ProviderLocality | None = None,
    ) -> "TransportTarget":
        if dns_policy not in DNS_POLICIES:
            raise ModelGovernanceError(f"{field}.dns_policy is invalid")
        if isinstance(provider_locality, ProviderLocality):
            provider_locality = provider_locality.value
        if provider_locality is not None:
            provider_locality = parse_provider_locality(
                provider_locality, f"{field}.provider_locality"
            )
            if provider_locality == ProviderLocality.EXTERNAL_GOVERNED.value:
                default_scheme = default_scheme or "https"
        # The CSP request adapters already pin production DNS for external
        # providers. Treat that policy as explicit external context for bare
        # FQDN compatibility; ``dns_policy=none`` never guesses.
        if default_scheme is None and dns_policy == "production_fail_closed":
            default_scheme = "https"
        scheme, host, port, port_mode, path = _parse_transport_target_string(
            value,
            field,
            default_scheme=default_scheme,
        )
        return cls(scheme, host, port, port_mode, path, dns_policy)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, field: str = "transport_target") -> "TransportTarget":
        expected = {"scheme", "host", "port", "port_mode", "path", "dns_policy"}
        if not isinstance(value, Mapping) or set(value) != expected:
            raise ModelGovernanceError(f"{field} has unknown or missing fields")
        scheme = value["scheme"]
        if scheme is not None and (not isinstance(scheme, str) or scheme.casefold() not in TRANSPORT_SCHEMES):
            raise ModelGovernanceError(f"{field}.scheme is invalid")
        host = _normalise_dns_host(value["host"], f"{field}.host")
        port = value["port"]
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ModelGovernanceError(f"{field}.port must be numeric")
        port_mode = value["port_mode"]
        if port_mode not in TRANSPORT_PORT_MODES:
            raise ModelGovernanceError(f"{field}.port_mode is invalid")
        path = value["path"]
        if (
            not isinstance(path, str)
            or (path and (not path.startswith("/") or "//" in path))
            or any(c in path for c in "?#\\")
        ):
            raise ModelGovernanceError(f"{field}.path is invalid")
        dns_policy = value["dns_policy"]
        if dns_policy not in DNS_POLICIES:
            raise ModelGovernanceError(f"{field}.dns_policy is invalid")
        normalized_scheme = scheme.casefold() if isinstance(scheme, str) else None
        default_port = TRANSPORT_DEFAULT_PORTS.get(normalized_scheme)
        expected_port_mode = (
            "default"
            if default_port is not None and port == default_port
            else "explicit_nondefault"
        )
        if port_mode != expected_port_mode:
            raise ModelGovernanceError(f"{field}.port_mode is not canonical")
        target = cls(
            normalized_scheme,
            host,
            port,
            port_mode,
            path,
            dns_policy,
        )
        if value != target.to_dict():
            raise ModelGovernanceError(f"{field} is not canonical")
        parsed = cls.parse(target.canonical, dns_policy=dns_policy, field=field)
        if parsed != target:
            raise ModelGovernanceError(f"{field} is not canonical")
        return target

    @classmethod
    def from_value(
        cls,
        value: Any,
        *,
        field: str = "transport_target",
        dns_policy: str = "none",
        default_scheme: str | None = None,
        provider_locality: str | ProviderLocality | None = None,
    ) -> "TransportTarget":
        if isinstance(value, Mapping):
            return cls.from_dict(value, field=field)
        return cls.parse(
            value,
            dns_policy=dns_policy,
            field=field,
            default_scheme=default_scheme,
            provider_locality=provider_locality,
        )

    @property
    def canonical(self) -> str:
        return _transport_canonical_string(
            scheme=self.scheme,
            host=self.host,
            port=self.port,
            port_mode=self.port_mode,
            path=self.path,
        )

    @property
    def canonical_target(self) -> str:
        return self.canonical

    def to_dict(self) -> dict[str, Any]:
        return {
            "scheme": self.scheme,
            "host": self.host,
            "port": self.port,
            "port_mode": self.port_mode,
            "path": self.path,
            "dns_policy": self.dns_policy,
        }

    @property
    def sha256(self) -> str:
        return hashlib.sha256(canonical_json(self.to_dict())).hexdigest()

    @property
    def canonical_hash(self) -> str:
        return self.sha256


def transport_target_sha256(value: TransportTarget | Mapping[str, Any] | str) -> str:
    target = value if isinstance(value, TransportTarget) else TransportTarget.from_value(value)
    return target.sha256


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


def _is_fqdn_hostname(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return "." in host and not host.endswith(".")
    return False


def _validate_external_target(
    target: TransportTarget,
    *,
    egress_policy_id: str | None,
    field: str,
) -> None:
    """Validate the transport/DNS half of an externally governed edge.

    The signed egress policy is the authority for every external transport,
    including plaintext HTTP/gRPC/TCP and exact IP literals.  DNS names keep
    production fail-closed resolution so runtime probes re-resolve all A/AAAA
    records instead of pinning one address.
    """

    if not egress_policy_id:
        raise ModelGovernanceError(f"{field} requires egress_policy_id")
    if _host_is_ip_literal(target.host):
        if target.dns_policy not in {"none", "exact_ip"}:
            raise ModelGovernanceError(
                f"{field} IP literal requires dns_policy=none or exact_ip"
            )
        return
    if not _is_fqdn_hostname(target.host):
        raise ModelGovernanceError(f"{field} requires an external FQDN or IP literal")
    if target.dns_policy != "production_fail_closed":
        raise ModelGovernanceError(
            f"{field} hostname requires dns_policy=production_fail_closed"
        )


def _provider_target_from_value(
    value: Any,
    *,
    locality: str | None,
    field: str,
) -> TransportTarget:
    if isinstance(value, Mapping):
        return TransportTarget.from_dict(value, field=field)
    if isinstance(value, str):
        dns_policy = (
            "production_fail_closed"
            if locality == ProviderLocality.EXTERNAL_GOVERNED.value
            else "none"
        )
        target = TransportTarget.parse(
            value,
            dns_policy=dns_policy,
            field=field,
            provider_locality=locality,
        )
        # Raw external IP targets have no DNS to fail closed.  The provider
        # context still supplies HTTPS for a bare FQDN, but exact IP forms are
        # canonicalized to the explicit no-DNS policy before their hash is
        # checked.
        if locality == ProviderLocality.EXTERNAL_GOVERNED.value and _host_is_ip_literal(
            target.host
        ):
            target = TransportTarget(
                target.scheme,
                target.host,
                target.port,
                target.port_mode,
                target.path,
                "none",
            )
        return target
    raise ModelGovernanceError(f"{field} must be a transport target string or object")


@dataclass(frozen=True, slots=True)
class ProviderBinding:
    """Signed snapshot of one admitted model-registry provider row.

    The snapshot carries provider locality and both sides of a shim edge.  A
    callsite may only reference this entity by id; it cannot manufacture a
    locality from static source metadata.
    """

    provider_binding_id: str
    model_registry_id: str
    model_registry_name: str
    model_registry_revision: str
    provider_locality: str
    transport_target: TransportTarget
    transport_target_sha256: str
    upstream_provider_locality: str | None
    upstream_transport_target: TransportTarget | None
    upstream_transport_target_sha256: str | None
    egress_policy_id: str | None
    upstream_egress_policy_id: str | None
    model_artifact_id: str
    deployment_id: str

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, field: str = "provider_binding") -> "ProviderBinding":
        if not isinstance(value, Mapping) or set(value) != PROVIDER_BINDING_FIELDS:
            raise ModelGovernanceError(f"{field} has unknown or missing fields")
        locality = parse_provider_locality(value["provider_locality"], f"{field}.provider_locality")
        target = _provider_target_from_value(
            value["transport_target"],
            locality=locality,
            field=f"{field}.transport_target",
        )
        target_hash = require_hex_sha256(value["transport_target_sha256"], f"{field}.transport_target_sha256")
        if target_hash != target.sha256:
            raise ModelGovernanceError(f"{field}.transport_target_sha256 mismatch")

        upstream_locality_value = value["upstream_provider_locality"]
        upstream_locality = (
            None
            if upstream_locality_value is None
            else parse_provider_locality(
                upstream_locality_value, f"{field}.upstream_provider_locality"
            )
        )
        upstream_target_value = value["upstream_transport_target"]
        upstream_target = (
            None
            if upstream_target_value is None
            else _provider_target_from_value(
                upstream_target_value,
                locality=upstream_locality,
                field=f"{field}.upstream_transport_target",
            )
        )
        upstream_hash_value = value["upstream_transport_target_sha256"]
        upstream_hash = (
            None
            if upstream_hash_value is None
            else require_hex_sha256(
                upstream_hash_value, f"{field}.upstream_transport_target_sha256"
            )
        )
        if upstream_target is None:
            if upstream_locality is not None or upstream_hash is not None:
                raise ModelGovernanceError(f"{field} upstream target/locality must be paired")
        elif upstream_hash != upstream_target.sha256:
            raise ModelGovernanceError(f"{field}.upstream_transport_target_sha256 mismatch")

        egress_value = value["egress_policy_id"]
        egress_policy_id = (
            None
            if egress_value is None
            else require_identifier(egress_value, f"{field}.egress_policy_id")
        )
        upstream_egress_value = value["upstream_egress_policy_id"]
        upstream_egress_policy_id = (
            None
            if upstream_egress_value is None
            else require_identifier(upstream_egress_value, f"{field}.upstream_egress_policy_id")
        )

        if locality == ProviderLocality.EXTERNAL_GOVERNED.value:
            _validate_external_target(
                target,
                egress_policy_id=egress_policy_id,
                field=f"{field}.external_governed",
            )
            if (
                upstream_target is not None
                or upstream_locality is not None
                or upstream_egress_policy_id is not None
            ):
                raise ModelGovernanceError(f"{field}.external_governed cannot carry shim upstream")
        elif locality == ProviderLocality.INTERNAL_SHIM.value:
            if upstream_target is None or upstream_locality is None:
                raise ModelGovernanceError(
                    f"{field}.internal_shim requires upstream provider locality and target"
                )
            if upstream_locality == ProviderLocality.INTERNAL_SHIM.value:
                raise ModelGovernanceError(f"{field}.internal_shim cannot nest another shim")
            if upstream_locality == ProviderLocality.EXTERNAL_GOVERNED.value:
                _validate_external_target(
                    upstream_target,
                    egress_policy_id=upstream_egress_policy_id,
                    field=f"{field}.external upstream",
                )
            elif upstream_egress_policy_id is not None:
                raise ModelGovernanceError(
                    f"{field}.non-external upstream cannot carry egress policy"
                )
            if egress_policy_id is not None:
                raise ModelGovernanceError(f"{field}.internal_shim cannot claim upstream egress policy")
        elif locality in {
            ProviderLocality.INTERNAL_ISOLATED.value,
            ProviderLocality.UNCLASSIFIED.value,
        }:
            if upstream_target is not None or upstream_locality is not None:
                raise ModelGovernanceError(f"{field} non-shim locality cannot carry upstream target")
            if egress_policy_id is not None or upstream_egress_policy_id is not None:
                raise ModelGovernanceError(f"{field} non-external locality cannot carry egress policy")

        return cls(
            provider_binding_id=require_identifier(value["provider_binding_id"], f"{field}.provider_binding_id"),
            model_registry_id=require_identifier(value["model_registry_id"], f"{field}.model_registry_id"),
            model_registry_name=require_string(value["model_registry_name"], f"{field}.model_registry_name", max_length=200),
            model_registry_revision=require_string(value["model_registry_revision"], f"{field}.model_registry_revision", max_length=256),
            provider_locality=locality,
            transport_target=target,
            transport_target_sha256=target_hash,
            upstream_provider_locality=upstream_locality,
            upstream_transport_target=upstream_target,
            upstream_transport_target_sha256=upstream_hash,
            egress_policy_id=egress_policy_id,
            upstream_egress_policy_id=upstream_egress_policy_id,
            model_artifact_id=require_identifier(value["model_artifact_id"], f"{field}.model_artifact_id"),
            deployment_id=require_identifier(value["deployment_id"], f"{field}.deployment_id"),
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
    # Routing/resolution mode is source evidence only.  Provider locality is
    # intentionally absent and must come from a signed ProviderBinding.
    provider_resolution: str = "direct_raw"

    @property
    def classification_level(self) -> str:
        """Return the ceiling through the canonical contracts enum."""

        return parse_classification_level(self.classification_ceiling)

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
        *,
        field: str = "callsite",
        schema_version: str = GOVERNANCE_INVENTORY_SCHEMA_V1,
    ) -> "InferenceCallsite":
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
        is_v2 = schema_version == GOVERNANCE_INVENTORY_SCHEMA_V2
        if is_v2:
            expected = expected | {"provider_resolution"}
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
        resolution = value.get("provider_resolution")
        if resolution is None:
            resolution = "direct_raw" if raw_endpoint else "gateway_dynamic"
        if resolution not in PROVIDER_RESOLUTION_MODES:
            raise ModelGovernanceError(f"{field}.provider_resolution is invalid")
        if resolution == "direct_raw" and (not raw_endpoint or gateway is not None):
            raise ModelGovernanceError(
                f"{field}.direct_raw resolution must be an unbound raw endpoint"
            )
        if resolution == "gateway_dynamic" and (raw_endpoint or gateway != GATEWAY_ID):
            raise ModelGovernanceError(
                f"{field}.gateway_dynamic resolution must use the CSP gateway"
            )
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
            provider_resolution=resolution,
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
    # v1 fields remain available to direct constructor callers while v2
    # payloads must use the provider binding allow-list below.
    model_artifact_id: str | None = None
    deployment_id: str | None = None
    provider_binding_ids: tuple[str, ...] = ()

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
        *,
        field: str = "callsite_binding",
        schema_version: str = GOVERNANCE_PROFILE_SCHEMA_V1,
    ) -> "CallsiteBinding":
        is_v2 = schema_version == GOVERNANCE_PROFILE_SCHEMA_V2
        expected = BINDING_FIELDS_V2 if is_v2 else BINDING_FIELDS_V1
        if not isinstance(value, Mapping) or set(value) != expected:
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
        provider_ids: tuple[str, ...] = ()
        artifact_id: str | None = None
        deployment_id: str | None = None
        if is_v2:
            provider_ids = require_unique_strings(
                value["provider_binding_ids"], f"{field}.provider_binding_ids", allow_empty=False
            )
            if provider_ids != tuple(sorted(provider_ids)):
                raise ModelGovernanceError(f"{field}.provider_binding_ids must be sorted")
        else:
            artifact_id = require_identifier(value["model_artifact_id"], f"{field}.model_artifact_id")
            deployment_id = require_identifier(value["deployment_id"], f"{field}.deployment_id")
        return cls(
            callsite_id=require_identifier(value["callsite_id"], f"{field}.callsite_id"),
            gateway_id=gateway,
            classification_ceiling=ceiling,
            usage_sink=usage,
            audit_sink=audit,
            agent_scope=require_unique_strings(value["agent_scope"], f"{field}.agent_scope"),
            model_artifact_id=artifact_id,
            deployment_id=deployment_id,
            provider_binding_ids=provider_ids,
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
    provider_binding: ProviderBinding | None = None


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
    schema_version = inventory.get("schema_version")
    if schema_version not in {GOVERNANCE_INVENTORY_SCHEMA_V1, GOVERNANCE_INVENTORY_SCHEMA_V2}:
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
        call = InferenceCallsite.from_dict(
            raw,
            field=f"callsites[{index}]",
            schema_version=schema_version,
        )
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
    if not isinstance(profile, Mapping):
        raise ModelGovernanceError("profile has unknown or missing fields")
    schema_version = profile.get("schema_version")
    expected_fields = PROFILE_FIELDS_V2 if schema_version == GOVERNANCE_PROFILE_SCHEMA_V2 else PROFILE_FIELDS
    if set(profile) != expected_fields:
        raise ModelGovernanceError("profile has unknown or missing fields")
    if schema_version not in {GOVERNANCE_PROFILE_SCHEMA_V1, GOVERNANCE_PROFILE_SCHEMA_V2}:
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
    if schema_version == GOVERNANCE_PROFILE_SCHEMA_V2 and not isinstance(
        profile.get("provider_bindings"), list
    ):
        raise ModelGovernanceError("provider_bindings must be a list")
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
    inventory_schema_version: str,
    now: datetime,
) -> tuple[
    bool,
    tuple[str, ...],
    tuple[str, ...],
    dict[str, CallsiteBinding],
    dict[str, ProviderBinding],
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
        provider_values = profile.get("provider_bindings", [])
        if (
            profile["callsite_bindings"]
            or provider_values
            or profile["model_artifacts"]
            or profile["deployments"]
        ):
            raise ModelGovernanceError("disabled profile cannot carry model authority")
        if profile["valid_from"] is not None or profile["valid_until"] is not None:
            raise ModelGovernanceError("disabled profile cannot carry validity")
        if profile["approvers"] or profile["signatures"]:
            raise ModelGovernanceError("disabled profile cannot carry approvers/signatures")
        return False, enabled, disabled, {}, {}, {}, {}, None, None

    if profile.get("schema_version") != GOVERNANCE_PROFILE_SCHEMA_V2:
        raise ModelGovernanceError(
            "enabled production profile requires model-governance profile v2"
        )
    if inventory_schema_version != GOVERNANCE_INVENTORY_SCHEMA_V2:
        raise ModelGovernanceError(
            "enabled production profile requires model-governance inventory v2"
        )

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

    provider_bindings: dict[str, ProviderBinding] = {}
    for index, raw in enumerate(profile.get("provider_bindings", [])):
        provider_binding = ProviderBinding.from_dict(
            raw, field=f"provider_bindings[{index}]"
        )
        if provider_binding.provider_binding_id in provider_bindings:
            raise ModelGovernanceError(
                f"duplicate provider binding {provider_binding.provider_binding_id}"
            )
        if provider_binding.model_artifact_id not in artifacts:
            raise ModelGovernanceError(
                f"provider binding {provider_binding.provider_binding_id} references unknown artifact"
            )
        if provider_binding.deployment_id not in deployments:
            raise ModelGovernanceError(
                f"provider binding {provider_binding.provider_binding_id} references unknown deployment"
            )
        deployment = deployments[provider_binding.deployment_id]
        if deployment.artifact_id != provider_binding.model_artifact_id:
            raise ModelGovernanceError(
                f"provider binding {provider_binding.provider_binding_id} artifact mismatch"
            )
        if provider_binding.provider_locality == ProviderLocality.UNCLASSIFIED.value:
            raise ModelGovernanceError(
                f"provider binding {provider_binding.provider_binding_id} has unclassified locality"
            )
        if (
            provider_binding.upstream_provider_locality
            == ProviderLocality.UNCLASSIFIED.value
        ):
            raise ModelGovernanceError(
                f"provider binding {provider_binding.provider_binding_id} has unclassified upstream locality"
            )
        provider_bindings[provider_binding.provider_binding_id] = provider_binding

    bindings: dict[str, CallsiteBinding] = {}
    for index, raw in enumerate(profile["callsite_bindings"]):
        binding = CallsiteBinding.from_dict(
            raw,
            field=f"callsite_bindings[{index}]",
            schema_version=profile["schema_version"],
        )
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
        if not binding.provider_binding_ids:
            raise ModelGovernanceError(f"binding {call_id} has no provider binding ids")
        for provider_binding_id in binding.provider_binding_ids:
            if provider_binding_id not in provider_bindings:
                raise ModelGovernanceError(
                    f"binding {call_id} references unknown provider binding"
                )
    referenced_provider_ids = {
        provider_binding_id
        for binding in bindings.values()
        for provider_binding_id in binding.provider_binding_ids
    }
    if referenced_provider_ids != set(provider_bindings):
        raise ModelGovernanceError("provider_bindings must be referenced exactly by enabled callsites")
    return (
        True,
        enabled,
        disabled,
        bindings,
        provider_bindings,
        artifacts,
        deployments,
        valid_from,
        valid_until,
    )


_AUTHORITY_CONSTRUCTOR_TOKEN = object()

# ``agent_scope`` is deliberately a small selector language, not a list of
# values that are all compared as literal Agent ids.  The inventory uses this
# reserved selector for a callsite available to any *verified registered*
# Agent.  Concrete values remain exact canonical Agent ids for future profiles.
# Keep the selector out of the canonical-id namespace so a caller cannot pass
# the sentinel itself and accidentally satisfy a scoped binding.
REGISTERED_AGENT_SCOPE = "registered-agent"
_AGENT_SCOPE_SELECTORS = frozenset({REGISTERED_AGENT_SCOPE})


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
    provider_bindings: Mapping[str, ProviderBinding]
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
        provider_bindings: Mapping[str, ProviderBinding] | None = None,
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
        object.__setattr__(
            self,
            "provider_bindings",
            MappingProxyType(dict(provider_bindings or {})),
        )
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
            provider_bindings,
            artifacts,
            deployments,
            valid_from,
            valid_until,
        ) = _verified_profile_parts(
            profile=profile,
            calls=calls,
            inventory_schema_version=inventory["schema_version"],
            now=current,
        )
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
            provider_bindings=provider_bindings,
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
        provider_binding_id: str | None = None,
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
        provider_binding: ProviderBinding | None = None
        if binding.provider_binding_ids:
            if provider_binding_id is None:
                raise ModelGovernanceError("provider binding identity is required")
            require_identifier(provider_binding_id, "provider_binding_id")
            if provider_binding_id not in binding.provider_binding_ids:
                raise ModelGovernanceError("provider binding is not admitted for this callsite")
            provider_binding = self.provider_bindings.get(provider_binding_id)
            if provider_binding is None:
                raise ModelGovernanceError("provider binding is not present in authority")
            if provider_binding.model_artifact_id != model_artifact_id:
                raise ModelGovernanceError("provider binding artifact mismatch")
            if provider_binding.deployment_id != deployment_id:
                raise ModelGovernanceError("provider binding deployment mismatch")
        artifact = self.model_artifacts.get(model_artifact_id)
        deployment = self.deployments.get(deployment_id)
        if artifact is None or (
            not binding.provider_binding_ids and model_artifact_id != binding.model_artifact_id
        ):
            raise ModelGovernanceError("model artifact is not admitted for this callsite")
        if deployment is None or (
            not binding.provider_binding_ids and deployment_id != binding.deployment_id
        ):
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
        if requested_agent in _AGENT_SCOPE_SELECTORS:
            # ``registered-agent`` is a category selector, never a real
            # canonical id.  Treating the literal sentinel as an identity
            # would turn a missing/forged caller context into an admission.
            raise ModelGovernanceError("agent scope selector cannot be used as an agent id")
        if binding.agent_scope:
            if requested_agent is None:
                raise ModelGovernanceError("agent is outside the admitted agent scope")
            exact_scopes = set(binding.agent_scope) - _AGENT_SCOPE_SELECTORS
            registered_scope = REGISTERED_AGENT_SCOPE in binding.agent_scope
            if not (
                (registered_scope and isinstance(requested_agent, str) and requested_agent.strip())
                or (requested_agent in exact_scopes)
            ):
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
            provider_binding=provider_binding,
        )


__all__ = [
    "CLASSIFICATION_LEVELS",
    "CLASSIFICATION_ORDER",
    "CallsiteBinding",
    "GOVERNANCE_INVENTORY_SCHEMA_V1",
    "GOVERNANCE_INVENTORY_SCHEMA_V2",
    "GOVERNANCE_PROFILE_SCHEMA_V1",
    "GOVERNANCE_PROFILE_SCHEMA_V2",
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
    "ProviderBinding",
    "ProviderLocality",
    "PROVIDER_LOCALITIES",
    "PROVIDER_RESOLUTION_MODES",
    "TransportTarget",
    "TRANSPORT_PORT_MODES",
    "parse_provider_locality",
    "transport_target_sha256",
    "Deployment",
    "InferenceCallsite",
    "REQUIRED_APPROVER_ROLES",
    "REGISTERED_AGENT_SCOPE",
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
