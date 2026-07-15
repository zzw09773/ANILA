from __future__ import annotations

import base64
import copy
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "packages/anila-security/src"))

from anila_security.model_governance import (  # noqa: E402
    canonical_json,
    inventory_content_sha256,
    sha256_hex,
)
from infra.policy.gate5.check_model_governance import (  # noqa: E402
    ModelGovernancePolicyError,
    _load_json,
)
from infra.policy.gate6.generate_p9_enabled_callsite_inventory import (  # noqa: E402
    generate_evidence,
    render_canonical_evidence,
)
from anila_security.production_acceptance_profile import (  # noqa: E402
    PRODUCTION_ACCEPTANCE_SCHEMA,
    canonical_json as p0_canonical_json,
    production_profile_content_sha256,
)


INVENTORY = ROOT / "infra/policy/gate5/model-governance-inventory.v1.json"
TEMPLATE = ROOT / "infra/policy/gate5/model-governance-profile.disabled-template.json"
SCRIPT = ROOT / "infra/policy/gate6/generate_p9_enabled_callsite_inventory.py"
NOW = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
_DIGEST_A = "sha256:" + "a" * 64
_DIGEST_B = "sha256:" + "b" * 64


def _profile_digest(profile: dict) -> str:
    unsigned = dict(profile)
    unsigned.pop("profile_content_sha256", None)
    unsigned.pop("signatures", None)
    return sha256_hex(unsigned)


def _base_profile(callsite_id: str = "r7.router.core") -> dict:
    inventory = _load_json(INVENTORY)
    profile = copy.deepcopy(_load_json(TEMPLATE))
    call = next(item for item in inventory["callsites"] if item["id"] == callsite_id)
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


_P0_ROLES = ("system_owner", "data_owner", "pki_owner", "security", "operations")


def _base_p0_profile() -> dict:
    inventory = _load_json(INVENTORY)
    profile = {
        "schema_version": PRODUCTION_ACCEPTANCE_SCHEMA,
        "profile_id": "production-acceptance.synthetic",
        "profile_version": "2026-07-16.1",
        "enabled": True,
        "template_only": False,
        "approval_status": "approved",
        "production_topology": {
            "topology_id": "topology.synthetic",
            "environment": "production",
            "regions": ["airgap-a"],
            "network_zones": ["csp", "data", "models"],
            "services": ["csp", "router", "worker"],
            "egress_policy": "csp_only",
            "artifact_hashes": {"release": "a" * 64, "ca_bundle": "b" * 64},
        },
        "enabled_features": ["chat", "retrieval"],
        "disabled_features": ["studio", "flux", "export"],
        "data_classification_ceiling": "營業秘密",
        "rto_rpo": {"rto_seconds": 3600, "rpo_seconds": 900},
        "slo_thresholds": {
            "ingestion_p99_ms": 10_000,
            "dispatch_success_rate": 0.99,
            "queue_age_seconds": 300,
            "stuck_job_count": 0,
            "artifact_download_success_rate": 0.99,
            "auth_error_rate": 0.01,
        },
        "load_profile": {
            "profile_id": "load.synthetic",
            "concurrency": 10,
            "requests_per_second": 2.5,
            "duration_seconds": 3600,
            "workflow_ids": ["chat-basic", "retrieval-basic"],
        },
        "observation_window": {
            "start": (NOW - timedelta(days=8)).isoformat(),
            "end": (NOW - timedelta(days=1)).isoformat(),
            "minimum_duration_seconds": 7 * 86400,
        },
        "workflow_matrix": [
            {
                "workflow_id": "chat-basic",
                "auth_method": "card",
                "classification": "營業秘密",
                "compartment": "default",
                "positive_fixture": "wf-positive-1",
                "negative_fixture": "wf-negative-1",
            },
            {
                "workflow_id": "retrieval-basic",
                "auth_method": "card",
                "classification": "無機密",
                "compartment": "default",
                "positive_fixture": "wf-positive-2",
                "negative_fixture": "wf-negative-2",
            },
        ],
        "p5_sample_n": 100,
        "enabled_inference_callsite_inventory": {
            "schema_version": inventory["schema_version"],
            "version": inventory["inventory_version"],
            "sha256": inventory_content_sha256(inventory),
            "callsite_ids": ["r7.router.core"],
        },
        "revocation_sla": {"token_seconds": 60, "card_seconds": 300},
        "pki_policy": {
            "stale_after_seconds": 3600,
            "offline_behavior": "fail_closed",
            "missing_behavior": "fail_closed",
            "refresh_failure_behavior": "fail_closed",
        },
        "severity_taxonomy": {
            "sev1": {
                "definition": ["auth bypass", "classification bypass"],
                "ack_seconds": 300,
                "mitigate_seconds": 3600,
            },
            "sev2": {
                "definition": ["material degradation"],
                "ack_seconds": 1800,
                "mitigate_seconds": 86_400,
            },
        },
        "finding_acceptance_rule": {
            "critical": "reject",
            "high": "reject",
            "medium": "conditional_accept",
            "low": "accept",
            "blocked_categories": ["auth", "classification", "egress"],
            "conditional_requirements": [
                "owner",
                "expiry",
                "compensating_control",
                "independent_signoff",
            ],
        },
        "revalidation_impact_matrix": {
            "version": "impact-2026-07-16.1",
            "sha256": "d" * 64,
        },
        "valid_from": (NOW - timedelta(days=9)).isoformat(),
        "valid_until": (NOW + timedelta(days=30)).isoformat(),
        "signer_roles": list(_P0_ROLES),
        "signatures": [],
    }
    profile["profile_content_sha256"] = production_profile_content_sha256(profile)
    return profile


