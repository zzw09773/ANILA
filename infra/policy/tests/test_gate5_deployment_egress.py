from __future__ import annotations

import copy
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

from infra.policy.tests.test_gate5_model_governance import (
    _base_profile,
    _profile_digest,
    _write_signed_profile,
)
from anila_security.model_governance import TransportTarget, transport_target_sha256
from infra.policy.gate5.check_deployment_egress import (
    DeploymentEgressError,
    _csp_base_url,
    _gateway_url,
    _is_model_network,
    _normalise_external_target,
    verify_deployment_egress,
)


ROOT = Path(__file__).parents[3]
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
            "models": {
                "name": "anila-models-net",
                "external": True,
            },
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
        "networks": {
            "models": {
                "name": "anila-models-net",
                "external": True,
            }
        },
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


def test_formal_gateway_must_resolve_to_csp_host() -> None:
    platform = _platform()
    platform["services"]["csp"]["environment"][
        "GATE5_MODEL_GATEWAY_ENDPOINT"
    ] = "https://evil.example/v1"
    with pytest.raises(DeploymentEgressError, match="resolve to the CSP service"):
        verify_deployment_egress(
            [platform, _models()],
            profile="prod-intranet-card",
            require_material=False,
        )


@pytest.mark.parametrize(
    "key", ["TRITON_GRPC_URL", "MODEL_URL", "VISION_URL", "RELATION_LLM_URL"]
)
def test_formal_agent_rejects_every_raw_endpoint_key(key: str) -> None:
    platform = _platform()
    platform["services"]["router"]["environment"][key] = "http://evil.example:9000/v1"
    with pytest.raises(DeploymentEgressError, match="raw/non-CSP model endpoint"):
        verify_deployment_egress(
            [platform, _models()],
            profile="prod-intranet-card",
            require_material=False,
        )


def test_formal_platform_csp_only_raw_endpoint_is_allowed() -> None:
    platform = _platform()
    platform["services"]["router"]["environment"]["MODEL_URL"] = "http://csp:8000/v1"
    result = verify_deployment_egress(
        [platform, _models()],
        profile="prod-intranet-card",
        require_material=False,
    )
    assert result["formal"] is True


@pytest.mark.parametrize("internal", [False, None])
def test_formal_model_network_must_be_explicitly_internal(internal: bool | None) -> None:
    model = _models()
    model["networks"]["models"] = {"name": "owned-model-net"}
    if internal is not None:
        model["networks"]["models"]["internal"] = internal
    with pytest.raises(
        DeploymentEgressError,
        match="model networks must be internal|internal model network|owned internal",
    ):
        verify_deployment_egress(
            [_platform(), model],
            profile="prod-intranet-card",
            require_material=False,
        )


def test_formal_model_document_rejects_external_raw_endpoint() -> None:
    model = _models()
    model["services"]["gemma4"]["environment"] = {
        "MODEL_URL": "http://evil.example:9000/v1"
    }
    with pytest.raises(DeploymentEgressError, match="external/unknown model endpoint"):
        verify_deployment_egress(
            [_platform(), model],
            profile="prod-intranet-card",
            require_material=False,
        )


def test_formal_model_document_internal_endpoint_is_allowed() -> None:
    model = _models()
    model["services"]["gemma4"]["environment"] = {
        "MODEL_URL": "http://gemma4:8000/v1"
    }
    result = verify_deployment_egress(
        [_platform(), model],
        profile="prod-intranet-card",
        require_material=False,
    )
    assert result["formal"] is True


def test_formal_model_document_internal_triton_endpoint_is_allowed() -> None:
    model = _models()
    model["services"]["gemma4"]["environment"] = {
        "TRITON_GRPC_URL": "gemma4:8001"
    }
    result = verify_deployment_egress(
        [_platform(), model],
        profile="prod-intranet-card",
        require_material=False,
    )
    assert result["formal"] is True


