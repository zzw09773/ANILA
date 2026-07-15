from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from infra.policy.gate5.check_deployment_egress import (
    DeploymentEgressError,
    _csp_base_url,
    _gateway_url,
    verify_deployment_egress,
)


GOVERNANCE_TARGETS = {
    "GATE5_MODEL_GOVERNANCE_INVENTORY_PATH": "/etc/anila/governance/inventory.json",
    "GATE5_MODEL_GOVERNANCE_PROFILE_PATH": "/etc/anila/governance/profile.json",
    "GATE5_MODEL_GOVERNANCE_TRUST_STORE_PATH": "/etc/anila/governance/trust-store.json",
    "GATE5_MODEL_GOVERNANCE_OBSERVED_FACTS_PATH": "/etc/anila/governance/observed-facts.json",
}


def _platform(
    *,
    agent_on_models: bool = False,
    raw_endpoint: bool = False,
    flux_legal_approved: bool = False,
) -> dict:
    csp = {
        "environment": {
            "GATE5_MODEL_GOVERNANCE_ENABLED": "true",
            "GATE5_MODEL_GOVERNANCE_STARTUP_REQUIRED": "true",
            "GATE5_MODEL_GATEWAY_ENDPOINT": "http://csp:8000/v1",
            "GATE5_FLUX_LEGAL_APPROVED": "true" if flux_legal_approved else "false",
            **GOVERNANCE_TARGETS,
        },
        "volumes": [
            {"source": f"/tmp/gate5-{name}", "target": target, "read_only": True}
            for name, target in (
                ("inventory", GOVERNANCE_TARGETS["GATE5_MODEL_GOVERNANCE_INVENTORY_PATH"]),
                ("profile", GOVERNANCE_TARGETS["GATE5_MODEL_GOVERNANCE_PROFILE_PATH"]),
                ("trust", GOVERNANCE_TARGETS["GATE5_MODEL_GOVERNANCE_TRUST_STORE_PATH"]),
                ("observed", GOVERNANCE_TARGETS["GATE5_MODEL_GOVERNANCE_OBSERVED_FACTS_PATH"]),
            )
        ],
        "networks": ["default", "models"],
    }
    router = {"environment": {"RENDERER_BASE_URL": "http://pptx-renderer:7100"}, "networks": ["default"]}
    if raw_endpoint:
        router["environment"]["ANILA_BASE_URL"] = "http://gemma4:8000"
    if agent_on_models:
        router["networks"] = ["default", "models"]
    return {
        "services": {"csp": csp, "router": router},
        "networks": {
            "default": {"name": "anila-net"},
            "models": {"name": "anila-models-net"},
        },
    }


def _models(*, flux: bool = False) -> dict:
    services = {
        "gemma4": {
            "labels": {"com.anila.inference-role": "model-runtime"},
            "networks": ["models"],
        }
    }
    if flux:
        services["flux2-dev"] = {
            "labels": {
                "com.anila.inference-role": "model-runtime",
                "com.anila.required-profile": "flux-approved",
            },
            "networks": ["models"],
        }
        services["flux2-dev-agent"] = {
            "labels": {
                "com.anila.inference-role": "model-side-shim",
                "com.anila.required-profile": "flux-approved",
            },
            "environment": {"FLUX_BACKEND_URL": "http://flux2-dev:8000"},
            "networks": ["models"],
            "expose": ["8000"],
        }
    return {
        "services": services,
        "networks": {"models": {"name": "anila-models-net"}},
    }


def test_formal_csp_only_model_network_and_renderer_url_are_allowed() -> None:
    result = verify_deployment_egress(
        [_platform(), _models()],
        profile="prod-intranet-card",
        require_material=False,
    )
    assert result["formal"] is True
    assert result["flux_present"] is False


def test_formal_agent_cannot_join_model_network() -> None:
    with pytest.raises(DeploymentEgressError, match="only csp may join"):
        verify_deployment_egress(
            [_platform(agent_on_models=True), _models()],
            profile="prod-intranet-card",
            require_material=False,
        )


def test_formal_agent_raw_endpoint_is_rejected() -> None:
    with pytest.raises(DeploymentEgressError, match="raw/non-CSP model endpoint"):
        verify_deployment_egress(
            [_platform(raw_endpoint=True), _models()],
            profile="prod-intranet-card",
            require_material=False,
        )


