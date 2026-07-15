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
    VerifiedModelGovernanceAuthority,
    canonical_json,
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
    profile["callsite_bindings"] = [
        {
            "callsite_id": callsite_id,
            "gateway_id": call["gateway_id"],
            "classification_ceiling": call["classification_ceiling"],
            "usage_sink": call["usage_sink"],
            "audit_sink": call["audit_sink"],
            "agent_scope": call["agent_scope"],
            "model_artifact_id": "artifact.synthetic",
            "deployment_id": "deployment.synthetic",
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


def test_new_unregistered_inference_sink_is_blocked() -> None:
    rogue = ROOT / "services/_r7_unregistered_model_sink.py"
    rogue.write_text(
        "import httpx\n\n"
        "async def invoke(client: httpx.AsyncClient):\n"
        "    return await client.post('/chat/completions', json={})\n",
        encoding="utf-8",
    )
    try:
        with pytest.raises(ModelGovernancePolicyError, match="scanner mismatch"):
            verify(
                inventory_path=INVENTORY,
                profile_path=TEMPLATE,
                allow_disabled_template=True,
                now=NOW,
            )
    finally:
        rogue.unlink(missing_ok=True)


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
    )
    assert admitted.gateway_id == "csp-model-gateway"
    assert admitted.classification == "機密"
    assert admitted.agent_scope == ("router", "registered-agent")
    with pytest.raises(TypeError):
        authority.bindings["r7.router.core"] = authority.bindings["r7.router.core"]
    with pytest.raises(ModelGovernanceError, match="ceiling"):
        authority.authorize(
            "r7.router.core",
            "絕對機密",
            "artifact.synthetic",
            "deployment.synthetic",
            "router",
            NOW,
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
        )
    with pytest.raises(ModelGovernanceError, match="agent scope"):
        authority.authorize(
            "r7.router.core",
            "機密",
            "artifact.synthetic",
            "deployment.synthetic",
            None,
            NOW,
        )
    with pytest.raises(ModelGovernanceError, match="stale"):
        authority.authorize(
            "r7.router.core",
            "機密",
            "artifact.synthetic",
            "deployment.synthetic",
            "router",
            NOW + timedelta(seconds=61),
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


def test_inventory_hash_is_bound_to_profile() -> None:
    inventory = _load_json(INVENTORY)
    assert inventory_hash(inventory) == (
        "e49b3be132dcce46925c97d50c4d944887f0d552e1d04c7a02bbaad6512ccbfe"
    )