def test_formal_model_document_triton_endpoint_rejects_scheme_form() -> None:
    model = _models()
    model["services"]["gemma4"]["environment"] = {
        "TRITON_GRPC_URL": "http://gemma4:8001"
    }
    with pytest.raises(DeploymentEgressError, match="bare host:port"):
        verify_deployment_egress(
            [_platform(), model],
            profile="prod-intranet-card",
            require_material=False,
        )


@pytest.mark.parametrize(
    "network",
    [
        {"name": "evil-models-net", "external": True},
        {"name": "anila-models-net"},
        {"external": True},
        {"name": "anila-models-net-evil", "external": True},
    ],
)
def test_formal_shared_model_network_requires_exact_external_identity(
    network: dict[str, object],
) -> None:
    model = _models()
    model["networks"]["models"] = network
    with pytest.raises(DeploymentEgressError, match="model network|owned internal"):
        verify_deployment_egress(
            [_platform(), model],
            profile="prod-intranet-card",
            require_material=False,
        )


@pytest.mark.parametrize("network_name", ["donkernet", "anila-donkernet-net"])
def test_formal_owned_model_network_is_still_required_to_be_internal(
    network_name: str,
) -> None:
    model = _models()
    model["networks"]["models"] = {"name": network_name, "internal": True}
    result = verify_deployment_egress(
        [_platform(), model],
        profile="prod-intranet-card",
        require_material=False,
    )
    assert result["formal"] is True


@pytest.mark.parametrize(
    "name",
    ["donkernet", "anila-donkernet-net", "model-net", "inference-net"],
)
def test_owned_model_network_tokens_are_governed(name: str) -> None:
    document = {
        "name": "anila-models",
        "networks": {"candidate": {"name": name, "internal": True}},
    }
    assert _is_model_network("candidate", document) is True


def test_unrelated_donker_prefix_is_not_a_model_network() -> None:
    document = {
        "name": "anila-models",
        "networks": {"candidate": {"name": "donkerfoo", "internal": True}},
    }
    assert _is_model_network("candidate", document) is False


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("TRITON.Example.:9001", "triton.example:9001"),
        ("[2001:0db8:0:0:0:0:0:35]:9001", "[2001:db8::35]:9001"),
        ("172.16.120.35:9001", "172.16.120.35:9001"),
        ("127.provider.example:9001", "127.provider.example:9001"),
        ("model-127.vendor.example:9001", "model-127.vendor.example:9001"),
    ],
)
def test_external_target_is_canonicalised(target: str, expected: str) -> None:
    model = _external_shim_model(target=target)
    model["services"]["nv-embed-proxy"]["labels"]["com.anila.egress-target"] = expected
    result = verify_deployment_egress(
        [_platform(), model],
        profile="prod-intranet-card",
        require_material=False,
    )
    assert result["formal"] is True


@pytest.mark.parametrize(
    "target",
    [
        "127.1:9001",
        "127.000.000.001:9001",
        "2130706433:9001",
        "0x7f000001:9001",
        "0177.0.0.1:9001",
        "127.0.1:9001",
        "127.1.1:9001",
        "0x7f.0.0.1:9001",
        "0X7F000001:9001",
        "1.2.3.4.:9001",
    ],
)
def test_external_target_rejects_ambiguous_numeric_ipv4_syntax(target: str) -> None:
    assert _normalise_external_target(target) is None
    with pytest.raises(DeploymentEgressError, match="host:port TRITON_GRPC_URL"):
        verify_deployment_egress(
            [_platform(), _external_shim_model(target=target)],
            profile="prod-intranet-card",
            require_material=False,
        )


@pytest.mark.parametrize(
    "target",
    ["172.16.120.35:0", "triton.internal.example:0", "[2001:db8::35]:0"],
)
def test_external_target_rejects_zero_port(target: str) -> None:
    assert _normalise_external_target(target) is None
    with pytest.raises(DeploymentEgressError, match="host:port TRITON_GRPC_URL"):
        verify_deployment_egress(
            [_platform(), _external_shim_model(target=target)],
            profile="prod-intranet-card",
            require_material=False,
        )


