from __future__ import annotations

import base64
import copy
import gc
import hashlib
import json
import os
import shutil
import stat
from collections.abc import Mapping
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from anila_security.production_acceptance_profile import (
    PRODUCTION_ACCEPTANCE_SCHEMA,
    canonical_json,
    production_profile_content_sha256,
)
import anila_security.release_envelope as release_module
from anila_security.release_envelope import (
    RELEASE_ENVELOPE_ACCEPTANCE_STATUS,
    RELEASE_ENVELOPE_ERROR_DUPLICATE_JSON_KEY,
    RELEASE_ENVELOPE_ERROR_INVALID_BUNDLE,
    RELEASE_ENVELOPE_ERROR_INVALID_JSON,
    RELEASE_ENVELOPE_ERROR_INVALID_P0,
    RELEASE_ENVELOPE_ERROR_INVALID_SIGNATURE,
    RELEASE_ENVELOPE_ERROR_INVALID_TRUST,
    RELEASE_ENVELOPE_ERROR_NOT_EFFECTIVE,
    RELEASE_ENVELOPE_ERROR_RESOURCE_LIMIT,
    RELEASE_ENVELOPE_MAX_JSON_DEPTH,
    RELEASE_ENVELOPE_MISSING_EXTERNAL_EVIDENCE,
    RELEASE_ENVELOPE_SCHEMA,
    RELEASE_ENVELOPE_STATUS,
    RELEASE_TRUST_STORE_SCHEMA,
    ReleaseEnvelopeError,
    canonical_release_json,
    read_release_envelope,
    release_envelope_content_sha256,
    verify_release_envelope,
    verify_signed_release_envelope,
)


NOW = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
ROLES = ("system_owner", "data_owner", "pki_owner", "security", "operations")


def _p0_fixture() -> tuple[dict, dict, dict, dict]:
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
        "disabled_features": ["export", "flux", "studio"],
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
            "cadence": {"interval_seconds": 86400, "tolerance_seconds": 0},
        },
        "workflow_matrix": [
            {
                "workflow_id": "chat-basic",
                "auth_method": "card",
                "classification": "營業秘密",
                "compartment": "default",
                "positive_fixture": "positive-chat",
                "negative_fixture": "negative-chat",
            },
            {
                "workflow_id": "retrieval-basic",
                "auth_method": "card",
                "classification": "無機密",
                "compartment": "default",
                "positive_fixture": "positive-retrieval",
                "negative_fixture": "negative-retrieval",
            },
        ],
        "p5_sample_n": 100,
        "enabled_inference_callsite_inventory": {
            "schema_version": "anila.gate5.inference-inventory.v1",
            "version": "inventory-2026-07-16.1",
            "sha256": "c" * 64,
            "callsite_ids": ["csp.embedding", "router.primary"],
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
                "compensating_control",
                "expiry",
                "independent_signoff",
                "owner",
            ],
        },
        "revalidation_impact_matrix": {"version": "impact-2026-07-16.1", "sha256": "d" * 64},
        "valid_from": (NOW - timedelta(days=9)).isoformat(),
        "valid_until": (NOW + timedelta(days=30)).isoformat(),
        "signer_roles": list(ROLES),
    }
    inventory = {
        "schema_version": "anila.gate5.inference-inventory.v1",
        "version": "inventory-2026-07-16.1",
        "callsite_ids": ["csp.embedding", "router.primary"],
    }
    profile["enabled_inference_callsite_inventory"]["sha256"] = hashlib.sha256(
        canonical_release_json(inventory)
    ).hexdigest()
    profile["profile_content_sha256"] = production_profile_content_sha256(profile)
    keys = {role: Ed25519PrivateKey.generate() for role in ROLES}
    trust = {
        "trusted_signers": {
            role: key.public_key()
            .public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
            )
            .decode("ascii")
            for role, key in keys.items()
        }
    }
    payload = dict(profile)
    profile["signatures"] = [
        {
            "role": role,
            "signature": base64.b64encode(keys[role].sign(canonical_json(payload))).decode("ascii"),
        }
        for role in ROLES
    ]
    return profile, trust, inventory, keys