def _write_signed_p0_profile(tmp_path: Path, profile: dict) -> tuple[Path, Path]:
    keys = {role: Ed25519PrivateKey.generate() for role in _P0_ROLES}
    trusted = {
        role: key.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("ascii")
        for role, key in keys.items()
    }
    payload = dict(profile)
    payload.pop("signatures", None)
    profile["signatures"] = [
        {
            "role": role,
            "signature": base64.b64encode(
                keys[role].sign(p0_canonical_json(payload))
            ).decode("ascii"),
        }
        for role in _P0_ROLES
    ]
    profile_path = tmp_path / "acceptance-profile.json"
    trust_path = tmp_path / "acceptance-trust.json"
    profile_path.write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")
    trust_path.write_text(json.dumps({"trusted_signers": trusted}), encoding="utf-8")
    return profile_path, trust_path


def test_signed_profile_export_is_canonical_and_deterministic(tmp_path: Path) -> None:
    profile_path, trust_path = _write_signed_profile(tmp_path, _base_profile())
    acceptance_profile_path, acceptance_trust_path = _write_signed_p0_profile(
        tmp_path, _base_p0_profile()
    )
    first = generate_evidence(
        inventory_path=INVENTORY,
        profile_path=profile_path,
        trust_store_path=trust_path,
        acceptance_profile_path=acceptance_profile_path,
        acceptance_trust_store_path=acceptance_trust_path,
        repo_root=ROOT,
        now=NOW,
        generated_at=NOW,
    )
    second = generate_evidence(
        inventory_path=INVENTORY,
        profile_path=profile_path,
        trust_store_path=trust_path,
        acceptance_profile_path=acceptance_profile_path,
        acceptance_trust_store_path=acceptance_trust_path,
        repo_root=ROOT,
        now=NOW,
        generated_at=NOW,
    )
    assert first == second
    assert first["schema_version"].startswith("anila.gate6.p9.")
    assert first["status"] == "VERIFIED"
    assert first["environment"] == "production"
    assert first["gate6_pass"] is False
    assert first["acceptance_profile_id"] == "production-acceptance.synthetic"
    assert first["acceptance_profile_version"] == "2026-07-16.1"
    assert first["acceptance_profile_content_sha256"]
    assert set(first["acceptance_profile_signer_roles"]) == set(_P0_ROLES)
    assert [item["id"] for item in first["enabled_callsites"]] == sorted(
        item["id"] for item in first["enabled_callsites"]
    )
    call = first["enabled_callsites"][0]
    assert {
        "category",
        "component",
        "path",
        "gateway",
        "raw_endpoint",
        "artifact_digest",
        "deployment_digest",
    } <= set(call)
    assert first["source_inventory_sha256"] == first["signed_profile"]["inventory_sha256"]
    assert first["signed_profile_content_sha256"] == first["signed_profile"]["content_sha256"]
    assert first["content_sha256"] == sha256_hex(
        {key: value for key, value in first.items() if key != "content_sha256"}
    )
    assert render_canonical_evidence(first) == render_canonical_evidence(second)