def test_external_egress_missing_internal_false_is_allowed_after_resolution() -> None:
    model = _external_shim_model()
    model["networks"]["embedding-egress"].pop("internal")
    result = verify_deployment_egress(
        [_platform(), model],
        profile="prod-intranet-card",
        require_material=False,
    )
    assert result["formal"] is True


def _external_shim_model(*, target: str = "triton.internal.example:9001") -> dict:
    model = {
        "services": {
            "nv-embed-proxy": {
                "labels": {
                    "com.anila.inference-role": "external-shim",
                    "com.anila.provider-locality": "internal_shim",
                    "com.anila.upstream-locality": "external_governed",
                    "com.anila.upstream-transport": "triton-grpc",
                    "com.anila.egress-network": "embedding-egress",
                    "com.anila.egress-target": target,
                    "com.anila.upstream-egress-policy-id": "egress.embedding",
                },
                "environment": {
                    "TRITON_GRPC_URL": target,
                    "MODEL_NAME": "nv-embed-v2",
                },
                "networks": ["models", "embedding-egress"],
            }
        },
        "networks": {
            "models": {"name": "anila-models-net", "external": True},
            "embedding-egress": {"name": "anila-embedding-egress", "internal": False},
        },
    }
    return model


def _write_signed_embedding_material(tmp_path: Path) -> Path:
    """Create valid signed authority material for the external-shim fixture."""

    material = tmp_path / "governance-material"
    material.mkdir()
    profile = _base_profile("r7.csp.memory")
    profile["deployments"][0]["health_readiness"]["last_check"] = (
        datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    )
    provider = profile["provider_bindings"][0]
    provider["model_registry_name"] = "nv-embed-v2"
    provider["provider_locality"] = "internal_shim"
    provider_target = TransportTarget.parse(
        "nv-embed-proxy:8000", dns_policy="none"
    ).to_dict()
    provider["transport_target"] = provider_target
    provider["transport_target_sha256"] = transport_target_sha256(provider_target)
    provider["upstream_provider_locality"] = "external_governed"
    upstream_target = TransportTarget.parse(
        "172.16.120.35:9001", dns_policy="none"
    ).to_dict()
    provider["upstream_transport_target"] = upstream_target
    provider["upstream_transport_target_sha256"] = transport_target_sha256(
        upstream_target
    )
    provider["egress_policy_id"] = None
    provider["upstream_egress_policy_id"] = "egress.embedding"
    profile["profile_content_sha256"] = _profile_digest(profile)
    _write_signed_profile(material, profile)
    shutil.copy2(
        ROOT / "infra/policy/gate5/model-governance-inventory.v1.json",
        material / "inventory.json",
    )
    (material / "observed-facts.json").write_text("{}", encoding="utf-8")
    return material


def _platform_with_signed_embedding_material(material: Path) -> dict:
    platform = _platform()
    source_by_target = {
        GOVERNANCE_TARGETS["GATE5_MODEL_GOVERNANCE_INVENTORY_PATH"]: "inventory.json",
        GOVERNANCE_TARGETS["GATE5_MODEL_GOVERNANCE_PROFILE_PATH"]: "profile.json",
        GOVERNANCE_TARGETS["GATE5_MODEL_GOVERNANCE_TRUST_STORE_PATH"]: "trust.json",
        GOVERNANCE_TARGETS["GATE5_MODEL_GOVERNANCE_OBSERVED_FACTS_PATH"]: "observed-facts.json",
    }
    for mount in platform["services"]["csp"]["volumes"]:
        mount["source"] = str(material / source_by_target[mount["target"]])
    return platform


def test_external_shim_destination_must_match_signed_provider_authority(
    tmp_path: Path,
) -> None:
    material = _write_signed_embedding_material(tmp_path)
    platform = _platform_with_signed_embedding_material(material)

    result = verify_deployment_egress(
        [platform, _external_shim_model(target="172.16.120.35:9001")],
        profile="prod-intranet-card",
        repo_root=ROOT,
        require_material=True,
    )
    assert result["formal"] is True

    substituted = _external_shim_model(target="172.16.120.36:9001")
    with pytest.raises(
        DeploymentEgressError, match="does not match signed upstream_transport_target"
    ):
        verify_deployment_egress(
            [platform, substituted],
            profile="prod-intranet-card",
            repo_root=ROOT,
            require_material=True,
        )