def _release_fixture(tmp_path: Path) -> tuple[dict, dict, Path, dict, dict, dict]:
    p0, p0_trust, p0_inventory, _p0_keys = _p0_fixture()
    root = tmp_path / "bundle"
    root.mkdir()
    (root / "ca.pem").write_bytes(b"synthetic-ca")
    (root / "image.tar").write_bytes(b"synthetic-image")
    ca_hash = hashlib.sha256(b"synthetic-ca").hexdigest()
    image_hash = hashlib.sha256(b"synthetic-image").hexdigest()
    topology_hash = hashlib.sha256(canonical_release_json(p0["production_topology"])).hexdigest()
    envelope = {
        "schema_version": RELEASE_ENVELOPE_SCHEMA,
        "envelope_id": "release.synthetic",
        "envelope_version": "2026.07.16.1",
        "code_line": "main",
        "code_commit": "a" * 40,
        "p0_profile": {
            "profile_id": p0["profile_id"],
            "profile_version": p0["profile_version"],
            "profile_content_sha256": p0["profile_content_sha256"],
            "inventory_version": p0["enabled_inference_callsite_inventory"]["version"],
            "inventory_sha256": p0["enabled_inference_callsite_inventory"]["sha256"],
        },
        "release_owner": {"owner_id": "owner.synthetic", "key_id": "key.synthetic"},
        "bundle_inventory": [
            {"path": "ca.pem", "sha256": ca_hash, "size": len(b"synthetic-ca")},
            {"path": "image.tar", "sha256": image_hash, "size": len(b"synthetic-image")},
        ],
        "platform_images": {"csp": f"sha256:{'1' * 64}"},
        "enabled_models": {"llm": f"sha256:{'2' * 64}"},
        "enabled_artifacts": {"release": f"sha256:{'3' * 64}"},
        "sbom": [{"component_id": "component.synthetic", "digest": f"sha256:{'4' * 64}"}],
        "ca_bundle": {"path": "ca.pem", "sha256": ca_hash},
        "deployment_config": {"sha256": "5" * 64},
        "topology": {"topology_id": "topology.synthetic", "sha256": topology_hash},
        "features": {
            "enabled": sorted(p0["enabled_features"]),
            "disabled": sorted(p0["disabled_features"]),
        },
        "data_classification_ceiling": p0["data_classification_ceiling"],
        "startup_posture": ["csp_only", "fail_closed"],
        "generated_at": (NOW - timedelta(days=1)).isoformat(),
        "valid_from": NOW.isoformat(),
        "valid_until": (NOW + timedelta(days=10)).isoformat(),
        "signature": "placeholder",
    }
    # Deterministic synthetic key only: tests can re-sign controlled mutations
    # without ever treating the fixture as production trust material.
    release_key = Ed25519PrivateKey.from_private_bytes(b"r" * 32)
    release_trust = {
        "schema_version": RELEASE_TRUST_STORE_SCHEMA,
        "trusted_release_owners": [
            {
                "owner_id": "owner.synthetic",
                "key_id": "key.synthetic",
                "public_key": release_key.public_key()
                .public_bytes(
                    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
                )
                .decode("ascii"),
            }
        ],
    }
    envelope["content_sha256"] = release_envelope_content_sha256(envelope)
    signature_payload = dict(envelope)
    signature_payload.pop("signature")
    envelope["signature"] = base64.b64encode(
        release_key.sign(canonical_release_json(signature_payload))
    ).decode("ascii")
    return envelope, release_trust, root, p0, p0_trust, p0_inventory


def _resign_release(envelope: dict) -> None:
    envelope["content_sha256"] = release_envelope_content_sha256(envelope)
    payload = dict(envelope)
    payload.pop("signature")
    key = Ed25519PrivateKey.from_private_bytes(b"r" * 32)
    envelope["signature"] = base64.b64encode(key.sign(canonical_release_json(payload))).decode(
        "ascii"
    )


def _resign_p0(profile: dict, keys: dict[str, Ed25519PrivateKey]) -> None:
    profile["profile_content_sha256"] = production_profile_content_sha256(profile)
    payload = dict(profile)
    payload.pop("signatures")
    profile["signatures"] = [
        {
            "role": role,
            "signature": base64.b64encode(keys[role].sign(canonical_json(payload))).decode("ascii"),
        }
        for role in ROLES
    ]


def _verify_fixture(tmp_path: Path):
    envelope, trust, root, p0, p0_trust, p0_inventory = _release_fixture(tmp_path)
    return verify_release_envelope(
        envelope,
        trust,
        root,
        p0,
        p0_trust,
        p0_inventory,
        now=NOW + timedelta(seconds=1),
    )


def test_valid_synthetic_fixture_is_deterministic_nonacceptance(tmp_path: Path) -> None:
    envelope, trust, root, p0, p0_trust, p0_inventory = _release_fixture(tmp_path)
    verified = verify_release_envelope(
        envelope, trust, root, p0, p0_trust, p0_inventory, now=NOW + timedelta(seconds=1)
    )
    report = verified.as_dict()
    assert verified.status == RELEASE_ENVELOPE_STATUS
    assert verified.acceptance_status == RELEASE_ENVELOPE_ACCEPTANCE_STATUS
    assert verified.gate6_pass is False
    assert verified.production_approval is False
    assert report["missing_external_p4_evidence"] == list(
        RELEASE_ENVELOPE_MISSING_EXTERNAL_EVIDENCE
    )
    assert verified.serialized_report() == verified.to_json()

    reordered = copy.deepcopy(envelope)
    reordered["bundle_inventory"].reverse()
    with pytest.raises(ReleaseEnvelopeError):
        verify_release_envelope(
            reordered, trust, root, p0, p0_trust, p0_inventory, now=NOW + timedelta(seconds=1)
        )


@pytest.mark.parametrize(
    "mutation, code",
    [
        (lambda e: e.update(envelope_id="other"), RELEASE_ENVELOPE_ERROR_INVALID_SIGNATURE),
        (
            lambda e: e["p0_profile"].update(profile_version="other"),
            RELEASE_ENVELOPE_ERROR_INVALID_SIGNATURE,
        ),
        (lambda e: e["release_owner"].update(key_id="other"), RELEASE_ENVELOPE_ERROR_INVALID_TRUST),
        (lambda e: e.update(content_sha256="f" * 64), RELEASE_ENVELOPE_ERROR_INVALID_SIGNATURE),
    ],
)
def test_content_and_signature_binding_tamper_fails(tmp_path: Path, mutation, code: str) -> None:
    envelope, trust, root, p0, p0_trust, p0_inventory = _release_fixture(tmp_path)
    mutation(envelope)
    with pytest.raises(ReleaseEnvelopeError, match=f"^{code}$"):
        verify_release_envelope(
            envelope, trust, root, p0, p0_trust, p0_inventory, now=NOW + timedelta(seconds=1)
        )


