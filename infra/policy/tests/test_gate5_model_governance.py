from __future__ import annotations

import base64
import copy
import hashlib
import json
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).parents[3]
sys.path.insert(0, str(ROOT / "packages/anila-security/src"))

from anila_security.model_governance import (  # noqa: E402
    ModelGovernanceError,
    ProviderBinding,
    TransportTarget,
    VerifiedModelGovernanceAuthority,
    canonical_json,
    transport_target_sha256,
)
from infra.policy.gate5.check_model_governance import (  # noqa: E402
    ModelGovernancePolicyError,
    _finding_for_file,
    _load_json,
    inventory_hash,
    scan_inference_sources,
    verify,
)

INVENTORY = ROOT / "infra/policy/gate5/model-governance-inventory.v1.json"
TEMPLATE = ROOT / "infra/policy/gate5/model-governance-profile.disabled-template.json"
NOW = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
_DIGEST_A = "sha256:" + "a" * 64
_DIGEST_B = "sha256:" + "b" * 64


def _base_profile(callsite_id: str) -> dict:
    inventory = _load_json(INVENTORY)
    template = _load_json(TEMPLATE)
    call = next(item for item in inventory["callsites"] if item["id"] == callsite_id)
    profile = copy.deepcopy(template)
    profile["enabled"] = True
    profile["enabled_callsites"] = [callsite_id]
    profile["disabled_callsites"] = [
        item["id"] for item in inventory["callsites"] if item["id"] != callsite_id
    ]
    profile["valid_from"] = (NOW - timedelta(minutes=5)).isoformat()
    profile["valid_until"] = (NOW + timedelta(days=7)).isoformat()
    profile["approvers"] = [
        {"role": role, "subject": f"synthetic-{role}"}
        for role in ("system_owner", "data_owner", "security", "operations")
    ]
    profile["model_artifacts"] = [
        {
            "artifact_id": "artifact.synthetic",
            "model_family": "synthetic-llm",
            "digest": _DIGEST_A,
            "revision": "rev-synthetic-1",
            "license_id": "Apache-2.0",
            "license_approved": True,
            "license_approval_artifact_id": "legal.synthetic.v1",
            "legal_approver_ids": ["legal"],
        }
    ]
    profile["deployments"] = [
        {
            "deployment_id": "deployment.synthetic",
            "artifact_id": "artifact.synthetic",
            "image_digest": _DIGEST_B,
            "gpu_topology": {
                "vendor": "NVIDIA",
                "count": 1,
                "memory_gib": 80,
                "compute_capability": "sm_90",
            },
            "health_readiness": {
                "health_url": "/health",
                "readiness_url": "/ready",
                "freshness_seconds": 60,
                "last_check": (NOW - timedelta(seconds=10)).isoformat(),
                "healthy": True,
                "ready": True,
            },
        }
    ]
    target = TransportTarget.parse("synthetic-model:8000").to_dict()
    profile["provider_bindings"] = [
        {
            "provider_binding_id": "provider.synthetic",
            "model_registry_id": "model.synthetic",
            "model_registry_name": "synthetic-llm",
            "model_registry_revision": "registry-rev-1",
            "provider_locality": "internal_isolated",
            "transport_target": target,
            "transport_target_sha256": transport_target_sha256(target),
            "upstream_provider_locality": None,
            "upstream_transport_target": None,
            "upstream_transport_target_sha256": None,
            "egress_policy_id": None,
            "upstream_egress_policy_id": None,
            "model_artifact_id": "artifact.synthetic",
            "deployment_id": "deployment.synthetic",
        }
    ]
    profile["callsite_bindings"] = [
        {
            "callsite_id": callsite_id,
            "gateway_id": call["gateway_id"],
            "classification_ceiling": call["classification_ceiling"],
            "usage_sink": call["usage_sink"],
            "audit_sink": call["audit_sink"],
            "agent_scope": call["agent_scope"],
            "provider_binding_ids": ["provider.synthetic"],
        }
    ]
    profile["profile_content_sha256"] = _profile_digest(profile)
    return profile