def test_enabled_profile_requires_p0_arguments(tmp_path: Path) -> None:
    profile_path, trust_path = _write_signed_profile(tmp_path, _base_profile())
    with pytest.raises(ModelGovernancePolicyError, match="acceptance-profile"):
        generate_evidence(
            inventory_path=INVENTORY,
            profile_path=profile_path,
            trust_store_path=trust_path,
            repo_root=ROOT,
            now=NOW,
            generated_at=NOW,
        )


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("schema_version", "anila.gate5.other.v1", "schema_version mismatch"),
        ("version", "inventory-other", "version mismatch"),
        ("sha256", "0" * 64, "hash mismatch"),
        ("callsite_ids", ["r7.csp.memory"], "callsite_ids mismatch"),
    ],
)
def test_p0_inventory_projection_must_match_gate5_authority(
    tmp_path: Path, field: str, value: object, match: str
) -> None:
    gate5_profile_path, gate5_trust_path = _write_signed_profile(
        tmp_path, _base_profile()
    )
    p0 = _base_p0_profile()
    p0["enabled_inference_callsite_inventory"][field] = value
    p0["profile_content_sha256"] = production_profile_content_sha256(p0)
    p0_profile_path, p0_trust_path = _write_signed_p0_profile(tmp_path, p0)
    with pytest.raises(ModelGovernancePolicyError, match=match):
        generate_evidence(
            inventory_path=INVENTORY,
            profile_path=gate5_profile_path,
            trust_store_path=gate5_trust_path,
            acceptance_profile_path=p0_profile_path,
            acceptance_trust_store_path=p0_trust_path,
            repo_root=ROOT,
            now=NOW,
            generated_at=NOW,
        )


def test_tampered_or_unsigned_p0_profile_fails_closed(tmp_path: Path) -> None:
    gate5_profile_path, gate5_trust_path = _write_signed_profile(
        tmp_path, _base_profile()
    )
    p0 = _base_p0_profile()
    p0_profile_path, p0_trust_path = _write_signed_p0_profile(tmp_path, p0)
    tampered = json.loads(p0_profile_path.read_text(encoding="utf-8"))
    tampered["profile_version"] = "tampered"
    p0_profile_path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(ModelGovernancePolicyError, match="profile_content_sha256"):
        generate_evidence(
            inventory_path=INVENTORY,
            profile_path=gate5_profile_path,
            trust_store_path=gate5_trust_path,
            acceptance_profile_path=p0_profile_path,
            acceptance_trust_store_path=p0_trust_path,
            repo_root=ROOT,
            now=NOW,
            generated_at=NOW,
        )

    unsigned = _base_p0_profile()
    unsigned["signatures"] = []
    unsigned_profile_path, unsigned_trust_path = _write_signed_p0_profile(tmp_path, unsigned)
    unsigned_value = json.loads(unsigned_profile_path.read_text(encoding="utf-8"))
    unsigned_value["signatures"] = []
    unsigned_profile_path.write_text(json.dumps(unsigned_value), encoding="utf-8")
    with pytest.raises(ModelGovernancePolicyError, match="five signatures"):
        generate_evidence(
            inventory_path=INVENTORY,
            profile_path=gate5_profile_path,
            trust_store_path=gate5_trust_path,
            acceptance_profile_path=unsigned_profile_path,
            acceptance_trust_store_path=unsigned_trust_path,
            repo_root=ROOT,
            now=NOW,
            generated_at=NOW,
        )


def test_disabled_template_is_rejected_by_default() -> None:
    with pytest.raises(ModelGovernancePolicyError, match="disabled template"):
        generate_evidence(
            inventory_path=INVENTORY,
            profile_path=TEMPLATE,
            repo_root=ROOT,
            now=NOW,
            generated_at=NOW,
        )


def test_disabled_template_test_flag_is_non_production_and_empty() -> None:
    evidence = generate_evidence(
        inventory_path=INVENTORY,
        profile_path=TEMPLATE,
        repo_root=ROOT,
        allow_disabled_template=True,
        now=NOW,
        generated_at=NOW,
    )
    assert evidence["status"] == "NOT_ACCEPTANCE"
    assert evidence["acceptance_status"] == "NOT_ACCEPTANCE"
    assert evidence["environment"] == "non-production"
    assert evidence["profile_enabled"] is False
    assert evidence["gate6_pass"] is False
    assert evidence["enabled_callsites"] == []
    assert evidence["artifact_digests"] == []
    assert evidence["deployment_digests"] == []


