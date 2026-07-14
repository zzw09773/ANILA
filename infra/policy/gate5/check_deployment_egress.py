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
import json
import os
import re
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit


class DeploymentEgressError(RuntimeError):
    """Raised when a resolved deployment violates a fail-closed invariant."""


FORMAL_PROFILES = frozenset({"prod", "production"})
MODEL_NETWORK_TOKENS = ("model", "inference")
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


def _is_model_network(name: str, document: Mapping[str, Any]) -> bool:
    networks = document.get("networks")
    declared = networks.get(name) if isinstance(networks, Mapping) else None
    effective = ""
    if isinstance(declared, Mapping):
        effective = str(declared.get("name", ""))
    haystack = f"{name} {effective}".lower()
    return any(token in haystack for token in MODEL_NETWORK_TOKENS)


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
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise DeploymentEgressError("GATE5_MODEL_GATEWAY_ENDPOINT must be an absolute URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise DeploymentEgressError("CSP gateway endpoint must not contain credentials/query/fragment")
    path = parsed.path.rstrip("/")
    if not path.startswith("/v1"):
        raise DeploymentEgressError("CSP gateway endpoint must be rooted at /v1")
    return parsed.hostname.lower(), parsed.port, path


def _host_is_csp(value: str, gateway: tuple[str, int | None, str]) -> bool:
    try:
        host, port, _ = _gateway_url(value)
    except DeploymentEgressError:
        return False
    gateway_host, gateway_port, _ = gateway
    return host in {"csp", "csp-model-gateway", gateway_host} and (
        port is None or gateway_port is None or port == gateway_port
    )


def _csp_base_url(value: str, gateway: tuple[str, int | None, str]) -> bool:
    """Accept CSP service bases with or without the gateway's ``/v1`` path."""

    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        return False
    path = parsed.path.rstrip("/")
    if path not in {"", "/v1"}:
        return False
    gateway_host, gateway_port, _ = gateway
    return parsed.hostname.lower() in {"csp", "csp-model-gateway", gateway_host} and (
        parsed.port is None or gateway_port is None or parsed.port == gateway_port
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
    if key == "ANILA_BASE_URL" or "BASE_URL" in key or "BACKEND_URL" in key:
        if not _host_is_csp(stripped, gateway):
            return f"{service_name}.{key} is a raw/non-CSP model endpoint"
    # MODEL_URL and TRITON_URL can be legitimate values on model-runtime
    # services.  They are still forbidden on platform agents; model documents
    # are checked separately below.
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
    for document in documents[1:]:
        model_services = _services(document)
        for service_name, raw_service in model_services.items():
            if not isinstance(raw_service, Mapping):
                raise DeploymentEgressError(f"resolved model service {service_name} is malformed")
            if "flux" in service_name.lower():
                flux_present = True
            labels = _service_labels(raw_service)
            role = labels.get("com.anila.inference-role", "model-runtime")
            if AGENT_NAME.search(service_name) and role != "model-side-shim":
                raise DeploymentEgressError(
                    f"model-stack Agent {service_name} lacks model-side-shim classification"
                )
            if role == "model-side-shim" and service_name != "flux2-dev-agent":
                raise DeploymentEgressError(
                    f"unknown model-side shim is not admitted: {service_name}"
                )
            if role == "model-side-shim":
                for key, value in _environment(raw_service).items():
                    if key == "FLUX_BACKEND_URL" and value.strip() and not _as_bool(
                        csp_env.get("GATE5_FLUX_LEGAL_APPROVED", "")
                    ):
                        raise DeploymentEgressError(
                            "FLUX model-side shim has a backend URL without legal approval"
                        )

    if flux_present:
        if not _as_bool(csp_env.get("GATE5_FLUX_LEGAL_APPROVED", "")):
            raise DeploymentEgressError(
                "FLUX services are present but no legal-approved Gate 5 profile is declared"
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
    args = parser.parse_args(argv)
    try:
        result = verify_deployment_egress(
            [_load_json(path) for path in args.compose_json],
            profile=args.profile,
            repo_root=args.repo_root,
            require_material=not args.skip_material_check,
        )
    except DeploymentEgressError as exc:
        print(f"FAIL: {exc}")
        return 1
    print("PASS: " + json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