def _profile_digest(profile: dict) -> str:
    unsigned = dict(profile)
    unsigned.pop("profile_content_sha256", None)
    unsigned.pop("signatures", None)
    return hashlib.sha256(canonical_json(unsigned)).hexdigest()


def _write_signed_profile(tmp_path: Path, profile: dict) -> tuple[Path, Path]:
    signatures = []
    trusted: dict[str, str] = {}
    payload = dict(profile)
    payload.pop("signatures", None)
    for role in ("system_owner", "data_owner", "security", "operations"):
        key = Ed25519PrivateKey.generate()
        trusted[role] = (
            key.public_key()
            .public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            .decode("ascii")
        )
        signatures.append(
            {
                "role": role,
                "signature": base64.b64encode(key.sign(canonical_json(payload))).decode(
                    "ascii"
                ),
            }
        )
    profile["signatures"] = signatures
    profile_path = tmp_path / "profile.json"
    trust_path = tmp_path / "trust.json"
    profile_path.write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")
    trust_path.write_text(json.dumps({"trusted_signers": trusted}), encoding="utf-8")
    return profile_path, trust_path


def test_disabled_template_is_explicitly_not_an_approval() -> None:
    result = verify(
        inventory_path=INVENTORY,
        profile_path=TEMPLATE,
        allow_disabled_template=True,
        now=NOW,
    )
    assert result["inventory_callsites"] == 40
    assert result["enabled_callsites"] == 0
    with pytest.raises(ModelGovernancePolicyError, match="not an approval"):
        verify(inventory_path=INVENTORY, profile_path=TEMPLATE, now=NOW)


def test_scanner_and_inventory_are_exhaustive() -> None:
    inventory = _load_json(INVENTORY)
    expected = {item["source"] for item in inventory["callsites"]}
    findings = scan_inference_sources(ROOT)
    assert {item.source for item in findings} == expected
    assert any("relation-llm" in item["id"] for item in inventory["callsites"])
    assert any("judge" in item["id"] for item in inventory["callsites"])
    assert any("flux" in item["id"] for item in inventory["callsites"])


def test_inventory_keeps_provider_locality_out_of_static_callsites() -> None:
    inventory = _load_json(INVENTORY)
    for callsite in inventory["callsites"]:
        assert "provider_locality" not in callsite
        assert "transport_target" not in callsite
        if callsite["raw_endpoint"]:
            assert callsite["provider_resolution"] == "direct_raw"
        else:
            assert callsite["provider_resolution"] == "gateway_dynamic"


def test_studio_raw_flux_and_csp_gateway_evidence_are_not_conflated() -> None:
    inventory = _load_json(INVENTORY)
    by_id = {item["id"]: item for item in inventory["callsites"]}

    raw = by_id["r7.studio.flux-provider"]
    assert raw["source"] == (
        "services/anila-studio/app/services/flux_image_provider.py"
    )
    assert raw["symbol"] == "FluxImageProvider._generate"
    assert raw["provider_resolution"] == "direct_raw"
    assert raw["raw_endpoint"] is True
    assert raw["gateway_id"] is None
    assert raw["usage_sink"] is None
    assert raw["audit_sink"] is None
    assert raw["sink_kinds"] == ["images"]

    gateway = by_id["r7.studio.csp-client"]
    assert gateway["source"] == "services/anila-studio/app/clients/csp_client.py"
    assert gateway["symbol"] == (
        "proxy_chat_completions/proxy_image_generations"
    )
    assert gateway["provider_resolution"] == "gateway_dynamic"
    assert gateway["raw_endpoint"] is False
    assert gateway["gateway_id"] == "csp-model-gateway"
    assert gateway["usage_sink"] == "csp.token_usage"
    assert gateway["audit_sink"] == "csp.audit_log"
    assert gateway["sink_kinds"] == ["chat_completions", "images"]

    provider_source = (ROOT / raw["source"]).read_text(encoding="utf-8")
    assert "if self.via_csp_runtime:" in provider_source
    assert "self._images_endpoint()" in provider_source