def test_tampered_profile_fails_closed(tmp_path: Path) -> None:
    profile_path, trust_path = _write_signed_profile(tmp_path, _base_profile())
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    profile["profile_version"] = "tampered"
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    with pytest.raises(ModelGovernancePolicyError, match="profile_content_sha256"):
        generate_evidence(
            inventory_path=INVENTORY,
            profile_path=profile_path,
            trust_store_path=trust_path,
            repo_root=ROOT,
            now=NOW,
            generated_at=NOW,
        )


def test_inventory_hash_mismatch_fails_closed(tmp_path: Path) -> None:
    inventory = _load_json(INVENTORY)
    inventory["inventory_version"] = "tampered"
    inventory_path = tmp_path / "inventory.json"
    inventory_path.write_text(json.dumps(inventory), encoding="utf-8")
    with pytest.raises(ModelGovernancePolicyError, match="inventory_sha256 mismatch"):
        generate_evidence(
            inventory_path=inventory_path,
            profile_path=TEMPLATE,
            repo_root=ROOT,
            allow_disabled_template=True,
            now=NOW,
            generated_at=NOW,
        )


def test_unknown_enabled_callsite_fails_closed(tmp_path: Path) -> None:
    profile = _base_profile()
    profile["enabled_callsites"] = ["unknown.callsite"]
    profile["profile_content_sha256"] = _profile_digest(profile)
    profile_path, trust_path = _write_signed_profile(tmp_path, profile)
    with pytest.raises(ModelGovernancePolicyError, match="partition|unknown"):
        generate_evidence(
            inventory_path=INVENTORY,
            profile_path=profile_path,
            trust_store_path=trust_path,
            repo_root=ROOT,
            now=NOW,
            generated_at=NOW,
        )


def test_cli_exit_code_and_non_production_marker(tmp_path: Path) -> None:
    rejected = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--repo-root",
            str(ROOT),
            "--inventory",
            str(INVENTORY),
            "--profile",
            str(TEMPLATE),
            "--generated-at",
            "2026-07-15T12:00:00Z",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert rejected.returncode == 1
    assert "disabled template" in rejected.stderr

    output = tmp_path / "p9.json"
    accepted_for_test = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--repo-root",
            str(ROOT),
            "--inventory",
            str(INVENTORY),
            "--profile",
            str(TEMPLATE),
            "--test-only-allow-disabled-template",
            "--generated-at",
            "2026-07-15T12:00:00Z",
            "--output",
            str(output),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert accepted_for_test.returncode == 0, accepted_for_test.stderr
    evidence = json.loads(output.read_text(encoding="utf-8"))
    assert evidence["acceptance_status"] == "NOT_ACCEPTANCE"
    assert evidence["environment"] == "non-production"


def test_cli_enabled_profile_requires_and_emits_p0_evidence(tmp_path: Path) -> None:
    gate5_profile = _base_profile()
    gate5_profile["valid_from"] = (NOW - timedelta(days=1)).isoformat()
    gate5_profile["deployments"][0]["health_readiness"]["last_check"] = (
        datetime.now(timezone.utc) - timedelta(seconds=10)
    ).isoformat()
    gate5_profile["profile_content_sha256"] = _profile_digest(gate5_profile)
    gate5_profile_path, gate5_trust_path = _write_signed_profile(
        tmp_path, gate5_profile
    )
    p0_profile_path, p0_trust_path = _write_signed_p0_profile(
        tmp_path, _base_p0_profile()
    )
    common = [
        sys.executable,
        str(SCRIPT),
        "--repo-root",
        str(ROOT),
        "--inventory",
        str(INVENTORY),
        "--profile",
        str(gate5_profile_path),
        "--trust-store",
        str(gate5_trust_path),
        "--generated-at",
        "2026-07-16T12:00:00Z",
    ]
    missing = subprocess.run(
        common,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert missing.returncode == 1
    assert "acceptance-profile" in missing.stderr

    output = tmp_path / "p9-enabled.json"
    complete = subprocess.run(
        [
            *common,
            "--acceptance-profile",
            str(p0_profile_path),
            "--acceptance-trust-store",
            str(p0_trust_path),
            "--output",
            str(output),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert complete.returncode == 0, complete.stderr
    evidence = json.loads(output.read_text(encoding="utf-8"))
    assert evidence["status"] == "VERIFIED"
    assert evidence["acceptance_profile_id"] == "production-acceptance.synthetic"