def test_formal_external_shim_requires_strict_dedicated_contract() -> None:
    result = verify_deployment_egress(
        [_platform(), _external_shim_model()],
        profile="prod-intranet-card",
        require_material=False,
    )
    assert result["formal"] is True

    missing_label = copy.deepcopy(_external_shim_model())
    missing_label["services"]["nv-embed-proxy"]["labels"].pop(
        "com.anila.egress-target"
    )
    with pytest.raises(DeploymentEgressError, match="egress target"):
        verify_deployment_egress(
            [_platform(), missing_label],
            profile="prod-intranet-card",
            require_material=False,
        )


@pytest.mark.parametrize(
    "egress_network",
    ["mydonkernet-egress", "donkernetfoo"],
)
def test_formal_external_shim_allows_donkernet_only_as_a_substring(
    egress_network: str,
) -> None:
    model = _external_shim_model()
    service = model["services"]["nv-embed-proxy"]
    service["labels"]["com.anila.egress-network"] = egress_network
    service["networks"] = ["models", egress_network]
    model["networks"].pop("embedding-egress")
    model["networks"][egress_network] = {
        "name": egress_network,
        "internal": False,
    }

    result = verify_deployment_egress(
        [_platform(), model],
        profile="prod-intranet-card",
        require_material=False,
    )
    assert result["formal"] is True


def test_formal_external_shim_rejects_shared_or_mismatched_egress() -> None:
    shared = _external_shim_model()
    shared["services"]["other-model"] = {"networks": ["embedding-egress"]}
    with pytest.raises(DeploymentEgressError, match="one attached service"):
        verify_deployment_egress(
            [_platform(), shared],
            profile="prod-intranet-card",
            require_material=False,
        )

    mismatched = _external_shim_model(target="triton.internal.example:9001")
    mismatched["services"]["nv-embed-proxy"]["environment"][
        "TRITON_GRPC_URL"
    ] = "triton.internal.example:9002"
    with pytest.raises(DeploymentEgressError, match="does not match"):
        verify_deployment_egress(
            [_platform(), mismatched],
            profile="prod-intranet-card",
            require_material=False,
        )


def test_formal_external_shim_rejects_effective_egress_alias_in_other_document() -> None:
    shim = _external_shim_model()
    second_model_document = {
        "services": {"other-model": {"networks": ["other-egress"]}},
        "networks": {
            "models": {"name": "anila-models-net", "external": True},
            "other-egress": {
                "name": "anila-embedding-egress",
                "driver": "bridge",
                "internal": False,
            },
        },
    }
    with pytest.raises(DeploymentEgressError, match="one attached service"):
        verify_deployment_egress(
            [_platform(), shim, second_model_document],
            profile="prod-intranet-card",
            require_material=False,
        )


def test_formal_external_shim_rejects_any_third_network() -> None:
    shim = _external_shim_model()
    shim["services"]["nv-embed-proxy"]["networks"].append("alternate-egress")
    shim["networks"]["alternate-egress"] = {
        "name": "anila-alternate-egress",
        "driver": "bridge",
        "internal": False,
    }
    with pytest.raises(DeploymentEgressError, match="network set must be exactly"):
        verify_deployment_egress(
            [_platform(), shim],
            profile="prod-intranet-card",
            require_material=False,
        )


def test_formal_model_side_shim_rejects_external_raw_endpoint() -> None:
    model = _models(flux=True)
    model["services"]["flux2-dev-agent"]["environment"][
        "TRITON_GRPC_URL"
    ] = "triton.external.example:9001"
    with pytest.raises(DeploymentEgressError, match="external/unknown model endpoint"):
        verify_deployment_egress(
            [_platform(), model],
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