@pytest.mark.parametrize(
    "section, mutate",
    [
        ("code_commit", lambda e: e.update(code_commit="a" * 39)),
        ("platform_images", lambda e: e["platform_images"].update(extra="sha256:" + "1" * 64)),
        ("bundle_inventory", lambda e: e["bundle_inventory"][0].update(size=True)),
        ("sbom", lambda e: e["sbom"][0].update(extra=True)),
        ("features", lambda e: e["features"].update(extra=True)),
        ("startup_posture", lambda e: e.update(startup_posture=[])),
    ],
)
def test_exact_sections_and_types_fail_closed(tmp_path: Path, section: str, mutate) -> None:
    envelope, trust, root, p0, p0_trust, p0_inventory = _release_fixture(tmp_path)
    mutate(envelope)
    with pytest.raises(ReleaseEnvelopeError):
        verify_release_envelope(
            envelope, trust, root, p0, p0_trust, p0_inventory, now=NOW + timedelta(seconds=1)
        )


@pytest.mark.parametrize(
    "name, change",
    [
        ("traversal", lambda e: e["bundle_inventory"][0].update(path="../ca.pem")),
        ("absolute", lambda e: e["bundle_inventory"][0].update(path="/ca.pem")),
        ("dot_inventory", lambda e: e["bundle_inventory"][0].update(path=".")),
        ("dot_ca", lambda e: e["ca_bundle"].update(path=".")),
        (
            "duplicate",
            lambda e: e["bundle_inventory"].append(copy.deepcopy(e["bundle_inventory"][0])),
        ),
        ("missing", lambda e: e["bundle_inventory"].pop()),
        ("hash", lambda e: e["bundle_inventory"][0].update(sha256="f" * 64)),
        ("size", lambda e: e["bundle_inventory"][0].update(size=999)),
    ],
)
def test_closed_set_inventory_mutations_are_rejected(tmp_path: Path, name: str, change) -> None:
    envelope, trust, root, p0, p0_trust, p0_inventory = _release_fixture(tmp_path)
    change(envelope)
    if name == "duplicate":
        envelope["bundle_inventory"].sort(key=lambda entry: entry["path"])
    _resign_release(envelope)
    expected_code = (
        RELEASE_ENVELOPE_ERROR_INVALID_BUNDLE
        if name in {"missing", "hash", "size"}
        else RELEASE_ENVELOPE_ERROR_INVALID_BUNDLE
    )
    with pytest.raises(ReleaseEnvelopeError, match=f"^{expected_code}$"):
        verify_release_envelope(
            envelope, trust, root, p0, p0_trust, p0_inventory, now=NOW + timedelta(seconds=1)
        )


class _SecondReadTimestampMapping(Mapping[str, object]):
    """Return a signed alternate validity window only on a repeated lookup."""

    def __init__(self, first: dict, second: dict) -> None:
        self._first = first
        self._second = second
        self._reads = {"generated_at": 0, "valid_from": 0, "valid_until": 0}

    def __getitem__(self, key: str) -> object:
        if key in self._reads:
            self._reads[key] += 1
            if self._reads[key] == 1:
                return self._first[key]
        return self._second[key]

    def __iter__(self):
        return iter(self._second)

    def __len__(self) -> int:
        return len(self._second)

    def items(self):
        return self._second.items()


def test_non_idempotent_timestamp_mapping_is_rejected(tmp_path: Path) -> None:
    envelope, trust, root, p0, p0_trust, p0_inventory = _release_fixture(tmp_path)
    alternate = copy.deepcopy(envelope)
    alternate["valid_until"] = (NOW + timedelta(days=517)).isoformat()
    _resign_release(alternate)

    with pytest.raises(ReleaseEnvelopeError, match=f"^{RELEASE_ENVELOPE_ERROR_INVALID_SIGNATURE}$"):
        verify_release_envelope(
            _SecondReadTimestampMapping(envelope, alternate),
            trust,
            root,
            p0,
            p0_trust,
            p0_inventory,
            now=NOW + timedelta(seconds=1),
        )


def test_bundle_inventory_over_4096_entries_is_resource_limited(tmp_path: Path) -> None:
    envelope, trust, root, p0, p0_trust, p0_inventory = _release_fixture(tmp_path)
    assert release_module.RELEASE_ENVELOPE_MAX_BUNDLE_FILES == 4_096
    envelope["bundle_inventory"] = [
        {"path": f"bundle-{index:04d}", "sha256": "a" * 64, "size": 0}
        for index in range(release_module.RELEASE_ENVELOPE_MAX_BUNDLE_FILES + 1)
    ]

    with pytest.raises(ReleaseEnvelopeError, match=f"^{RELEASE_ENVELOPE_ERROR_RESOURCE_LIMIT}$"):
        verify_release_envelope(
            envelope, trust, root, p0, p0_trust, p0_inventory, now=NOW + timedelta(seconds=1)
        )


def _resignable_p0_fixture(tmp_path: Path) -> tuple[dict, dict, Path, dict, dict, dict, dict]:
    envelope, trust, root, _p0, _p0_trust, _p0_inventory = _release_fixture(tmp_path)
    p0, p0_trust, p0_inventory, p0_keys = _p0_fixture()
    return envelope, trust, root, p0, p0_trust, p0_inventory, p0_keys


def _assert_p0_binding_rejected(
    envelope: dict,
    trust: dict,
    root: Path,
    p0: dict,
    p0_trust: dict,
    p0_inventory: dict,
) -> None:
    with pytest.raises(ReleaseEnvelopeError, match=f"^{RELEASE_ENVELOPE_ERROR_INVALID_P0}$"):
        verify_release_envelope(
            envelope, trust, root, p0, p0_trust, p0_inventory, now=NOW + timedelta(seconds=1)
        )


