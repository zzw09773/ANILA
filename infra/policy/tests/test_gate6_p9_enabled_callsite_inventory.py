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
    GOVERNANCE_INVENTORY_SCHEMA_V1,
    GOVERNANCE_PROFILE_SCHEMA_V1,
    TransportTarget,
    canonical_json,
    inventory_content_sha256,
    sha256_hex,
    transport_target_sha256,
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
README_P9 = ROOT / "infra/policy/gate6/README-p9.md"
NOW = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
_DIGEST_A = "sha256:" + "a" * 64
_DIGEST_B = "sha256:" + "b" * 64


def _profile_digest(profile: dict) -> str:
    unsigned = dict(profile)
    unsigned.pop("profile_content_sha256", None)
    unsigned.pop("signatures", None)
    return sha256_hex(unsigned)


def _base_profile(callsite_id: str = "r7.router.core", *, now: datetime = NOW) -> dict:
    inventory = _load_json(INVENTORY)
    profile = copy.deepcopy(_load_json(TEMPLATE))
    call = next(item for item in inventory["callsites"] if item["id"] == callsite_id)
    profile["enabled"] = True
    profile["enabled_callsites"] = [callsite_id]
    profile["disabled_callsites"] = [
        item["id"] for item in inventory["callsites"] if item["id"] != callsite_id
    ]
    profile["valid_from"] = (now - timedelta(minutes=5)).isoformat()
    profile["valid_until"] = (now + timedelta(days=7)).isoformat()
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
                "last_check": (now - timedelta(seconds=10)).isoformat(),
                "healthy": True,
                "ready": True,
            },
        }
    ]
    transport = TransportTarget.parse("synthetic-model:8000").to_dict()
    profile["provider_bindings"] = [
        {
            "provider_binding_id": "provider.synthetic",
            "model_registry_id": "model.synthetic",
            "model_registry_name": "synthetic-llm",
            "model_registry_revision": "registry-rev-1",
            "provider_locality": "internal_isolated",
            "transport_target": transport,
            "transport_target_sha256": transport_target_sha256(transport),
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


def _base_p0_profile(*, now: datetime = NOW) -> dict:
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
            "start": (now - timedelta(days=8)).isoformat(),
            "end": (now - timedelta(days=1)).isoformat(),
            "minimum_duration_seconds": 7 * 86400,
            "cadence": {"interval_seconds": 86400, "tolerance_seconds": 0},
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
        "valid_from": (now - timedelta(days=9)).isoformat(),
        "valid_until": (now + timedelta(days=30)).isoformat(),
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
    assert (
        first["source_inventory_sha256"] == first["signed_profile"]["inventory_sha256"]
    )
    assert (
        first["signed_profile_content_sha256"]
        == first["signed_profile"]["content_sha256"]
    )
    assert first["content_sha256"] == sha256_hex(
        {key: value for key, value in first.items() if key != "content_sha256"}
    )
    assert render_canonical_evidence(first) == render_canonical_evidence(second)


def test_enabled_evidence_rejects_signed_nonproduction_p0(tmp_path: Path) -> None:
    gate5_profile_path, gate5_trust_path = _write_signed_profile(
        tmp_path, _base_profile()
    )
    p0 = _base_p0_profile()
    p0["production_topology"]["environment"] = "development"
    p0["profile_content_sha256"] = production_profile_content_sha256(p0)
    p0_profile_path, p0_trust_path = _write_signed_p0_profile(tmp_path, p0)

    with pytest.raises(
        ModelGovernancePolicyError,
        match="production_topology.environment must be exactly production",
    ):
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


@pytest.mark.parametrize(
    ("target", "dns_policy", "locality", "egress_policy_id", "expected_kind"),
    [
        ("synthetic-model:8000", "none", "internal_isolated", None, "host_port"),
        (
            "https://api.provider.example/v1",
            "production_fail_closed",
            "external_governed",
            "egress.synthetic",
            "fqdn",
        ),
    ],
)
def test_v2_provider_snapshot_hashes_targets_without_raw_endpoint_leakage(
    tmp_path: Path,
    target: str,
    dns_policy: str,
    locality: str,
    egress_policy_id: str | None,
    expected_kind: str,
) -> None:
    profile = _base_profile()
    transport = TransportTarget.parse(target, dns_policy=dns_policy).to_dict()
    provider = profile["provider_bindings"][0]
    provider.update(
        {
            "provider_locality": locality,
            "transport_target": transport,
            "transport_target_sha256": transport_target_sha256(transport),
            "egress_policy_id": egress_policy_id,
        }
    )
    profile["profile_content_sha256"] = _profile_digest(profile)
    profile_path, trust_path = _write_signed_profile(tmp_path, profile)
    p0_profile_path, p0_trust_path = _write_signed_p0_profile(
        tmp_path, _base_p0_profile()
    )

    evidence = generate_evidence(
        inventory_path=INVENTORY,
        profile_path=profile_path,
        trust_store_path=trust_path,
        acceptance_profile_path=p0_profile_path,
        acceptance_trust_store_path=p0_trust_path,
        repo_root=ROOT,
        now=NOW,
        generated_at=NOW,
    )

    serialized = json.dumps(evidence, ensure_ascii=False)
    assert target not in serialized
    assert "api.provider.example" not in serialized
    call = evidence["enabled_callsites"][0]
    assert call["provider_binding_ids"] == ["provider.synthetic"]
    snapshot = call["provider_snapshots"][0]
    assert snapshot["provider_binding_id"] == "provider.synthetic"
    assert snapshot["transport_target_sha256"] == transport_target_sha256(transport)
    assert snapshot["transport_target_identity"]["kind"] == expected_kind
    assert snapshot["model_registry_id"] == "model.synthetic"
    assert snapshot["model_registry_name"] == "synthetic-llm"
    assert snapshot["model_registry_revision"] == "registry-rev-1"
    assert "transport_target" not in snapshot
    assert "upstream_transport_target" not in snapshot


def _configure_internal_shim_provider(profile: dict) -> tuple[dict, dict]:
    """Bind the synthetic callsite to an internal shim and external upstream."""

    transport = TransportTarget.parse("172.19.0.42:8443").to_dict()
    upstream = TransportTarget.parse(
        "https://upstream.provider.example:9443/managed-api",
        dns_policy="production_fail_closed",
    ).to_dict()
    profile["provider_bindings"][0].update(
        {
            "provider_locality": "internal_shim",
            "transport_target": transport,
            "transport_target_sha256": transport_target_sha256(transport),
            "upstream_provider_locality": "external_governed",
            "upstream_transport_target": upstream,
            "upstream_transport_target_sha256": transport_target_sha256(upstream),
            "egress_policy_id": None,
            "upstream_egress_policy_id": "egress.synthetic",
        }
    )
    return transport, upstream


def test_v2_shim_snapshot_is_complete_and_hides_both_transport_targets(
    tmp_path: Path,
) -> None:
    profile = _base_profile()
    transport, upstream = _configure_internal_shim_provider(profile)
    profile["profile_content_sha256"] = _profile_digest(profile)
    profile_path, trust_path = _write_signed_profile(tmp_path, profile)
    p0_profile_path, p0_trust_path = _write_signed_p0_profile(
        tmp_path, _base_p0_profile()
    )

    evidence = generate_evidence(
        inventory_path=INVENTORY,
        profile_path=profile_path,
        trust_store_path=trust_path,
        acceptance_profile_path=p0_profile_path,
        acceptance_trust_store_path=p0_trust_path,
        repo_root=ROOT,
        now=NOW,
        generated_at=NOW,
    )

    snapshot = evidence["enabled_callsites"][0]["provider_snapshots"][0]
    assert {
        "provider_binding_id",
        "model_registry_id",
        "model_registry_name",
        "model_registry_revision",
        "provider_locality",
        "transport_target_sha256",
        "transport_target_identity",
        "upstream_provider_locality",
        "upstream_transport_target_sha256",
        "upstream_transport_target_identity",
        "egress_policy_id",
        "upstream_egress_policy_id",
        "model_artifact_id",
        "artifact_digest",
        "artifact_revision",
        "deployment_id",
        "deployment_digest",
        "deployment_image_digest",
    } <= set(snapshot)
    assert snapshot["provider_locality"] == "internal_shim"
    assert snapshot["transport_target_sha256"] == transport_target_sha256(transport)
    assert snapshot["transport_target_identity"] == {
        "kind": "ip_literal",
        "scheme": None,
        "port_mode": "explicit_nondefault",
        "path_present": False,
    }
    assert snapshot["upstream_provider_locality"] == "external_governed"
    assert snapshot["upstream_transport_target_sha256"] == transport_target_sha256(
        upstream
    )
    assert snapshot["upstream_transport_target_identity"] == {
        "kind": "fqdn",
        "scheme": "https",
        "port_mode": "explicit_nondefault",
        "path_present": True,
    }
    assert snapshot["egress_policy_id"] is None
    assert snapshot["upstream_egress_policy_id"] == "egress.synthetic"
    assert evidence["provider_binding_ids"] == ["provider.synthetic"]
    assert evidence["provider_snapshots"] == [snapshot]

    serialized = json.dumps(evidence, ensure_ascii=False)
    for raw_target_part in (
        "172.19.0.42",
        "8443",
        "upstream.provider.example",
        "9443",
        "/managed-api",
    ):
        assert raw_target_part not in serialized


def test_enabled_profile_requires_p0_arguments(tmp_path: Path) -> None:
    profile_path, trust_path = _write_signed_profile(tmp_path, _base_profile())
    with pytest.raises(ModelGovernancePolicyError, match="acceptance-profile"):
        generate_evidence(
            inventory_path=INVENTORY,
            profile_path=profile_path,
            trust_store_path=trust_path,
            repo_root=ROOT,
            allow_disabled_template=True,
            now=NOW,
            generated_at=NOW,
        )

    acceptance_profile_path, acceptance_trust_path = _write_signed_p0_profile(
        tmp_path, _base_p0_profile()
    )
    evidence = generate_evidence(
        inventory_path=INVENTORY,
        profile_path=profile_path,
        trust_store_path=trust_path,
        acceptance_profile_path=acceptance_profile_path,
        acceptance_trust_store_path=acceptance_trust_path,
        repo_root=ROOT,
        allow_disabled_template=True,
        now=NOW,
        generated_at=NOW,
    )
    assert evidence["status"] == "VERIFIED"
    assert evidence["gate6_pass"] is False


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
    unsigned_profile_path, unsigned_trust_path = _write_signed_p0_profile(
        tmp_path, unsigned
    )
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
    assert evidence["provider_binding_ids"] == []
    assert evidence["provider_snapshots"] == []


def test_provider_target_hash_tamper_fails_closed(tmp_path: Path) -> None:
    profile = _base_profile()
    profile["provider_bindings"][0]["transport_target_sha256"] = "0" * 64
    profile["profile_content_sha256"] = _profile_digest(profile)
    profile_path, trust_path = _write_signed_profile(tmp_path, profile)
    with pytest.raises(
        ModelGovernancePolicyError,
        match="transport_target_sha256 mismatch",
    ):
        generate_evidence(
            inventory_path=INVENTORY,
            profile_path=profile_path,
            trust_store_path=trust_path,
            repo_root=ROOT,
            now=NOW,
        )


def test_provider_upstream_target_hash_tamper_fails_closed(tmp_path: Path) -> None:
    profile = _base_profile()
    _configure_internal_shim_provider(profile)
    profile["provider_bindings"][0]["upstream_transport_target_sha256"] = "0" * 64
    profile["profile_content_sha256"] = _profile_digest(profile)
    profile_path, trust_path = _write_signed_profile(tmp_path, profile)

    with pytest.raises(
        ModelGovernancePolicyError,
        match="upstream_transport_target_sha256 mismatch",
    ):
        generate_evidence(
            inventory_path=INVENTORY,
            profile_path=profile_path,
            trust_store_path=trust_path,
            repo_root=ROOT,
            now=NOW,
        )


@pytest.mark.parametrize(
    ("provider_binding_ids", "match"),
    [
        ([], "provider_binding_ids must not be empty"),
        (None, "callsite_bindings\\[0\\] has unknown or missing fields"),
        (["provider.unknown"], "references unknown provider binding"),
    ],
)
def test_v2_empty_missing_or_unknown_provider_ids_fail_closed(
    tmp_path: Path,
    provider_binding_ids: list[str] | None,
    match: str,
) -> None:
    profile = _base_profile()
    binding = profile["callsite_bindings"][0]
    if provider_binding_ids is None:
        del binding["provider_binding_ids"]
    else:
        binding["provider_binding_ids"] = provider_binding_ids
    profile["profile_content_sha256"] = _profile_digest(profile)
    profile_path, trust_path = _write_signed_profile(tmp_path, profile)

    with pytest.raises(ModelGovernancePolicyError, match=match):
        generate_evidence(
            inventory_path=INVENTORY,
            profile_path=profile_path,
            trust_store_path=trust_path,
            repo_root=ROOT,
            now=NOW,
        )


def test_v1_disabled_material_remains_audit_compatible(tmp_path: Path) -> None:
    inventory = copy.deepcopy(_load_json(INVENTORY))
    profile = copy.deepcopy(_load_json(TEMPLATE))
    inventory["schema_version"] = GOVERNANCE_INVENTORY_SCHEMA_V1
    for callsite in inventory["callsites"]:
        callsite.pop("provider_resolution")
    profile["schema_version"] = GOVERNANCE_PROFILE_SCHEMA_V1
    profile.pop("provider_bindings")
    profile["inventory_sha256"] = inventory_content_sha256(inventory)
    profile["profile_content_sha256"] = _profile_digest(profile)
    inventory_path = tmp_path / "inventory-v1.json"
    profile_path = tmp_path / "profile-v1.json"
    inventory_path.write_text(json.dumps(inventory), encoding="utf-8")
    profile_path.write_text(json.dumps(profile), encoding="utf-8")

    evidence = generate_evidence(
        inventory_path=inventory_path,
        profile_path=profile_path,
        repo_root=ROOT,
        allow_disabled_template=True,
        now=NOW,
        generated_at=NOW,
    )
    assert evidence["status"] == "NOT_ACCEPTANCE"
    assert evidence["enabled_callsites"] == []
    assert evidence["provider_binding_ids"] == []
    assert evidence["provider_snapshots"] == []


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


def test_cli_help_and_readme_reserve_fixed_generated_at_for_disabled_mode() -> None:
    help_result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert help_result.returncode == 0
    normalized_help = " ".join(help_result.stdout.split())
    assert "enabled evidence must omit --generated-at" in normalized_help
    assert "disabled NOT_ACCEPTANCE" in normalized_help

    readme = README_P9.read_text(encoding="utf-8")
    assert "enabled/production 模式必須省略 `--generated-at`" in readme
    assert "固定時間戳只允許用於 disabled `NOT_ACCEPTANCE`" in readme


def test_cli_enabled_profile_requires_and_emits_p0_evidence(tmp_path: Path) -> None:
    cli_now = datetime.now(timezone.utc)
    gate5_profile = _base_profile(now=cli_now)
    gate5_profile["valid_from"] = (cli_now - timedelta(days=1)).isoformat()
    gate5_profile["deployments"][0]["health_readiness"]["last_check"] = (
        cli_now - timedelta(seconds=10)
    ).isoformat()
    gate5_profile["profile_content_sha256"] = _profile_digest(gate5_profile)
    gate5_profile_path, gate5_trust_path = _write_signed_profile(
        tmp_path, gate5_profile
    )
    p0_profile_path, p0_trust_path = _write_signed_p0_profile(
        tmp_path, _base_p0_profile(now=cli_now)
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

    backdated = subprocess.run(
        [
            *common,
            "--acceptance-profile",
            str(p0_profile_path),
            "--acceptance-trust-store",
            str(p0_trust_path),
            "--generated-at",
            cli_now.isoformat(),
            "--output",
            str(tmp_path / "p9-backdated.json"),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert backdated.returncode == 1
    assert "must equal the verification instant" in backdated.stderr


@pytest.mark.parametrize(
    "generated_at",
    [NOW - timedelta(microseconds=1), NOW + timedelta(microseconds=1)],
)
def test_enabled_evidence_generated_at_must_equal_verification_instant(
    tmp_path: Path, generated_at: datetime
) -> None:
    profile_path, trust_path = _write_signed_profile(tmp_path, _base_profile())
    p0_profile_path, p0_trust_path = _write_signed_p0_profile(
        tmp_path, _base_p0_profile()
    )

    with pytest.raises(
        ModelGovernancePolicyError, match="must equal the verification instant"
    ):
        generate_evidence(
            inventory_path=INVENTORY,
            profile_path=profile_path,
            trust_store_path=trust_path,
            acceptance_profile_path=p0_profile_path,
            acceptance_trust_store_path=p0_trust_path,
            repo_root=ROOT,
            now=NOW,
            generated_at=generated_at,
        )


@pytest.mark.parametrize(
    "generated_at",
    [NOW, NOW.astimezone(timezone(timedelta(hours=8)))],
)
def test_enabled_evidence_accepts_equal_or_offset_equivalent_verification_instant(
    tmp_path: Path, generated_at: datetime
) -> None:
    profile_path, trust_path = _write_signed_profile(tmp_path, _base_profile())
    p0_profile_path, p0_trust_path = _write_signed_p0_profile(
        tmp_path, _base_p0_profile()
    )

    evidence = generate_evidence(
        inventory_path=INVENTORY,
        profile_path=profile_path,
        trust_store_path=trust_path,
        acceptance_profile_path=p0_profile_path,
        acceptance_trust_store_path=p0_trust_path,
        repo_root=ROOT,
        now=NOW,
        generated_at=generated_at,
    )

    assert evidence["status"] == "VERIFIED"
    assert evidence["generated_at"] == "2026-07-16T12:00:00Z"


def test_enabled_evidence_uses_single_captured_instant_when_now_and_generated_at_omitted(
    tmp_path: Path,
) -> None:
    before = datetime.now(timezone.utc)
    profile_path, trust_path = _write_signed_profile(
        tmp_path, _base_profile(now=before)
    )
    p0_profile_path, p0_trust_path = _write_signed_p0_profile(
        tmp_path, _base_p0_profile(now=before)
    )

    evidence = generate_evidence(
        inventory_path=INVENTORY,
        profile_path=profile_path,
        trust_store_path=trust_path,
        acceptance_profile_path=p0_profile_path,
        acceptance_trust_store_path=p0_trust_path,
        repo_root=ROOT,
    )
    after = datetime.now(timezone.utc)

    generated = datetime.fromisoformat(evidence["generated_at"].replace("Z", "+00:00"))
    assert evidence["status"] == "VERIFIED"
    assert before <= generated <= after


def test_disabled_template_keeps_deterministic_non_acceptance_timestamp() -> None:
    evidence = generate_evidence(
        inventory_path=INVENTORY,
        profile_path=TEMPLATE,
        repo_root=ROOT,
        allow_disabled_template=True,
        now=NOW,
        generated_at="2036-07-13T12:00:00+08:00",
    )

    assert evidence["status"] == "NOT_ACCEPTANCE"
    assert evidence["environment"] == "non-production"
    assert evidence["generated_at"] == "2036-07-13T04:00:00Z"


def test_enabled_evidence_rejects_naive_generated_at(tmp_path: Path) -> None:
    profile_path, trust_path = _write_signed_profile(tmp_path, _base_profile())
    p0_profile_path, p0_trust_path = _write_signed_p0_profile(
        tmp_path, _base_p0_profile()
    )

    with pytest.raises(
        ModelGovernancePolicyError, match="generated_at must include timezone"
    ):
        generate_evidence(
            inventory_path=INVENTORY,
            profile_path=profile_path,
            trust_store_path=trust_path,
            acceptance_profile_path=p0_profile_path,
            acceptance_trust_store_path=p0_trust_path,
            repo_root=ROOT,
            now=NOW,
            generated_at=datetime(2036, 7, 13, 12, 0),
        )
