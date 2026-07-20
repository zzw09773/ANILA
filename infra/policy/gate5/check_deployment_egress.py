#!/usr/bin/env python3
"""Fail-closed deployment checks for Gate 5 model egress.

This checker deliberately consumes *resolved* Compose JSON rather than the
source YAML.  Interpolation, profiles, extension fields and mount modes must
be checked after Compose has resolved them; looking at source text is not a
network boundary.  The checker is static and side-effect free.  It does not
start containers and it never treats a development profile as production.

The formal invariant is intentionally small:

* CSP is the only platform service allowed on ``anila-models-net``.
* Formal agents never receive a raw model URL or an arbitrary
  ``ANILA_BASE_URL``; model calls are addressed to CSP.
* CSP governance material is explicit, read-only, complete and present.
* FLUX is absent from the default/formal service set until a named legal
  profile and a signed governance binding are supplied.

The command accepts more than one resolved Compose document.  Pass the
platform document first and the independent model-stack document second:

    python infra/policy/gate5/check_deployment_egress.py \
      --profile prod-intranet-card \
      --compose-json /tmp/platform.json \
      --compose-json /tmp/models.json

Development is intentionally still testable.  ``--profile development``
reports the resolved topology but does not apply formal restrictions; this is
not an approval for a production deployment.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit


# The deployment entrypoint invokes this checker with the system interpreter,
# not with an installed editable package.  Keep the signed-authority import
# usable from the repository checkout while retaining a dependency-free path
# for topology-only development checks.
_REPO_HINT = Path(__file__).resolve().parents[3]
_SECURITY_SRC = _REPO_HINT / "packages/anila-security/src"
if _SECURITY_SRC.is_dir() and str(_SECURITY_SRC) not in sys.path:
    sys.path.insert(0, str(_SECURITY_SRC))


class DeploymentEgressError(RuntimeError):
    """Raised when a resolved deployment violates a fail-closed invariant."""


# ``prod-*`` profiles are formal by syntax.  Reduction/demo identities such as
# ``trial-military`` do not share that prefix, so keep them in this explicit
# set; otherwise the deployment checker would silently return its development
# success result and skip every Gate 5 network/governance invariant.
FORMAL_PROFILES = frozenset({"prod", "production", "trial-military"})
MODEL_NETWORK_TOKENS = ("model", "inference")
MODEL_NETWORK_LEXICAL_TOKENS = frozenset({"donkernet"})
SHARED_MODEL_NETWORK_NAME = "anila-models-net"
EXTERNAL_SHIM_UPSTREAM_EGRESS_POLICY_LABEL = (
    "com.anila.upstream-egress-policy-id"
)
GOVERNANCE_TARGETS = {
    "GATE5_MODEL_GOVERNANCE_INVENTORY_PATH": "/etc/anila/governance/inventory.json",
    "GATE5_MODEL_GOVERNANCE_PROFILE_PATH": "/etc/anila/governance/profile.json",
    "GATE5_MODEL_GOVERNANCE_TRUST_STORE_PATH": "/etc/anila/governance/trust-store.json",
    "GATE5_MODEL_GOVERNANCE_OBSERVED_FACTS_PATH": "/etc/anila/governance/observed-facts.json",
}
# Keep this list limited to model/inference endpoints.  A generic ``*_BASE_URL``
# key is not by itself an inference sink (for example Studio's
# ``RENDERER_BASE_URL`` is a service-to-service artifact renderer), so banning
# every key ending in ``BASE_URL`` would create a false positive while failing
# to express the actual Gate 5 boundary.
RAW_ENDPOINT_KEYS = re.compile(
    r"(?:^ANILA_BASE_URL$|^(?:LOCAL_LLM|LOCAL_EMBEDDING|MODEL|VISION|RELATION_LLM|EMBEDDING|TRITON|FLUX)_.*URL$|^.*_MODEL_URL$|^FLUX_BACKEND_URL$)"
)
AGENT_NAME = re.compile(r"(?:^|[-_])agent(?:$|[-_])", re.IGNORECASE)
CSP_GATEWAY_HOSTS = frozenset({"csp", "csp-model-gateway"})
# Keep this pure-syntax rule equivalent to anila-security's transport target
# parser.  ``ipaddress`` rejects legacy IPv4 spellings that libc/socket may
# still resolve as an address; they must not subsequently be accepted as DNS.
AMBIGUOUS_NUMERIC_IPV4_COMPONENT_RE = re.compile(r"(?:[0-9]+|0[xX][0-9A-Fa-f]+)$")


def _as_bool(value: object) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _is_formal(profile: str) -> bool:
    normalized = profile.strip().lower()
    return normalized in FORMAL_PROFILES or normalized.startswith("prod-")


def _services(document: Mapping[str, Any]) -> Mapping[str, Any]:
    services = document.get("services")
    if not isinstance(services, Mapping):
        raise DeploymentEgressError("resolved compose document has no services map")
    return services


def _environment(service: Mapping[str, Any]) -> dict[str, str]:
    value = service.get("environment", {})
    if isinstance(value, Mapping):
        return {str(key): "" if item is None else str(item) for key, item in value.items()}
    if isinstance(value, list):
        result: dict[str, str] = {}
        for item in value:
            if not isinstance(item, str) or "=" not in item:
                raise DeploymentEgressError("resolved service environment entry is malformed")
            key, item_value = item.split("=", 1)
            result[key] = item_value
        return result
    raise DeploymentEgressError("resolved service environment must be a map or list")


def _networks(service: Mapping[str, Any]) -> set[str]:
    value = service.get("networks", {})
    if isinstance(value, Mapping):
        return {str(key) for key in value}
    if isinstance(value, list):
        result: set[str] = set()
        for item in value:
            if isinstance(item, str):
                result.add(item)
            elif isinstance(item, Mapping) and "target" in item:
                result.add(str(item["target"]))
            else:
                raise DeploymentEgressError("resolved service network entry is malformed")
        return result
    raise DeploymentEgressError("resolved service networks must be a map or list")


def _network_effective_name(
    name: str,
    document: Mapping[str, Any],
) -> str | None:
    """Return the Docker network identity from resolved Compose JSON.

    Logical network keys are document-local aliases.  Cross-document
    isolation decisions must use the resolved ``name`` that Docker receives;
    otherwise two different aliases can silently attach to the same bridge.
    The project fallback is retained for small pure fixtures, although normal
    ``docker compose config --format json`` output always supplies ``name``.
    """

    networks = document.get("networks")
    declared = networks.get(name) if isinstance(networks, Mapping) else None
    if not isinstance(declared, Mapping):
        return None
    effective = str(declared.get("name", "")).strip()
    if effective:
        return effective
    if declared.get("external") is True:
        return None
    project = str(document.get("name", "")).strip()
    if not project:
        return None
    return f"{project}_{name}"


def _is_model_network(name: str, document: Mapping[str, Any]) -> bool:
    """Return whether a document's model network is admitted by Gate 5.

    The shared model network has no Compose owner, so a resolved document must
    describe it as the exact external ``anila-models-net`` instead of
    pretending that the document owns ``internal: true``.  Any document-owned
    model/inference network remains required to be an internal network.
    """

    networks = document.get("networks")
    declared = networks.get(name) if isinstance(networks, Mapping) else None
    if not isinstance(declared, Mapping):
        return False
    effective = _network_effective_name(name, document) or ""
    if declared.get("external") is True:
        return effective == SHARED_MODEL_NETWORK_NAME
    if declared.get("internal") is not True:
        return False
    return _network_matches_model_tokens(name, document)


def _network_matches_model_tokens(name: str, document: Mapping[str, Any]) -> bool:
    effective = _network_effective_name(name, document) or ""
    values = (name.casefold(), effective.casefold())
    if any(token in value for token in MODEL_NETWORK_TOKENS for value in values):
        return True
    lexical_tokens = {
        token
        for value in values
        for token in re.findall(r"[a-z0-9]+", value)
    }
    return not lexical_tokens.isdisjoint(MODEL_NETWORK_LEXICAL_TOKENS)


def _is_external_shared_model_network(name: str, document: Mapping[str, Any]) -> bool:
    networks = document.get("networks")
    declared = networks.get(name) if isinstance(networks, Mapping) else None
    return (
        isinstance(declared, Mapping)
        and declared.get("external") is True
        and _network_effective_name(name, document) == SHARED_MODEL_NETWORK_NAME
    )


def _service_effective_networks(
    service: Mapping[str, Any], document: Mapping[str, Any]
) -> set[str] | None:
    """Resolve every network a service joins to its Docker identity.

    A Compose network key is only local to one document.  Returning ``None``
    for an unresolved key makes the external-shim topology fail closed rather
    than silently treating its local alias as an isolated bridge.
    """

    effective_networks: set[str] = set()
    for logical_name in _networks(service):
        effective_name = _network_effective_name(logical_name, document)
        if effective_name is None:
            return None
        effective_networks.add(effective_name)
    return effective_networks


def _effective_network_attachers(
    documents: list[Mapping[str, Any]], *, effective_network: str
) -> list[tuple[int, str]]:
    """Return every service attached to a Docker network across documents."""

    attachers: list[tuple[int, str]] = []
    for document_index, candidate_document in enumerate(documents):
        for candidate_name, candidate in _services(candidate_document).items():
            if not isinstance(candidate, Mapping):
                continue
            effective_networks = _service_effective_networks(candidate, candidate_document)
            if effective_networks is not None and effective_network in effective_networks:
                attachers.append((document_index, candidate_name))
    return attachers


def _mounts(service: Mapping[str, Any]) -> list[dict[str, Any]]:
    value = service.get("volumes", [])
    if not isinstance(value, list):
        raise DeploymentEgressError("resolved service volumes must be a list")
    result: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, Mapping):
            target = item.get("target")
            if not isinstance(target, str):
                raise DeploymentEgressError("resolved volume is missing target")
            result.append(dict(item))
            continue
        if isinstance(item, str):
            # Compose JSON normally uses mapping entries.  Supporting the
            # short syntax keeps the pure checker useful with small fixtures.
            parts = item.split(":")
            if len(parts) < 2:
                raise DeploymentEgressError("resolved volume short syntax is malformed")
            mode = parts[2] if len(parts) > 2 else ""
            result.append(
                {
                    "source": parts[0],
                    "target": parts[1],
                    "read_only": "ro" in mode.split(","),
                }
            )
            continue
        raise DeploymentEgressError("resolved volume entry is malformed")
    return result


def _ports(service: Mapping[str, Any]) -> list[object]:
    """Return resolved host-published ports, preserving malformed values."""

    value = service.get("ports", [])
    if value is None:
        return []
    if not isinstance(value, list):
        raise DeploymentEgressError("resolved service ports must be a list")
    return list(value)


def _service_labels(service: Mapping[str, Any]) -> dict[str, str]:
    value = service.get("labels", {})
    if isinstance(value, Mapping):
        return {str(key): str(item) for key, item in value.items()}
    if isinstance(value, list):
        result: dict[str, str] = {}
        for item in value:
            if isinstance(item, str) and "=" in item:
                key, item_value = item.split("=", 1)
                result[key] = item_value
        return result
    return {}


def _gateway_url(value: str) -> tuple[str, int | None, str]:
    try:
        parsed = urlsplit(value.strip())
        hostname = parsed.hostname
    except ValueError as exc:
        raise DeploymentEgressError("CSP gateway endpoint URL is invalid") from exc
    if parsed.scheme not in {"http", "https"} or not hostname:
        raise DeploymentEgressError("GATE5_MODEL_GATEWAY_ENDPOINT must be an absolute URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise DeploymentEgressError("CSP gateway endpoint must not contain credentials/query/fragment")
    path = parsed.path.rstrip("/")
    if not path.startswith("/v1"):
        raise DeploymentEgressError("CSP gateway endpoint must be rooted at /v1")
    try:
        port = parsed.port
    except ValueError as exc:
        raise DeploymentEgressError("CSP gateway endpoint port is invalid") from exc
    return hostname.lower(), port, path


def _host_is_csp(value: str, gateway: tuple[str, int | None, str]) -> bool:
    try:
        host, port, _ = _gateway_url(value)
    except DeploymentEgressError:
        return False
    _, gateway_port, _ = gateway
    return host in CSP_GATEWAY_HOSTS and (
        port is None or gateway_port is None or port == gateway_port
    )


def _csp_base_url(value: str, gateway: tuple[str, int | None, str]) -> bool:
    """Accept CSP service bases with or without the gateway's ``/v1`` path."""

    try:
        parsed = urlsplit(value.strip())
        hostname = parsed.hostname
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or not hostname:
        return False
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        return False
    path = parsed.path.rstrip("/")
    if path not in {"", "/v1"}:
        return False
    try:
        port = parsed.port
    except ValueError:
        return False
    _, gateway_port, _ = gateway
    return hostname.lower() in CSP_GATEWAY_HOSTS and (
        port is None or gateway_port is None or port == gateway_port
    )