def test_p0_profile_content_sha256_binding_is_independent(tmp_path: Path) -> None:
    envelope, trust, root, p0, p0_trust, p0_inventory, p0_keys = _resignable_p0_fixture(tmp_path)
    p0["p5_sample_n"] = 101
    _resign_p0(p0, p0_keys)
    _resign_release(envelope)

    _assert_p0_binding_rejected(envelope, trust, root, p0, p0_trust, p0_inventory)


def test_p0_profile_id_binding_is_independent_without_detail_leak(tmp_path: Path) -> None:
    envelope, trust, root, p0, p0_trust, p0_inventory = _release_fixture(tmp_path)
    attacker_profile_id = "production-acceptance.attacker"
    envelope["p0_profile"]["profile_id"] = attacker_profile_id
    _resign_release(envelope)

    with pytest.raises(
        ReleaseEnvelopeError, match=f"^{RELEASE_ENVELOPE_ERROR_INVALID_P0}$"
    ) as error:
        verify_release_envelope(
            envelope, trust, root, p0, p0_trust, p0_inventory, now=NOW + timedelta(seconds=1)
        )
    assert attacker_profile_id not in str(error.value)
    assert error.value.__cause__ is None


def test_p0_topology_id_binding_is_independent(tmp_path: Path) -> None:
    envelope, trust, root, p0, p0_trust, p0_inventory = _release_fixture(tmp_path)
    envelope["topology"]["topology_id"] = "topology.attacker"
    _resign_release(envelope)

    _assert_p0_binding_rejected(envelope, trust, root, p0, p0_trust, p0_inventory)


def test_p0_topology_binding_is_independent(tmp_path: Path) -> None:
    envelope, trust, root, p0, p0_trust, p0_inventory = _release_fixture(tmp_path)
    envelope["topology"]["sha256"] = "f" * 64
    _resign_release(envelope)

    _assert_p0_binding_rejected(envelope, trust, root, p0, p0_trust, p0_inventory)


def test_p0_features_binding_is_independent(tmp_path: Path) -> None:
    envelope, trust, root, p0, p0_trust, p0_inventory = _release_fixture(tmp_path)
    envelope["features"]["enabled"] = ["chat"]
    _resign_release(envelope)

    _assert_p0_binding_rejected(envelope, trust, root, p0, p0_trust, p0_inventory)


def test_p0_profile_version_binding_is_independent(tmp_path: Path) -> None:
    envelope, trust, root, p0, p0_trust, p0_inventory, p0_keys = _resignable_p0_fixture(tmp_path)
    p0["profile_version"] = "2026-07-16.2"
    _resign_p0(p0, p0_keys)
    envelope["p0_profile"]["profile_content_sha256"] = p0["profile_content_sha256"]
    _resign_release(envelope)

    _assert_p0_binding_rejected(envelope, trust, root, p0, p0_trust, p0_inventory)


def test_p0_inventory_version_binding_is_independent(tmp_path: Path) -> None:
    envelope, trust, root, p0, p0_trust, p0_inventory, p0_keys = _resignable_p0_fixture(tmp_path)
    p0_inventory["version"] = "inventory-2026-07-16.2"
    p0["enabled_inference_callsite_inventory"]["version"] = p0_inventory["version"]
    p0["enabled_inference_callsite_inventory"]["sha256"] = hashlib.sha256(
        canonical_release_json(p0_inventory)
    ).hexdigest()
    _resign_p0(p0, p0_keys)
    envelope["p0_profile"]["profile_content_sha256"] = p0["profile_content_sha256"]
    envelope["p0_profile"]["inventory_sha256"] = p0["enabled_inference_callsite_inventory"][
        "sha256"
    ]
    _resign_release(envelope)

    _assert_p0_binding_rejected(envelope, trust, root, p0, p0_trust, p0_inventory)


def test_p0_inventory_sha256_binding_is_independent(tmp_path: Path) -> None:
    envelope, trust, root, p0, p0_trust, p0_inventory, p0_keys = _resignable_p0_fixture(tmp_path)
    p0_inventory["callsite_ids"].append("router.secondary")
    p0_inventory["callsite_ids"].sort()
    p0["enabled_inference_callsite_inventory"]["sha256"] = hashlib.sha256(
        canonical_release_json(p0_inventory)
    ).hexdigest()
    _resign_p0(p0, p0_keys)
    envelope["p0_profile"]["profile_content_sha256"] = p0["profile_content_sha256"]
    _resign_release(envelope)

    _assert_p0_binding_rejected(envelope, trust, root, p0, p0_trust, p0_inventory)


def test_report_pins_nonacceptance_literals_on_every_surface(tmp_path: Path) -> None:
    with _verify_fixture(tmp_path) as verified:
        as_dict_report = verified.as_dict()
        serialized_report = json.loads(verified.serialized_report())
        to_json_report = json.loads(verified.to_json())

    expected_missing_external_p4_evidence = [
        "real release-owner signature/production trust",
        "SBOM production generation",
        "clean-host air-gap deploy log",
        "runtime-envelope readback match",
    ]
    assert as_dict_report["gate6_pass"] is False
    assert as_dict_report["production_approval"] is False
    assert as_dict_report["status"] == "VERIFIED_NON_ACCEPTANCE"
    assert as_dict_report["acceptance_status"] == "NOT_ACCEPTANCE"
    assert as_dict_report["evidence_class"] == "non-acceptance-release-envelope"
    assert as_dict_report["missing_external_p4_evidence"] == expected_missing_external_p4_evidence
    for report in (serialized_report, to_json_report):
        assert report["gate6_pass"] is False
        assert report["production_approval"] is False
        assert report["status"] == "VERIFIED_NON_ACCEPTANCE"
        assert report["acceptance_status"] == "NOT_ACCEPTANCE"
        assert report["evidence_class"] == "non-acceptance-release-envelope"
        assert report["missing_external_p4_evidence"] == expected_missing_external_p4_evidence