def test_provider_binding_accepts_external_ip_http_and_grpc_shim_shapes() -> None:
    external_target = TransportTarget.parse(
        "http://172.16.120.35:7000", dns_policy="none"
    ).to_dict()
    external = {
        "provider_binding_id": "provider.external.synthetic",
        "model_registry_id": "model.external.synthetic",
        "model_registry_name": "external-llm",
        "model_registry_revision": "registry-rev-1",
        "provider_locality": "external_governed",
        "transport_target": external_target,
        "transport_target_sha256": transport_target_sha256(external_target),
        "upstream_provider_locality": None,
        "upstream_transport_target": None,
        "upstream_transport_target_sha256": None,
        "egress_policy_id": "egress.external.synthetic",
        "upstream_egress_policy_id": None,
        "model_artifact_id": "artifact.synthetic",
        "deployment_id": "deployment.synthetic",
    }
    parsed_external = ProviderBinding.from_dict(external)
    assert parsed_external.transport_target.canonical == "http://172.16.120.35:7000"

    upstream_target = TransportTarget.parse("172.16.120.35:9001", dns_policy="none").to_dict()
    shim = dict(external)
    shim.update(
        {
            "provider_binding_id": "provider.shim.synthetic",
            "model_registry_id": "model.shim.synthetic",
            "provider_locality": "internal_shim",
            "transport_target": TransportTarget.parse(
                "embedding-shim.internal:8000", dns_policy="none"
            ).to_dict(),
            "transport_target_sha256": transport_target_sha256(
                TransportTarget.parse("embedding-shim.internal:8000", dns_policy="none").to_dict()
            ),
            "upstream_provider_locality": "external_governed",
            "upstream_transport_target": upstream_target,
            "upstream_transport_target_sha256": transport_target_sha256(upstream_target),
            "egress_policy_id": None,
            "upstream_egress_policy_id": "egress.embedding.synthetic",
        }
    )
    parsed_shim = ProviderBinding.from_dict(shim)
    assert parsed_shim.upstream_transport_target is not None
    assert parsed_shim.upstream_transport_target.canonical == "172.16.120.35:9001"

    external["egress_policy_id"] = None
    with pytest.raises(ModelGovernanceError, match="egress_policy_id"):
        ProviderBinding.from_dict(external)


@pytest.mark.parametrize(
    "host",
    ("127.1", "127.000.000.001", "2130706433", "0x7f000001", "0177.0.0.1"),
)
def test_provider_binding_rejects_ambiguous_numeric_ipv4_hosts(host: str) -> None:
    external = {
        "provider_binding_id": "provider.external.synthetic",
        "model_registry_id": "model.external.synthetic",
        "model_registry_name": "external-llm",
        "model_registry_revision": "registry-rev-1",
        "provider_locality": "external_governed",
        "transport_target": f"http://{host}:7000",
        "transport_target_sha256": "0" * 64,
        "upstream_provider_locality": None,
        "upstream_transport_target": None,
        "upstream_transport_target_sha256": None,
        "egress_policy_id": "egress.external.synthetic",
        "upstream_egress_policy_id": None,
        "model_artifact_id": "artifact.synthetic",
        "deployment_id": "deployment.synthetic",
    }

    with pytest.raises(ModelGovernanceError, match="ambiguous numeric IPv4"):
        ProviderBinding.from_dict(external)


def test_scanner_counts_endpoint_literals_inside_f_strings(tmp_path: Path) -> None:
    source = tmp_path / "fstring_sink.py"
    source.write_text(
        "async def invoke(client, version):\n"
        "    return await client.post(f'/v1/chat/completions{version}')\n",
        encoding="utf-8",
    )

    finding = _finding_for_file(source, tmp_path)

    assert finding is not None
    assert finding.sink_kinds == ("chat_completions",)


def test_external_material_uses_explicit_repository_root(tmp_path: Path) -> None:
    material = tmp_path / "governance-material"
    material.mkdir()
    inventory_path = material / "inventory.json"
    profile_path = material / "profile.json"
    shutil.copy2(INVENTORY, inventory_path)
    shutil.copy2(TEMPLATE, profile_path)

    result = verify(
        inventory_path=inventory_path,
        profile_path=profile_path,
        repo_root=ROOT,
        allow_disabled_template=True,
        now=NOW,
    )
    assert result["inventory_callsites"] == 40

    # Deriving a source tree from an external material path is intentionally
    # unsafe and must no longer be a usable CLI/runtime path.
    with pytest.raises(
        ModelGovernancePolicyError,
        match="(?:scanner source root missing|callsite source missing)",
    ):
        verify(
            inventory_path=inventory_path,
            profile_path=profile_path,
            allow_disabled_template=True,
            now=NOW,
        )


