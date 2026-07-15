from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from anila_security.production_acceptance_profile import (
    PRODUCTION_ACCEPTANCE_SCHEMA,
    REQUIRED_PRODUCTION_SIGNER_ROLES,
    ProductionAcceptanceProfileError,
    canonical_json,
    production_profile_content_sha256,
    validate_production_acceptance_profile,
    verify_production_acceptance_profile,
    verify_signed_production_acceptance_profile,
)


NOW = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
ROLES = ("system_owner", "data_owner", "pki_owner", "security", "operations")


def _base_profile() -> dict:
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
            "schema_version": "anila.gate5.inference-inventory.v1",
            "version": "inventory-2026-07-16.1",
            "sha256": "c" * 64,
            "callsite_ids": ["router.primary", "csp.embedding"],
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
                "owner", "expiry", "compensating_control", "independent_signoff"
            ],
        },
        "revalidation_impact_matrix": {"version": "impact-2026-07-16.1", "sha256": "d" * 64},
        "valid_from": (NOW - timedelta(days=9)).isoformat(),
        "valid_until": (NOW + timedelta(days=30)).isoformat(),
        "signer_roles": list(ROLES),
    }
    profile["profile_content_sha256"] = production_profile_content_sha256(profile)
    profile["signatures"] = []
    return profile


def _signed_fixture(*, same_key: bool = False) -> tuple[dict, dict, dict]:
    profile = _base_profile()
    keys: dict[str, Ed25519PrivateKey] = {}
    shared = Ed25519PrivateKey.generate() if same_key else None
    for role in ROLES:
        keys[role] = shared or Ed25519PrivateKey.generate()
    trust = {
        "trusted_signers": {
            role: key.public_key()
            .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
            .decode("ascii")
            for role, key in keys.items()
        }
    }
    payload = dict(profile)
    payload.pop("signatures")
    profile["signatures"] = [
        {"role": role, "signature": base64.b64encode(keys[role].sign(canonical_json(payload))).decode("ascii")}
        for role in ROLES
    ]
    return profile, trust, keys


def _resign(profile: dict, keys: dict[str, Ed25519PrivateKey]) -> None:
    profile["profile_content_sha256"] = production_profile_content_sha256(profile)
    payload = dict(profile)
    payload.pop("signatures", None)
    profile["signatures"] = [
        {
            "role": role,
            "signature": base64.b64encode(keys[role].sign(canonical_json(payload))).decode(
                "ascii"
            ),
        }
        for role in ROLES
    ]


def test_five_party_signed_profile_verifies_with_temporary_test_keys() -> None:
    profile, trust, _ = _signed_fixture()
    verified = verify_production_acceptance_profile(profile, trust, now=NOW)

    assert verified.enabled is True
    assert set(verified.signer_roles) == REQUIRED_PRODUCTION_SIGNER_ROLES
    assert verified.data_classification_ceiling == "營業秘密"
    assert verified.inventory_version == "inventory-2026-07-16.1"
    assert verified.profile_content_sha256 == production_profile_content_sha256(profile)


def test_canonical_json_is_stable_and_unicode_preserving() -> None:
    assert canonical_json({"b": 1, "a": "營業秘密"}) == '{"a":"營業秘密","b":1}'.encode()
    assert canonical_json({"a": 1}) == canonical_json({"a": 1})


@pytest.mark.parametrize(
    "mutation",
    [
        lambda p: p.pop("workflow_matrix"),
        lambda p: p.update({"unexpected": True}),
        lambda p: p["slo_thresholds"].update({"unknown": 1}),
    ],
)
def test_missing_or_unknown_fields_fail_closed(mutation) -> None:
    profile, trust, _ = _signed_fixture()
    mutation(profile)
    with pytest.raises(ProductionAcceptanceProfileError, match="unknown or missing"):
        verify_production_acceptance_profile(profile, trust, now=NOW)


def test_any_missing_role_signature_is_rejected() -> None:
    profile, trust, _ = _signed_fixture()
    profile["signatures"] = profile["signatures"][:-1]
    with pytest.raises(ProductionAcceptanceProfileError, match="exactly five signatures"):
        verify_production_acceptance_profile(profile, trust, now=NOW)


def test_wrong_signature_and_shared_key_are_rejected() -> None:
    profile, trust, keys = _signed_fixture()
    profile["signatures"][0]["signature"] = base64.b64encode(
        Ed25519PrivateKey.generate().sign(canonical_json({"wrong": True}))
    ).decode("ascii")
    with pytest.raises(ProductionAcceptanceProfileError, match="invalid signature"):
        verify_production_acceptance_profile(profile, trust, now=NOW)

    shared_profile, shared_trust, _ = _signed_fixture(same_key=True)
    with pytest.raises(ProductionAcceptanceProfileError, match="distinct keys"):
        verify_production_acceptance_profile(shared_profile, shared_trust, now=NOW)


def test_artifact_or_profile_hash_tamper_is_rejected() -> None:
    profile, trust, _ = _signed_fixture()
    profile["production_topology"]["artifact_hashes"]["release"] = "e" * 64
    with pytest.raises(ProductionAcceptanceProfileError, match="profile_content_sha256 mismatch"):
        verify_production_acceptance_profile(profile, trust, now=NOW)

    profile, trust, _ = _signed_fixture()
    profile["profile_content_sha256"] = "f" * 64
    with pytest.raises(ProductionAcceptanceProfileError, match="profile_content_sha256 mismatch"):
        verify_production_acceptance_profile(profile, trust, now=NOW)