def test_verified_release_envelope_is_frozen_with_read_only_result_flags(tmp_path: Path) -> None:
    with _verify_fixture(tmp_path) as verified:
        with pytest.raises(FrozenInstanceError):
            verified.content_sha256 = "f" * 64
        with pytest.raises((TypeError, AttributeError)):
            verified.gate6_pass = True
        with pytest.raises((TypeError, AttributeError)):
            verified.production_approval = True
        with pytest.raises(AttributeError):
            type(verified).gate6_pass.__set__(verified, True)
        with pytest.raises(AttributeError):
            type(verified).production_approval.__set__(verified, True)


def test_bundle_extra_symlink_hardlink_and_special_are_rejected(tmp_path: Path) -> None:
    envelope, trust, root, p0, p0_trust, p0_inventory = _release_fixture(tmp_path)
    (root / "extra").write_bytes(b"extra")
    with pytest.raises(ReleaseEnvelopeError):
        verify_release_envelope(
            envelope, trust, root, p0, p0_trust, p0_inventory, now=NOW + timedelta(seconds=1)
        )

    (root / "extra").unlink()
    os.symlink(root / "ca.pem", root / "link")
    envelope["bundle_inventory"].append({"path": "link", "sha256": "0" * 64, "size": 1})
    envelope["bundle_inventory"].sort(key=lambda entry: entry["path"])
    _resign_release(envelope)
    with pytest.raises(ReleaseEnvelopeError, match=f"^{RELEASE_ENVELOPE_ERROR_INVALID_BUNDLE}$"):
        verify_release_envelope(
            envelope, trust, root, p0, p0_trust, p0_inventory, now=NOW + timedelta(seconds=1)
        )

    os.unlink(root / "link")
    os.link(root / "ca.pem", root / "hardlink")
    envelope["bundle_inventory"] = [
        entry for entry in envelope["bundle_inventory"] if entry["path"] != "link"
    ]
    envelope["bundle_inventory"].append(
        {"path": "hardlink", "sha256": hashlib.sha256(b"synthetic-ca").hexdigest(), "size": 12}
    )
    envelope["bundle_inventory"].sort(key=lambda entry: entry["path"])
    _resign_release(envelope)
    with pytest.raises(ReleaseEnvelopeError, match=f"^{RELEASE_ENVELOPE_ERROR_INVALID_BUNDLE}$"):
        verify_release_envelope(
            envelope, trust, root, p0, p0_trust, p0_inventory, now=NOW + timedelta(seconds=1)
        )

    os.unlink(root / "hardlink")
    fifo = root / "fifo"
    os.mkfifo(fifo)
    envelope["bundle_inventory"] = [
        entry for entry in envelope["bundle_inventory"] if entry["path"] != "hardlink"
    ]
    envelope["bundle_inventory"].append({"path": "fifo", "sha256": "0" * 64, "size": 0})
    envelope["bundle_inventory"].sort(key=lambda entry: entry["path"])
    _resign_release(envelope)
    with pytest.raises(ReleaseEnvelopeError, match=f"^{RELEASE_ENVELOPE_ERROR_INVALID_BUNDLE}$"):
        verify_release_envelope(
            envelope, trust, root, p0, p0_trust, p0_inventory, now=NOW + timedelta(seconds=1)
        )


def test_verified_result_owns_private_snapshot_and_cleanup(tmp_path: Path) -> None:
    envelope, trust, root, p0, p0_trust, p0_inventory = _release_fixture(tmp_path)
    verified = verify_release_envelope(
        envelope, trust, root, p0, p0_trust, p0_inventory, now=NOW + timedelta(seconds=1)
    )
    snapshot = verified.bundle_snapshot_root
    assert snapshot.name.startswith("anila-release-envelope-")
    assert (snapshot / "ca.pem").read_bytes() == b"synthetic-ca"
    (root / "ca.pem").write_bytes(b"rewritten-after-verification")
    assert (snapshot / "ca.pem").read_bytes() == b"synthetic-ca"
    verified.cleanup_snapshot()
    assert not snapshot.exists()


def test_bundle_descriptor_walk_errors_are_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    envelope, trust, root, p0, p0_trust, p0_inventory = _release_fixture(tmp_path)

    original_listdir = release_module.os.listdir
    calls = 0

    def denied_listdir(target):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise PermissionError("hidden-subtree")
        return original_listdir(target)

    monkeypatch.setattr(release_module.os, "listdir", denied_listdir)
    with pytest.raises(ReleaseEnvelopeError, match=f"^{RELEASE_ENVELOPE_ERROR_INVALID_BUNDLE}$"):
        verify_release_envelope(
            envelope, trust, root, p0, p0_trust, p0_inventory, now=NOW + timedelta(seconds=1)
        )