def test_invalid_endpoint_ports_fail_closed() -> None:
    platform = _platform()
    platform["services"]["csp"]["environment"][
        "GATE5_MODEL_GATEWAY_ENDPOINT"
    ] = "http://csp:notaport/v1"
    with pytest.raises(DeploymentEgressError, match="port is invalid"):
        verify_deployment_egress(
            [platform, _models()],
            profile="prod-intranet-card",
            require_material=False,
        )

    with pytest.raises(DeploymentEgressError, match="port is invalid"):
        _gateway_url("http://csp:notaport/v1")
    assert not _csp_base_url(
        "http://csp:notaport/v1", ("csp", 8000, "/v1")
    )


def test_malformed_ipv6_endpoint_fails_closed() -> None:
    with pytest.raises(DeploymentEgressError, match="URL is invalid"):
        _gateway_url("http://[bad/v1")
    assert not _csp_base_url("http://[bad/v1", ("csp", 8000, "/v1"))


def test_flux_requires_legal_marker_and_signed_callsite() -> None:
    platform = _platform()
    with pytest.raises(DeploymentEgressError, match="legal-approved"):
        verify_deployment_egress(
            [platform, _models(flux=True)],
            profile="prod-intranet-card",
            require_material=False,
        )


def test_legal_approved_requires_active_flux_profile() -> None:
    with pytest.raises(DeploymentEgressError, match="--profile flux-approved"):
        verify_deployment_egress(
            [_platform(flux_legal_approved=True), _models()],
            profile="prod-intranet-card",
            require_material=False,
        )


def test_legal_approved_flux_profile_is_complete() -> None:
    result = verify_deployment_egress(
        [_platform(flux_legal_approved=True), _models(flux=True)],
        profile="prod-intranet-card",
        require_material=False,
    )
    assert result["flux_present"] is True


def test_flux_profile_must_not_publish_host_port() -> None:
    model = _models(flux=True)
    model["services"]["flux2-dev"]["ports"] = ["30010:8000"]
    with pytest.raises(DeploymentEgressError, match="host ports"):
        verify_deployment_egress(
            [_platform(flux_legal_approved=True), model],
            profile="prod-intranet-card",
            require_material=False,
        )


def test_development_is_explicitly_testable_without_formal_boundary() -> None:
    result = verify_deployment_egress(
        [{"services": {"agent": {"environment": {"ANILA_BASE_URL": "http://model:8000"}}}}],
        profile="development",
    )
    assert result["status"] == "development-allowed-for-testing"


def test_formal_material_must_exist_and_live_outside_repo(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    material = tmp_path / "material"
    repo.mkdir()
    material.mkdir()
    names = {
        "inventory": "inventory.json",
        "profile": "profile.json",
        "trust": "trust-store.json",
        "observed": "observed-facts.json",
    }
    for name, filename in names.items():
        payload = {"enabled": True, "egress_policy": {"raw_endpoint_denied": True}} if name == "profile" else {}
        (material / filename).write_text(json.dumps(payload), encoding="utf-8")
    platform = _platform()
    source_by_target = {
        GOVERNANCE_TARGETS["GATE5_MODEL_GOVERNANCE_INVENTORY_PATH"]: "inventory.json",
        GOVERNANCE_TARGETS["GATE5_MODEL_GOVERNANCE_PROFILE_PATH"]: "profile.json",
        GOVERNANCE_TARGETS["GATE5_MODEL_GOVERNANCE_TRUST_STORE_PATH"]: "trust-store.json",
        GOVERNANCE_TARGETS["GATE5_MODEL_GOVERNANCE_OBSERVED_FACTS_PATH"]: "observed-facts.json",
    }
    for mount in platform["services"]["csp"]["volumes"]:
        mount["source"] = str(material / source_by_target[mount["target"]])
    result = verify_deployment_egress(
        [platform, _models()],
        profile="prod-intranet-card",
        repo_root=repo,
        require_material=True,
    )
    assert result["governance"]["profile_id"] == ""

    missing = copy.deepcopy(platform)
    missing["services"]["csp"]["volumes"][0]["source"] = str(material / "missing.json")
    with pytest.raises(DeploymentEgressError, match="missing or symlinked"):
        verify_deployment_egress(
            [missing, _models()],
            profile="prod-intranet-card",
            repo_root=repo,
            require_material=True,
        )