def test_governance_cli_callers_pin_repository_root() -> None:
    workflow = (ROOT / ".github/workflows/gate5-static.yml").read_text(encoding="utf-8")
    model_serve = (ROOT / "infra/deployment/intranet/model-serve.sh").read_text(
        encoding="utf-8"
    )
    assert "--repo-root ." in workflow
    assert "--repo-root \"$REPO_ROOT\"" in model_serve


def test_signed_profile_uses_ephemeral_keys_and_passes(tmp_path: Path) -> None:
    profile = _base_profile("r7.router.core")
    profile_path, trust_path = _write_signed_profile(tmp_path, profile)
    result = verify(
        inventory_path=INVENTORY,
        profile_path=profile_path,
        trust_store_path=trust_path,
        now=NOW,
    )
    assert result["profile_enabled"] is True
    assert result["enabled_callsites"] == 1


def test_raw_endpoint_cannot_be_enabled(tmp_path: Path) -> None:
    profile = _base_profile("r7.csp.prompt-generator")
    profile_path, trust_path = _write_signed_profile(tmp_path, profile)
    with pytest.raises(ModelGovernancePolicyError, match="raw/non-CSP"):
        verify(
            inventory_path=INVENTORY,
            profile_path=profile_path,
            trust_store_path=trust_path,
            now=NOW,
        )


def test_studio_raw_flux_provider_cannot_be_enabled(tmp_path: Path) -> None:
    profile = _base_profile("r7.studio.flux-provider")
    profile_path, trust_path = _write_signed_profile(tmp_path, profile)
    with pytest.raises(ModelGovernancePolicyError, match="raw/non-CSP"):
        verify(
            inventory_path=INVENTORY,
            profile_path=profile_path,
            trust_store_path=trust_path,
            now=NOW,
        )


def test_usage_and_audit_sinks_are_required(tmp_path: Path) -> None:
    profile = _base_profile("r7.router.core")
    profile["callsite_bindings"][0]["usage_sink"] = None
    profile["profile_content_sha256"] = _profile_digest(profile)
    profile_path, trust_path = _write_signed_profile(tmp_path, profile)
    with pytest.raises(ModelGovernancePolicyError, match="usage sink"):
        verify(
            inventory_path=INVENTORY,
            profile_path=profile_path,
            trust_store_path=trust_path,
            now=NOW,
        )


def test_agent_scope_binding_drift_is_fail_closed(tmp_path: Path) -> None:
    profile = _base_profile("r7.router.core")
    profile["callsite_bindings"][0]["agent_scope"] = ["unregistered-agent"]
    profile["profile_content_sha256"] = _profile_digest(profile)
    profile_path, trust_path = _write_signed_profile(tmp_path, profile)
    with pytest.raises(ModelGovernancePolicyError, match="agent scope"):
        verify(
            inventory_path=INVENTORY,
            profile_path=profile_path,
            trust_store_path=trust_path,
            now=NOW,
        )


def test_stale_health_readiness_evidence_is_fail_closed(tmp_path: Path) -> None:
    profile = _base_profile("r7.router.core")
    profile["deployments"][0]["health_readiness"]["last_check"] = (
        NOW - timedelta(seconds=61)
    ).isoformat()
    profile["profile_content_sha256"] = _profile_digest(profile)
    profile_path, trust_path = _write_signed_profile(tmp_path, profile)
    with pytest.raises(ModelGovernancePolicyError, match="stale"):
        verify(
            inventory_path=INVENTORY,
            profile_path=profile_path,
            trust_store_path=trust_path,
            now=NOW,
        )