def test_post_eof_same_inode_rewrite_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    envelope, trust, root, p0, p0_trust, p0_inventory = _release_fixture(tmp_path)
    original_read = release_module.os.read
    rewritten = False

    target = root / "ca.pem"
    original_metadata = target.stat()

    def read_then_rewrite(fd: int, amount: int) -> bytes:
        nonlocal rewritten
        chunk = original_read(fd, amount)
        if chunk and not rewritten and os.fstat(fd).st_ino == target.stat().st_ino:
            rewritten = True
            # Keep the length and restore mtime.  The post-EOF identity check
            # must still detect the same-inode mutation via ctime.
            with target.open("r+b") as handle:
                handle.write(b"changed-ca!!")
            os.utime(target, ns=(original_metadata.st_atime_ns, original_metadata.st_mtime_ns))
        return chunk

    monkeypatch.setattr(release_module.os, "read", read_then_rewrite)
    with pytest.raises(ReleaseEnvelopeError, match=f"^{RELEASE_ENVELOPE_ERROR_INVALID_BUNDLE}$"):
        verify_release_envelope(
            envelope, trust, root, p0, p0_trust, p0_inventory, now=NOW + timedelta(seconds=1)
        )


def test_wrong_release_key_id_and_p0_binding_fail_without_detail(tmp_path: Path) -> None:
    envelope, trust, root, p0, p0_trust, p0_inventory = _release_fixture(tmp_path)
    trust["trusted_release_owners"][0]["key_id"] = "other-key"
    with pytest.raises(
        ReleaseEnvelopeError, match=f"^{RELEASE_ENVELOPE_ERROR_INVALID_TRUST}$"
    ) as error:
        verify_release_envelope(
            envelope, trust, root, p0, p0_trust, p0_inventory, now=NOW + timedelta(seconds=1)
        )
    assert "other-key" not in str(error.value)
    assert error.value.__cause__ is None

    nested = tmp_path / "p0"
    nested.mkdir()
    envelope, trust, root, _, _, _ = _release_fixture(nested)
    p0, p0_trust, p0_inventory, p0_keys = _p0_fixture()
    p0["data_classification_ceiling"] = "機密"
    _resign_p0(p0, p0_keys)
    envelope["p0_profile"]["profile_content_sha256"] = p0["profile_content_sha256"]
    _resign_release(envelope)
    with pytest.raises(ReleaseEnvelopeError, match=f"^{RELEASE_ENVELOPE_ERROR_INVALID_P0}$"):
        verify_release_envelope(
            envelope, trust, root, p0, p0_trust, p0_inventory, now=NOW + timedelta(seconds=1)
        )


def test_validity_as_of_and_disabled_template_rejected(tmp_path: Path) -> None:
    envelope, trust, root, p0, p0_trust, p0_inventory = _release_fixture(tmp_path)
    with pytest.raises(ReleaseEnvelopeError, match=f"^{RELEASE_ENVELOPE_ERROR_NOT_EFFECTIVE}$"):
        verify_release_envelope(
            envelope, trust, root, p0, p0_trust, p0_inventory, now=NOW + timedelta(days=11)
        )

    p0["enabled"] = False
    p0["template_only"] = True
    p0["approval_status"] = "disabled_template"
    with pytest.raises(ReleaseEnvelopeError, match=f"^{RELEASE_ENVELOPE_ERROR_INVALID_P0}$"):
        verify_release_envelope(
            envelope, trust, root, p0, p0_trust, p0_inventory, now=NOW + timedelta(seconds=1)
        )


def test_strict_json_duplicate_nan_depth_and_redaction(tmp_path: Path) -> None:
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"x":1,"x":2}', encoding="ascii")
    with pytest.raises(
        ReleaseEnvelopeError, match=f"^{RELEASE_ENVELOPE_ERROR_DUPLICATE_JSON_KEY}$"
    ):
        read_release_envelope(duplicate)

    nan = tmp_path / "nan.json"
    nan.write_text('{"x":NaN}', encoding="ascii")
    with pytest.raises(ReleaseEnvelopeError, match=f"^{RELEASE_ENVELOPE_ERROR_INVALID_JSON}$"):
        read_release_envelope(nan)

    deep = tmp_path / "deep.json"
    depth = RELEASE_ENVELOPE_MAX_JSON_DEPTH + 1
    deep.write_text("{" + '"x":[' * depth + "0" + "]" * depth + "}", encoding="ascii")
    with pytest.raises(ReleaseEnvelopeError, match=f"^{RELEASE_ENVELOPE_ERROR_RESOURCE_LIMIT}$"):
        read_release_envelope(deep)

    marker = "SECRET-CONTENT-MARKER"
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"secret": marker, "extra": True}), encoding="utf-8")
    with pytest.raises(ReleaseEnvelopeError) as error:
        verify_release_envelope(
            json.loads(bad.read_text(encoding="utf-8")), {}, tmp_path, {}, {}, {}
        )
    assert marker not in str(error.value)
    assert error.value.__cause__ is None

    fifo = tmp_path / "json-fifo"
    os.mkfifo(fifo)
    with pytest.raises(ReleaseEnvelopeError, match=f"^{RELEASE_ENVELOPE_ERROR_INVALID_JSON}$"):
        read_release_envelope(fifo)