def _raw_endpoint_violation(
    *, service_name: str, key: str, value: str, gateway: tuple[str, int | None, str]
) -> str | None:
    stripped = value.strip()
    if not stripped:
        return None
    if key == "CSP_BASE_URL":
        if not _csp_base_url(stripped, gateway):
            return f"{service_name}.{key} must point to CSP gateway"
        return None
    if not RAW_ENDPOINT_KEYS.search(key):
        return None
    if not _host_is_csp(stripped, gateway):
        return f"{service_name}.{key} is a raw/non-CSP model endpoint"
    return None


def _endpoint_host(value: str) -> str | None:
    candidate = value.strip()
    if not candidate:
        return None
    parsed_value = candidate if "://" in candidate else f"//{candidate}"
    try:
        parsed = urlsplit(parsed_value)
        hostname = parsed.hostname
    except ValueError:
        return None
    if not hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        return None
    return hostname.lower()


def _model_endpoint_violation(
    *,
    service_name: str,
    key: str,
    value: str,
    allowed_hosts: set[str],
    gateway: tuple[str, int | None, str],
) -> str | None:
    stripped = value.strip()
    if not stripped:
        return None
    if key == "CSP_BASE_URL":
        if not _csp_base_url(stripped, gateway):
            return f"{service_name}.{key} must point to CSP gateway"
        return None
    if not RAW_ENDPOINT_KEYS.search(key):
        return None
    if key == "TRITON_GRPC_URL" and _normalise_external_target(stripped) is None:
        return f"{service_name}.{key} must be a bare host:port model endpoint"
    host = _endpoint_host(stripped)
    if host not in allowed_hosts:
        return f"{service_name}.{key} is an external/unknown model endpoint"
    return None