def test_flux_requires_legal_license_artifact(tmp_path: Path) -> None:
    profile = _base_profile("r7.studio.flux-provider")
    profile["model_artifacts"][0]["model_family"] = "FLUX.2-dev"
    profile["model_artifacts"][0]["legal_approver_ids"] = []
    profile["profile_content_sha256"] = _profile_digest(profile)
    profile_path, trust_path = _write_signed_profile(tmp_path, profile)
    with pytest.raises(ModelGovernancePolicyError, match="legal approval"):
        verify(
            inventory_path=INVENTORY,
            profile_path=profile_path,
            trust_store_path=trust_path,
            now=NOW,
        )


def test_signature_and_content_hash_mutation_fails_closed(tmp_path: Path) -> None:
    profile = _base_profile("r7.router.core")
    profile_path, trust_path = _write_signed_profile(tmp_path, profile)
    value = json.loads(profile_path.read_text(encoding="utf-8"))
    value["profile_version"] = "1.0.1"
    profile_path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ModelGovernancePolicyError, match="profile_content_sha256"):
        verify(
            inventory_path=INVENTORY,
            profile_path=profile_path,
            trust_store_path=trust_path,
            now=NOW,
        )


def test_new_unregistered_inference_sink_is_blocked(tmp_path: Path) -> None:
    inventory = _load_json(INVENTORY)
    repo_root = tmp_path / "repo"
    for source_root in ("packages", "services", "infra/models/src"):
        (repo_root / source_root).mkdir(parents=True, exist_ok=True)
    for source in {item["source"] for item in inventory["callsites"]}:
        destination = repo_root / source
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / source, destination)

    real_rogue = ROOT / "services/_r7_unregistered_model_sink.py"
    assert not real_rogue.exists()
    rogue = repo_root / "services/_r7_unregistered_model_sink.py"
    rogue.write_text(
        "import httpx\n\n"
        "async def invoke(client: httpx.AsyncClient):\n"
        "    return await client.post('/chat/completions', json={})\n",
        encoding="utf-8",
    )
    with pytest.raises(ModelGovernancePolicyError, match="scanner mismatch"):
        verify(
            inventory_path=INVENTORY,
            profile_path=TEMPLATE,
            repo_root=repo_root,
            allow_disabled_template=True,
            now=NOW,
        )
    assert rogue.exists()
    assert not real_rogue.exists()


def test_verified_authority_returns_immutable_admission_and_checks_runtime_scope(
    tmp_path: Path,
) -> None:
    profile = _base_profile("r7.router.core")
    profile_path, trust_path = _write_signed_profile(tmp_path, profile)
    inventory = _load_json(INVENTORY)
    authority = VerifiedModelGovernanceAuthority.from_verified_payload(
        inventory=inventory,
        profile=_load_json(profile_path),
        trust_store=_load_json(trust_path),
        now=NOW,
    )
    admitted = authority.authorize(
        "r7.router.core",
        "機密",
        "artifact.synthetic",
        "deployment.synthetic",
        "router",
        NOW,
        artifact_digest=_DIGEST_A,
        artifact_revision="rev-synthetic-1",
        deployment_image_digest=_DIGEST_B,
        provider_binding_id="provider.synthetic",
    )
    assert admitted.gateway_id == "csp-model-gateway"
    assert admitted.classification == "機密"
    assert admitted.agent_scope == ("router", "registered-agent")
    assert admitted.provider_binding is not None
    assert admitted.provider_binding.provider_binding_id == "provider.synthetic"
    with pytest.raises(TypeError):
        authority.bindings["r7.router.core"] = authority.bindings["r7.router.core"]
    with pytest.raises(ModelGovernanceError, match="provider binding identity"):
        authority.authorize(
            "r7.router.core",
            "機密",
            "artifact.synthetic",
            "deployment.synthetic",
            "router",
            NOW,
        )
    with pytest.raises(ModelGovernanceError, match="ceiling"):
        authority.authorize(
            "r7.router.core",
            "絕對機密",
            "artifact.synthetic",
            "deployment.synthetic",
            "router",
            NOW,
            provider_binding_id="provider.synthetic",
        )
    with pytest.raises(ModelGovernanceError, match="digest mismatch"):
        authority.authorize(
            "r7.router.core",
            "機密",
            "artifact.synthetic",
            "deployment.synthetic",
            "router",
            NOW,
            artifact_digest=_DIGEST_B,
            provider_binding_id="provider.synthetic",
        )
    with pytest.raises(ModelGovernanceError, match="agent scope"):
        authority.authorize(
            "r7.router.core",
            "機密",
            "artifact.synthetic",
            "deployment.synthetic",
            None,
            NOW,
            provider_binding_id="provider.synthetic",
        )
    with pytest.raises(ModelGovernanceError, match="stale"):
        authority.authorize(
            "r7.router.core",
            "機密",
            "artifact.synthetic",
            "deployment.synthetic",
            "router",
            NOW + timedelta(seconds=61),
            provider_binding_id="provider.synthetic",
        )