def test_signed_file_entrypoint_uses_external_paths(tmp_path: Path) -> None:
    envelope, release_trust, root, p0, p0_trust, p0_inventory = _release_fixture(tmp_path)
    assert (
        release_module.verify_release_envelope_file is release_module.verify_signed_release_envelope
    )
    envelope_path = tmp_path / "attestation.json"
    trust_path = tmp_path / "release-trust.json"
    p0_path = tmp_path / "p0.json"
    p0_trust_path = tmp_path / "p0-trust.json"
    inventory_path = tmp_path / "p0-inventory.json"
    envelope_path.write_bytes(canonical_release_json(envelope))
    trust_path.write_bytes(canonical_release_json(release_trust))
    p0_path.write_bytes(canonical_release_json(p0))
    p0_trust_path.write_bytes(canonical_release_json(p0_trust))
    inventory_path.write_bytes(canonical_release_json(p0_inventory))
    with verify_signed_release_envelope(
        envelope_path,
        trust_path,
        root,
        p0_path,
        p0_trust_path,
        inventory_path,
        now=NOW + timedelta(seconds=1),
    ) as direct:
        with release_module.verify_release_envelope_file(
            envelope_path,
            trust_path,
            root,
            p0_path,
            p0_trust_path,
            inventory_path,
            now=NOW + timedelta(seconds=1),
        ) as via_alias:
            assert direct.acceptance_status == "NOT_ACCEPTANCE"
            assert via_alias.acceptance_status == "NOT_ACCEPTANCE"
            assert via_alias.content_sha256 == direct.content_sha256

    real_profile = tmp_path / "p0-real.json"
    p0_path.unlink()
    real_profile.write_bytes(canonical_release_json(p0))
    os.symlink(real_profile, p0_path)
    with pytest.raises(ReleaseEnvelopeError, match=f"^{RELEASE_ENVELOPE_ERROR_INVALID_P0}$"):
        verify_signed_release_envelope(
            envelope_path,
            trust_path,
            root,
            p0_path,
            p0_trust_path,
            inventory_path,
            now=NOW + timedelta(seconds=1),
        )

    p0_path.unlink()
    p0_path.write_bytes(b"{" + b"x" * (release_module.RELEASE_ENVELOPE_MAX_BYTES + 1))
    with pytest.raises(ReleaseEnvelopeError, match=f"^{RELEASE_ENVELOPE_ERROR_RESOURCE_LIMIT}$"):
        verify_signed_release_envelope(
            envelope_path,
            trust_path,
            root,
            p0_path,
            p0_trust_path,
            inventory_path,
            now=NOW + timedelta(seconds=1),
        )


def test_signed_p0_authority_substitution_is_rejected(tmp_path: Path) -> None:
    envelope, release_trust, root, _p0, _p0_trust, _p0_inventory = _release_fixture(tmp_path)
    p0, p0_trust, inventory, p0_keys = _p0_fixture()
    p0["profile_id"] = "production-acceptance.attacker"
    _resign_p0(p0, p0_keys)
    envelope_path = tmp_path / "attestation.json"
    release_trust_path = tmp_path / "release-trust.json"
    p0_path = tmp_path / "attacker-p0.json"
    p0_trust_path = tmp_path / "attacker-p0-trust.json"
    inventory_path = tmp_path / "attacker-inventory.json"
    envelope_path.write_bytes(canonical_release_json(envelope))
    release_trust_path.write_bytes(canonical_release_json(release_trust))
    p0_path.write_bytes(canonical_release_json(p0))
    p0_trust_path.write_bytes(canonical_release_json(p0_trust))
    inventory_path.write_bytes(canonical_release_json(inventory))
    with pytest.raises(ReleaseEnvelopeError, match=f"^{RELEASE_ENVELOPE_ERROR_INVALID_P0}$"):
        verify_signed_release_envelope(
            envelope_path,
            release_trust_path,
            root,
            p0_path,
            p0_trust_path,
            inventory_path,
            now=NOW + timedelta(seconds=1),
        )