def _normalise_external_target(value: str) -> str | None:
    candidate = value.strip()
    if not candidate or value != candidate or "://" in candidate or "\\" in candidate:
        return None
    try:
        parsed = urlsplit(f"//{candidate}")
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if (
        not hostname
        or port is None
        or not 1 <= port <= 65535
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        return None
    if parsed.netloc.endswith(":"):
        return None
    if "%" in hostname:
        return None
    try:
        host = ipaddress.ip_address(hostname).compressed.lower()
    except ValueError:
        candidate_host = hostname[:-1] if hostname.endswith(".") else hostname
        if candidate_host and all(
            AMBIGUOUS_NUMERIC_IPV4_COMPONENT_RE.fullmatch(label)
            for label in candidate_host.split(".")
        ):
            return None
        if not candidate_host or ".." in candidate_host or ":" in candidate_host:
            return None
        try:
            labels = tuple(
                label.encode("idna").decode("ascii").lower()
                for label in candidate_host.split(".")
            )
        except UnicodeError:
            return None
        if any(
            not label
            or len(label) > 63
            or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label)
            for label in labels
        ) or len(".".join(labels)) > 253:
            return None
        host = ".".join(labels)
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    host_text = f"[{host}]" if ip is not None and ip.version == 6 else host
    return f"{host_text}:{port}"