def test_disabled_template_is_never_a_production_approval() -> None:
    profile, trust, _ = _signed_fixture()
    profile["enabled"] = False
    profile["template_only"] = True
    profile["approval_status"] = "disabled_template"
    with pytest.raises(ProductionAcceptanceProfileError, match="not a production approval"):
        verify_production_acceptance_profile(profile, trust, now=NOW)


def test_repository_disabled_template_is_rejected_by_path_verifier(tmp_path: Path) -> None:
    trust_path = tmp_path / "trust.json"
    trust_path.write_text(
        json.dumps({"trusted_signers": {role: "not-a-key" for role in ROLES}}),
        encoding="utf-8",
    )
    template_path = (
        Path(__file__).parents[3]
        / "infra"
        / "policy"
        / "gate6"
        / "production-acceptance-profile.disabled-template.json"
    )
    with pytest.raises(ProductionAcceptanceProfileError, match="not a production approval"):
        verify_signed_production_acceptance_profile(
            template_path, trust_path, now=NOW
        )


def test_observation_window_requires_seven_days_and_validity_containment() -> None:
    profile, trust, _ = _signed_fixture()
    profile["observation_window"]["minimum_duration_seconds"] = 7 * 86400 - 1
    with pytest.raises(ProductionAcceptanceProfileError, match="outside allowed range"):
        verify_production_acceptance_profile(profile, trust, now=NOW)

    profile, trust, _ = _signed_fixture()
    profile["observation_window"]["end"] = (
        datetime.fromisoformat(profile["observation_window"]["start"]) + timedelta(days=6)
    ).isoformat()
    with pytest.raises(ProductionAcceptanceProfileError, match="seven days"):
        verify_production_acceptance_profile(profile, trust, now=NOW)

    profile, trust, _ = _signed_fixture()
    profile["observation_window"]["start"] = (
        datetime.fromisoformat(profile["valid_from"]) - timedelta(seconds=1)
    ).isoformat()
    with pytest.raises(ProductionAcceptanceProfileError, match="contained"):
        verify_production_acceptance_profile(profile, trust, now=NOW)


def test_profile_can_be_signed_before_a_future_observation_window() -> None:
    profile, trust, keys = _signed_fixture()
    profile["observation_window"] = {
        "start": (NOW + timedelta(days=1)).isoformat(),
        "end": (NOW + timedelta(days=8)).isoformat(),
        "minimum_duration_seconds": 7 * 86400,
    }
    _resign(profile, keys)
    verified = verify_production_acceptance_profile(profile, trust, now=NOW)
    assert verified.observation_start > NOW
    assert verified.observation_end <= verified.valid_until
    validate_production_acceptance_profile(profile, now=NOW)


def test_pki_must_fail_closed() -> None:
    profile, trust, _ = _signed_fixture()
    profile["pki_policy"]["offline_behavior"] = "fail_open"
    with pytest.raises(ProductionAcceptanceProfileError, match="fail_closed"):
        verify_production_acceptance_profile(profile, trust, now=NOW)


@pytest.mark.parametrize(
    ("path", "value", "match"),
    [
        (("p5_sample_n",), 0, "outside allowed range"),
        (("rto_rpo", "rto_seconds"), True, "must be an integer"),
        (("slo_thresholds", "dispatch_success_rate"), 0, "outside allowed range"),
        (("valid_until",), (NOW - timedelta(seconds=1)).isoformat(), "currently effective"),
    ],
)
def test_time_and_numeric_boundaries_are_rejected(path, value, match) -> None:
    profile, trust, _ = _signed_fixture()
    target = profile
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ProductionAcceptanceProfileError, match=match):
        verify_production_acceptance_profile(profile, trust, now=NOW)


def test_inventory_expectations_are_bound_out_of_band() -> None:
    profile, trust, _ = _signed_fixture()
    with pytest.raises(ProductionAcceptanceProfileError, match="inventory hash mismatch"):
        verify_production_acceptance_profile(
            profile, trust, now=NOW, expected_inventory_sha256="1" * 64
        )
    with pytest.raises(ProductionAcceptanceProfileError, match="inventory version mismatch"):
        verify_production_acceptance_profile(
            profile, trust, now=NOW, expected_inventory_version="inventory-other"
        )


def test_unsigned_profile_is_rejected_even_when_shape_is_valid() -> None:
    profile, trust, _ = _signed_fixture()
    profile["signatures"] = []
    with pytest.raises(ProductionAcceptanceProfileError, match="exactly five signatures"):
        verify_production_acceptance_profile(profile, trust, now=NOW)


def test_signer_role_list_cannot_be_replaced_by_subset() -> None:
    profile, trust, _ = _signed_fixture()
    profile["signer_roles"] = list(ROLES[:-1])
    with pytest.raises(ProductionAcceptanceProfileError, match="exactly the five"):
        verify_production_acceptance_profile(profile, trust, now=NOW)