def _add_bundle_file(envelope: dict, root: Path, relative: str, content: bytes) -> None:
    target = root.joinpath(*relative.split("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    envelope["bundle_inventory"].append(
        {
            "path": relative,
            "sha256": hashlib.sha256(content).hexdigest(),
            "size": len(content),
        }
    )
    envelope["bundle_inventory"].sort(key=lambda entry: entry["path"])
    _resign_release(envelope)


def test_authority_parent_symlink_is_rejected(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (real / "authority.json").write_text('{"safe":true}', encoding="utf-8")
    linked = tmp_path / "linked"
    os.symlink(real, linked)

    with pytest.raises(ReleaseEnvelopeError, match=f"^{RELEASE_ENVELOPE_ERROR_INVALID_JSON}$"):
        read_release_envelope(linked / "authority.json")


def test_snapshot_context_close_finalizer_and_replacement_are_safe(tmp_path: Path) -> None:
    verified = _verify_fixture(tmp_path)
    snapshot = verified.snapshot_root
    with verified as lease:
        assert lease.snapshot_root == snapshot
        assert (snapshot / "ca.pem").read_bytes() == b"synthetic-ca"
    assert not snapshot.exists()
    with pytest.raises(ReleaseEnvelopeError, match=f"^{RELEASE_ENVELOPE_ERROR_INVALID_BUNDLE}$"):
        _ = verified.snapshot_root
    # Closing twice must not erase a later directory reusing the old spelling.
    snapshot.mkdir()
    verified.close()
    assert snapshot.is_dir()
    snapshot.rmdir()

    def abandoned_snapshot() -> Path:
        fresh = tmp_path / "forgotten"
        fresh.mkdir()
        result = _verify_fixture(fresh)
        return result.snapshot_root

    forgotten = abandoned_snapshot()
    gc.collect()
    assert not forgotten.exists()


def test_snapshot_replacement_before_close_is_never_deleted(tmp_path: Path) -> None:
    verified = _verify_fixture(tmp_path)
    snapshot = verified.snapshot_root
    original = tmp_path / "moved-original"
    os.rename(snapshot, original)
    snapshot.mkdir()
    with pytest.raises(ReleaseEnvelopeError, match=f"^{RELEASE_ENVELOPE_ERROR_INVALID_BUNDLE}$"):
        verified.close()
    assert snapshot.is_dir()
    assert (original / "ca.pem").is_file()
    snapshot.rmdir()
    shutil.rmtree(original)


def test_snapshot_nested_modes_and_injected_entry_are_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    envelope, trust, root, p0, p0_trust, p0_inventory = _release_fixture(tmp_path)
    _add_bundle_file(envelope, root, "nested/payload.bin", b"nested-payload")
    old_umask = os.umask(0o777)
    try:
        verified = verify_release_envelope(
            envelope, trust, root, p0, p0_trust, p0_inventory, now=NOW + timedelta(seconds=1)
        )
    finally:
        os.umask(old_umask)
    snapshot = verified.snapshot_root
    assert stat.S_IMODE(snapshot.stat().st_mode) == 0o700
    assert stat.S_IMODE((snapshot / "nested").stat().st_mode) == 0o700
    assert stat.S_IMODE((snapshot / "nested" / "payload.bin").stat().st_mode) == 0o600
    snapshot_fd = release_module._open_directory_path(snapshot)
    try:
        with pytest.raises(release_module._InvalidBundle):
            release_module._discover_bundle(snapshot_fd, snapshot_owner_uid=os.geteuid() + 1)
    finally:
        os.close(snapshot_fd)
    verified.close()

    injected = False
    original_write = release_module._write_snapshot_file

    def write_then_inject(snapshot_fd: int, *args, **kwargs) -> None:
        nonlocal injected
        original_write(snapshot_fd, *args, **kwargs)
        if not injected:
            injected = True
            fd = os.open(
                "injected", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=snapshot_fd
            )
            os.close(fd)

    monkeypatch.setattr(release_module, "_write_snapshot_file", write_then_inject)
    with pytest.raises(ReleaseEnvelopeError, match=f"^{RELEASE_ENVELOPE_ERROR_INVALID_BUNDLE}$"):
        verify_release_envelope(
            envelope, trust, root, p0, p0_trust, p0_inventory, now=NOW + timedelta(seconds=1)
        )


def test_snapshot_same_size_content_injection_is_rehashed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    envelope, trust, root, p0, p0_trust, p0_inventory = _release_fixture(tmp_path)
    original_write = release_module._write_snapshot_file
    changed = False

    def write_then_rewrite(snapshot_fd: int, relative: str, *args, **kwargs) -> None:
        nonlocal changed
        original_write(snapshot_fd, relative, *args, **kwargs)
        if relative == "ca.pem" and not changed:
            changed = True
            fd = os.open("ca.pem", os.O_WRONLY, dir_fd=snapshot_fd)
            try:
                os.write(fd, b"changed-ca!!")
            finally:
                os.close(fd)

    monkeypatch.setattr(release_module, "_write_snapshot_file", write_then_rewrite)
    with pytest.raises(ReleaseEnvelopeError, match=f"^{RELEASE_ENVELOPE_ERROR_INVALID_BUNDLE}$"):
        verify_release_envelope(
            envelope, trust, root, p0, p0_trust, p0_inventory, now=NOW + timedelta(seconds=1)
        )


def test_bundle_substitution_hardlinks_and_copy_exception_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first"
    first.mkdir()
    envelope, trust, root, p0, p0_trust, p0_inventory = _release_fixture(first)
    original_prepare = release_module._prepare_snapshot_directories

    def replace_after_discovery(*args, **kwargs) -> None:
        original_prepare(*args, **kwargs)
        target = root / "ca.pem"
        content = target.read_bytes()
        target.unlink()
        target.write_bytes(content)

    monkeypatch.setattr(release_module, "_prepare_snapshot_directories", replace_after_discovery)
    with pytest.raises(ReleaseEnvelopeError, match=f"^{RELEASE_ENVELOPE_ERROR_INVALID_BUNDLE}$"):
        verify_release_envelope(
            envelope, trust, root, p0, p0_trust, p0_inventory, now=NOW + timedelta(seconds=1)
        )

    monkeypatch.undo()
    second = tmp_path / "second"
    second.mkdir()
    envelope, trust, root, p0, p0_trust, p0_inventory = _release_fixture(second)
    os.link(root / "ca.pem", root / "ca-copy.pem")
    _add_bundle_file(envelope, root, "ca-copy.pem", b"synthetic-ca")
    with pytest.raises(ReleaseEnvelopeError, match=f"^{RELEASE_ENVELOPE_ERROR_INVALID_BUNDLE}$"):
        verify_release_envelope(
            envelope, trust, root, p0, p0_trust, p0_inventory, now=NOW + timedelta(seconds=1)
        )

    third = tmp_path / "third"
    third.mkdir()
    envelope, trust, root, p0, p0_trust, p0_inventory = _release_fixture(third)
    snapshot_parent = third / "snapshot-parent"
    snapshot_parent.mkdir()
    token = "exception-cleanup"
    expected_snapshot = snapshot_parent / f"anila-release-envelope-{token}"

    def interrupted_copy(*_args, **_kwargs) -> None:
        raise OSError("synthetic copy interruption")

    monkeypatch.setattr(release_module.tempfile, "gettempdir", lambda: str(snapshot_parent))
    monkeypatch.setattr(release_module.secrets, "token_hex", lambda _bytes: token)
    monkeypatch.setattr(release_module, "_write_snapshot_file", interrupted_copy)
    with pytest.raises(ReleaseEnvelopeError, match=f"^{RELEASE_ENVELOPE_ERROR_INVALID_BUNDLE}$"):
        verify_release_envelope(
            envelope, trust, root, p0, p0_trust, p0_inventory, now=NOW + timedelta(seconds=1)
        )
    assert not expected_snapshot.exists()