def test_disabled_template_authority_is_constructible_for_audit_but_never_authorizes() -> (
    None
):
    authority = VerifiedModelGovernanceAuthority.from_verified_payload(
        inventory=_load_json(INVENTORY),
        profile=_load_json(TEMPLATE),
        allow_disabled_template=True,
        now=NOW,
    )
    assert authority.enabled is False
    with pytest.raises(ModelGovernanceError, match="disabled"):
        authority.authorize(
            "r7.router.core",
            "無機密",
            "artifact.synthetic",
            "deployment.synthetic",
            "router",
            NOW,
        )


def test_v1_material_is_auditable_only_when_disabled_and_authority_empty() -> None:
    inventory = copy.deepcopy(_load_json(INVENTORY))
    profile = copy.deepcopy(_load_json(TEMPLATE))
    inventory["schema_version"] = "anila.gate5.model-governance.inventory.v1"
    for callsite in inventory["callsites"]:
        callsite.pop("provider_resolution")
    profile["schema_version"] = "anila.gate5.model-governance.profile.v1"
    profile.pop("provider_bindings")
    profile["inventory_sha256"] = inventory_hash(inventory)
    profile["profile_content_sha256"] = _profile_digest(profile)

    authority = VerifiedModelGovernanceAuthority.from_verified_payload(
        inventory=inventory,
        profile=profile,
        allow_disabled_template=True,
        now=NOW,
    )

    assert authority.enabled is False
    assert dict(authority.provider_bindings) == {}

    profile["enabled"] = True
    profile["enabled_callsites"] = ["r7.router.core"]
    profile["disabled_callsites"] = [
        item["id"] for item in inventory["callsites"] if item["id"] != "r7.router.core"
    ]
    profile["profile_content_sha256"] = _profile_digest(profile)
    with pytest.raises(ModelGovernanceError, match="profile v2"):
        VerifiedModelGovernanceAuthority.from_verified_payload(
            inventory=inventory,
            profile=profile,
            now=NOW,
        )


def test_disabled_v2_profile_rejects_provider_authority_material() -> None:
    profile = copy.deepcopy(_load_json(TEMPLATE))
    profile["provider_bindings"] = [{"not": "an authority-free template"}]
    profile["profile_content_sha256"] = _profile_digest(profile)

    with pytest.raises(ModelGovernanceError, match="cannot carry model authority"):
        VerifiedModelGovernanceAuthority.from_verified_payload(
            inventory=_load_json(INVENTORY),
            profile=profile,
            allow_disabled_template=True,
            now=NOW,
        )


def test_unclassified_provider_cannot_form_enabled_authority(tmp_path: Path) -> None:
    profile = _base_profile("r7.router.core")
    profile["provider_bindings"][0]["provider_locality"] = "unclassified"
    profile["profile_content_sha256"] = _profile_digest(profile)
    profile_path, trust_path = _write_signed_profile(tmp_path, profile)

    with pytest.raises(ModelGovernancePolicyError, match="unclassified locality"):
        verify(
            inventory_path=INVENTORY,
            profile_path=profile_path,
            trust_store_path=trust_path,
            now=NOW,
        )


def test_inventory_hash_is_bound_to_profile() -> None:
    inventory = _load_json(INVENTORY)
    assert inventory_hash(inventory) == (
        "5ac58bede7c90e776d478f12b52c3f0f1db94c0975b68868b81787321cbc6fd5"
    )