def _external_shim_contract_error(
    *,
    service_name: str,
    service: Mapping[str, Any],
    document: Mapping[str, Any],
    documents: list[Mapping[str, Any]],
    document_index: int,
    signed_authority: Any | None = None,
) -> str | None:
    labels = _service_labels(service)
    if labels.get("com.anila.inference-role") != "external-shim":
        return f"model service {service_name} has an unclassified external-shim role"
    if labels.get("com.anila.provider-locality") != "internal_shim":
        return f"external shim {service_name} must declare internal_shim provider locality"
    egress_network = labels.get("com.anila.egress-network", "").strip()
    if not egress_network or egress_network not in _networks(service):
        return f"external shim {service_name} must attach its declared egress network"
    network_map = document.get("networks")
    declared_network = network_map.get(egress_network) if isinstance(network_map, Mapping) else None
    if not isinstance(declared_network, Mapping):
        return f"external shim {service_name} egress network declaration is missing"
    egress_effective_name = _network_effective_name(egress_network, document)
    if egress_effective_name is None:
        return f"external shim {service_name} egress network identity is unresolved"
    if egress_effective_name == SHARED_MODEL_NETWORK_NAME:
        return f"external shim {service_name} egress network must be dedicated"
    # Compose's resolved JSON often omits an explicit ``internal: false``.
    # Absence is therefore acceptable, but an internal or external-owned
    # network is not a dedicated project egress bridge.
    if declared_network.get("internal") is True:
        return f"external shim {service_name} egress network must not be internal"
    if declared_network.get("external") is True:
        return f"external shim {service_name} egress network must be project-owned"
    if str(declared_network.get("driver", "bridge")) != "bridge":
        return f"external shim {service_name} egress network must use the bridge driver"
    if _network_matches_model_tokens(egress_network, document):
        return f"external shim {service_name} egress network must be dedicated"

    logical_networks = _networks(service)
    effective_networks = _service_effective_networks(service, document)
    expected_effective_networks = {
        SHARED_MODEL_NETWORK_NAME,
        egress_effective_name,
    }
    if (
        len(logical_networks) != 2
        or effective_networks != expected_effective_networks
        or not any(
            _is_external_shared_model_network(logical_name, document)
            for logical_name in logical_networks
        )
    ):
        return (
            f"external shim {service_name} network set must be exactly the external "
            "shared model ingress and declared dedicated egress"
        )

    attachers = _effective_network_attachers(
        documents, effective_network=egress_effective_name
    )
    if attachers != [(document_index, service_name)]:
        return f"external shim {service_name} egress network must have one attached service"
    target = _normalise_external_target(_environment(service).get("TRITON_GRPC_URL", ""))
    if target is None:
        return f"external shim {service_name} must declare a host:port TRITON_GRPC_URL"
    if labels.get("com.anila.egress-target", "").strip() != target:
        return f"external shim {service_name} egress target does not match TRITON_GRPC_URL"
    if labels.get("com.anila.upstream-locality") != "external_governed":
        return (
            f"external shim {service_name} must declare external_governed "
            "upstream locality"
        )
    if labels.get("com.anila.upstream-transport") != "triton-grpc":
        return f"external shim {service_name} must declare triton-grpc upstream transport"
    upstream_egress_policy_id = labels.get(
        EXTERNAL_SHIM_UPSTREAM_EGRESS_POLICY_LABEL, ""
    ).strip()
    if not upstream_egress_policy_id:
        return (
            f"external shim {service_name} must declare its upstream egress policy id"
        )

    if signed_authority is not None:
        model_name = _environment(service).get("MODEL_NAME", "").strip()
        if not model_name:
            return f"external shim {service_name} must declare MODEL_NAME"
        provider_bindings = getattr(signed_authority, "provider_bindings", None)
        if not isinstance(provider_bindings, Mapping):
            return "signed model-governance authority has no provider bindings"
        matches = [
            binding
            for binding in provider_bindings.values()
            if getattr(binding, "model_registry_name", None) == model_name
        ]
        if len(matches) != 1:
            return (
                f"signed provider authority must contain exactly one binding for "
                f"{model_name!r}; found {len(matches)}"
            )
        binding = matches[0]
        if getattr(binding, "provider_locality", None) != labels.get(
            "com.anila.provider-locality"
        ):
            return (
                f"external shim {service_name} provider locality does not match "
                "signed provider authority"
            )
        upstream_target = getattr(binding, "upstream_transport_target", None)
        expected_upstream_hash = getattr(
            binding, "upstream_transport_target_sha256", None
        )
        expected_upstream_locality = getattr(
            binding, "upstream_provider_locality", None
        )
        expected_policy_id = getattr(binding, "upstream_egress_policy_id", None)
        if upstream_target is None or not isinstance(expected_upstream_hash, str):
            return (
                f"signed provider authority for {model_name!r} lacks its "
                "upstream transport target/hash"
            )
        if expected_upstream_locality != labels.get("com.anila.upstream-locality"):
            return (
                f"external shim {service_name} upstream locality does not match "
                "signed provider authority"
            )
        if expected_policy_id != upstream_egress_policy_id:
            return (
                f"external shim {service_name} upstream egress policy id does not "
                "match signed provider authority"
            )
        try:
            from anila_security.model_governance import (
                ModelGovernanceError,
                TransportTarget,
            )
        except ImportError:
            return (
                f"external shim {service_name} cannot load the signed "
                "transport-target verifier"
            )
        try:
            actual_upstream_target = TransportTarget.parse(
                target,
                dns_policy=upstream_target.dns_policy,
                field="resolved TRITON_GRPC_URL",
            )
        except ModelGovernanceError as exc:
            return (
                f"external shim {service_name} resolved TRITON_GRPC_URL cannot be "
                f"bound to signed provider authority: {exc}"
            )
        if actual_upstream_target.canonical != upstream_target.canonical:
            return (
                f"external shim {service_name} resolved TRITON_GRPC_URL does not "
                "match signed upstream_transport_target"
            )
        if actual_upstream_target.sha256 != expected_upstream_hash:
            return (
                f"external shim {service_name} upstream_transport_target_sha256 "
                "does not match signed provider authority"
            )
    return None


def _source_path(value: object, *, repo_root: Path | None) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value)
    if not path.is_absolute() and repo_root is not None:
        path = repo_root / path
    return path


def _verify_governance_material(
    *,
    csp: Mapping[str, Any],
    repo_root: Path | None,
    require_files: bool,
) -> dict[str, Any]:
    env = _environment(csp)
    result: dict[str, Any] = {"files": {}}
    mounts = _mounts(csp)
    by_target = {str(item.get("target")): item for item in mounts}
    for key, target in GOVERNANCE_TARGETS.items():
        path = env.get(key, "").strip()
        if path != target:
            raise DeploymentEgressError(
                f"csp {key} must resolve to read-only target {target}"
            )
        mount = by_target.get(target)
        if mount is None:
            raise DeploymentEgressError(f"csp governance material mount missing: {target}")
        if mount.get("read_only") is not True:
            raise DeploymentEgressError(f"csp governance material mount is writable: {target}")
        source = _source_path(mount.get("source"), repo_root=repo_root)
        if source is None:
            raise DeploymentEgressError(f"csp governance material source missing: {target}")
        if require_files:
            if repo_root is not None:
                try:
                    source.resolve().relative_to(repo_root.resolve())
                except ValueError:
                    pass
                else:
                    raise DeploymentEgressError(
                        f"formal governance material must live outside the repository: {source}"
                    )
            if not source.is_file() or source.is_symlink():
                raise DeploymentEgressError(f"governance material file missing or symlinked: {source}")
        result["files"][key] = str(source)

    profile_source = _source_path(
        by_target[GOVERNANCE_TARGETS["GATE5_MODEL_GOVERNANCE_PROFILE_PATH"]].get("source"),
        repo_root=repo_root,
    )
    if require_files and profile_source is not None:
        try:
            profile = json.loads(profile_source.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DeploymentEgressError("signed model-governance profile is unreadable") from exc
        if not isinstance(profile, Mapping) or profile.get("enabled") is not True:
            raise DeploymentEgressError("signed model-governance profile is disabled/not an approval")
        egress = profile.get("egress_policy")
        if not isinstance(egress, Mapping) or egress.get("raw_endpoint_denied") is not True:
            raise DeploymentEgressError("signed model-governance profile does not deny raw endpoints")
        result["profile_id"] = str(profile.get("profile_id", ""))
    return result


def _read_governance_json(path: object, *, label: str) -> dict[str, Any]:
    if not isinstance(path, str) or not path.strip():
        raise DeploymentEgressError(f"signed governance {label} path is missing")
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DeploymentEgressError(
            f"signed governance {label} is unreadable"
        ) from exc
    if not isinstance(value, dict):
        raise DeploymentEgressError(f"signed governance {label} must be an object")
    return value


def _load_signed_provider_authority(
    material: Mapping[str, Any],
) -> Any:
    files = material.get("files")
    if not isinstance(files, Mapping):
        raise DeploymentEgressError("signed governance material file map is missing")
    inventory = _read_governance_json(
        files.get("GATE5_MODEL_GOVERNANCE_INVENTORY_PATH"), label="inventory"
    )
    profile = _read_governance_json(
        files.get("GATE5_MODEL_GOVERNANCE_PROFILE_PATH"), label="profile"
    )
    trust_store = _read_governance_json(
        files.get("GATE5_MODEL_GOVERNANCE_TRUST_STORE_PATH"), label="trust store"
    )
    try:
        from anila_security.model_governance import (
            VerifiedModelGovernanceAuthority,
        )

        authority = VerifiedModelGovernanceAuthority.from_verified_payload(
            inventory=inventory,
            profile=profile,
            trust_store=trust_store,
        )
    except ImportError as exc:
        raise DeploymentEgressError(
            "signed model-governance authority verifier is unavailable"
        ) from exc
    except Exception as exc:  # noqa: BLE001 - deployment policy must fail closed
        raise DeploymentEgressError(
            f"signed model-governance provider authority is invalid: {exc}"
        ) from exc
    if not authority.enabled:
        raise DeploymentEgressError(
            "signed model-governance provider authority is disabled"
        )
    return authority


def _check_formal_services(
    documents: list[Mapping[str, Any]],
    *,
    profile: str,
    repo_root: Path | None,
    require_material: bool,
) -> dict[str, Any]:
    if not documents:
        raise DeploymentEgressError("at least one resolved Compose document is required")

    platform = documents[0]
    platform_services = _services(platform)
    csp = platform_services.get("csp")
    if not isinstance(csp, Mapping):
        raise DeploymentEgressError("formal deployment must contain a csp service")
    csp_env = _environment(csp)
    if not _as_bool(csp_env.get("GATE5_MODEL_GOVERNANCE_ENABLED", "")):
        raise DeploymentEgressError("formal csp must enable Gate 5 model governance")
    if not _as_bool(csp_env.get("GATE5_MODEL_GOVERNANCE_STARTUP_REQUIRED", "")):
        raise DeploymentEgressError("formal csp must require Gate 5 governance at startup")
    gateway_value = csp_env.get("GATE5_MODEL_GATEWAY_ENDPOINT", "").strip()
    gateway = _gateway_url(gateway_value)
    if not _host_is_csp(gateway_value, gateway):
        raise DeploymentEgressError("formal model gateway must resolve to the CSP service")
    material = _verify_governance_material(
        csp=csp, repo_root=repo_root, require_files=require_material
    )

    model_networks = {
        network
        for service in platform_services.values()
        if isinstance(service, Mapping)
        for network in _networks(service)
        if _is_model_network(network, platform)
    }
    if not model_networks:
        raise DeploymentEgressError("formal platform must declare a CSP model network")
    csp_model_networks = _networks(csp) & model_networks
    if not csp_model_networks:
        raise DeploymentEgressError("formal csp must be attached to the model network")
    attached = {
        name
        for name, service in platform_services.items()
        if isinstance(service, Mapping)
        and (_networks(service) & model_networks)
    }
    if attached != {"csp"}:
        raise DeploymentEgressError(
            "only csp may join the formal platform model network; "
            f"attached services={sorted(attached)}"
        )

    for service_name, raw_service in platform_services.items():
        if not isinstance(raw_service, Mapping):
            raise DeploymentEgressError(f"resolved service {service_name} is malformed")
        env = _environment(raw_service)
        if service_name != "csp":
            for key, value in env.items():
                violation = _raw_endpoint_violation(
                    service_name=service_name, key=key, value=value, gateway=gateway
                )
                if violation:
                    raise DeploymentEgressError(violation)
        for mount in _mounts(raw_service):
            target = str(mount.get("target", ""))
            if "governance" in target.lower() and service_name != "csp":
                raise DeploymentEgressError(
                    f"governance material may only be mounted into csp: {service_name}:{target}"
                )
            if "governance" in target.lower() and mount.get("read_only") is not True:
                raise DeploymentEgressError(f"governance mount is writable: {service_name}:{target}")
        if AGENT_NAME.search(service_name):
            if _networks(raw_service) & model_networks:
                labels = _service_labels(raw_service)
                if labels.get("com.anila.inference-role") != "model-side-shim":
                    raise DeploymentEgressError(
                        f"formal Agent {service_name} is attached to a model network"
                    )

    flux_present = any("flux" in name.lower() for name in platform_services)
    flux_services: set[str] = set()
    signed_authority: Any | None = None
    for document_index, document in enumerate(documents[1:], start=1):
        model_services = _services(document)
        model_network_candidates = {
            name
            for name in (document.get("networks") or {})
            if _network_matches_model_tokens(str(name), document)
            or _is_external_shared_model_network(str(name), document)
        }
        invalid_model_networks = sorted(
            name for name in model_network_candidates if not _is_model_network(str(name), document)
        )
        if invalid_model_networks:
            raise DeploymentEgressError(
                "formal model document model networks must be the exact external shared network "
                "or owned internal networks: "
                f"{invalid_model_networks}"
            )
        model_networks_in_document = {
            name for name in model_network_candidates if _is_model_network(str(name), document)
        }
        if not model_networks_in_document:
            raise DeploymentEgressError(
                "formal model document must declare the exact external shared model network "
                "or an owned internal model network"
            )
        allowed_model_hosts = set(model_services) | set(CSP_GATEWAY_HOSTS)
        for service_name, raw_service in model_services.items():
            if not isinstance(raw_service, Mapping):
                raise DeploymentEgressError(f"resolved model service {service_name} is malformed")
            if "flux" in service_name.lower():
                flux_present = True
                flux_services.add(service_name)
            if _ports(raw_service):
                raise DeploymentEgressError(
                    f"formal model service {service_name} must not publish host ports"
                )
            labels = _service_labels(raw_service)
            role = labels.get("com.anila.inference-role", "model-runtime")
            external_shim_error = None
            if role == "external-shim":
                if require_material and signed_authority is None:
                    signed_authority = _load_signed_provider_authority(material)
                external_shim_error = _external_shim_contract_error(
                    service_name=service_name,
                    service=raw_service,
                    document=document,
                    documents=documents,
                    document_index=document_index,
                    signed_authority=signed_authority,
                )
            if external_shim_error:
                raise DeploymentEgressError(external_shim_error)
            for key, value in _environment(raw_service).items():
                if role == "external-shim" and key == "TRITON_GRPC_URL":
                    continue
                if role == "external-shim" and RAW_ENDPOINT_KEYS.search(key):
                    raise DeploymentEgressError(
                        f"external shim {service_name} has an unsupported raw endpoint key: {key}"
                    )
                violation = _model_endpoint_violation(
                    service_name=service_name,
                    key=key,
                    value=value,
                    allowed_hosts=allowed_model_hosts,
                    gateway=gateway,
                )
                if violation:
                    raise DeploymentEgressError(violation)
            if AGENT_NAME.search(service_name) and role != "model-side-shim":
                raise DeploymentEgressError(
                    f"model-stack Agent {service_name} lacks model-side-shim classification"
                )
            if role == "model-side-shim" and service_name != "flux2-dev-agent":
                raise DeploymentEgressError(
                    f"unknown model-side shim is not admitted: {service_name}"
                )
            if role == "model-side-shim":
                if labels.get("com.anila.required-profile") != "flux-approved":
                    raise DeploymentEgressError(
                        f"model-side shim {service_name} must resolve under flux-approved profile"
                    )
                for key, value in _environment(raw_service).items():
                    if key == "FLUX_BACKEND_URL" and value.strip() and not _as_bool(
                        csp_env.get("GATE5_FLUX_LEGAL_APPROVED", "")
                    ):
                        raise DeploymentEgressError(
                            "FLUX model-side shim has a backend URL without legal-approved profile"
                        )

    legal_approved = _as_bool(csp_env.get("GATE5_FLUX_LEGAL_APPROVED", ""))
    if legal_approved and not flux_present:
        raise DeploymentEgressError(
            "GATE5_FLUX_LEGAL_APPROVED=true requires resolved model Compose with --profile flux-approved"
        )
    if flux_present:
        if not legal_approved:
            raise DeploymentEgressError(
                "FLUX services are present but no legal-approved Gate 5 profile is declared"
            )
        required_flux = {"flux2-dev", "flux2-dev-agent"}
        if flux_services != required_flux:
            raise DeploymentEgressError(
                "flux-approved resolved model Compose must include exactly flux2-dev and flux2-dev-agent; "
                f"resolved={sorted(flux_services)}"
            )
        profile_source = next(
            (
                item.get("source")
                for item in _mounts(csp)
                if item.get("target") == GOVERNANCE_TARGETS["GATE5_MODEL_GOVERNANCE_PROFILE_PATH"]
            ),
            None,
        )
        profile_path = _source_path(profile_source, repo_root=repo_root)
        if require_material and profile_path is not None:
            try:
                signed_profile = json.loads(profile_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise DeploymentEgressError("cannot read profile for FLUX legal binding") from exc
            enabled = signed_profile.get("enabled_callsites", []) if isinstance(signed_profile, Mapping) else []
            if not any(isinstance(item, str) and "flux" in item for item in enabled):
                raise DeploymentEgressError(
                    "FLUX legal-approved profile lacks a signed FLUX callsite binding"
                )

    return {
        "profile": profile,
        "formal": True,
        "platform_services": len(platform_services),
        "model_networks": sorted(model_networks),
        "csp_model_networks": sorted(csp_model_networks),
        "governance": material,
        "flux_present": flux_present,
    }


def verify_deployment_egress(
    documents: Iterable[Mapping[str, Any]],
    *,
    profile: str,
    repo_root: Path | None = None,
    require_material: bool = True,
) -> dict[str, Any]:
    """Verify resolved platform/model Compose documents.

    ``documents[0]`` is the platform stack; later documents are independent
    model stacks.  Development documents are accepted as an explicit test
    posture and reported without being mistaken for production approval.
    """

    materialized = list(documents)
    if not _is_formal(profile):
        if not materialized:
            raise DeploymentEgressError("at least one resolved Compose document is required")
        return {
            "profile": profile,
            "formal": False,
            "status": "development-allowed-for-testing",
            "services": sum(len(_services(item)) for item in materialized),
        }
    return _check_formal_services(
        materialized,
        profile=profile,
        repo_root=repo_root,
        require_material=require_material,
    )


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DeploymentEgressError(f"cannot read resolved Compose JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise DeploymentEgressError(f"resolved Compose JSON root must be an object: {path}")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", default=os.environ.get("ANILA_DEPLOYMENT_PROFILE", ""))
    parser.add_argument("--compose-json", action="append", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--skip-material-check",
        action="store_true",
        help="only for pure topology fixtures; never use for a formal deploy",
    )
    parser.add_argument(
        "--require-formal",
        action="store_true",
        help="fail unless the selected profile entered the formal policy path",
    )
    args = parser.parse_args(argv)
    try:
        result = verify_deployment_egress(
            [_load_json(path) for path in args.compose_json],
            profile=args.profile,
            repo_root=args.repo_root,
            require_material=not args.skip_material_check,
        )
        if args.require_formal and result.get("formal") is not True:
            raise DeploymentEgressError(
                f"profile {args.profile!r} did not enter the formal enforcement path"
            )
    except DeploymentEgressError as exc:
        print(f"FAIL: {exc}")
        return 1
    print("PASS: " + json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
