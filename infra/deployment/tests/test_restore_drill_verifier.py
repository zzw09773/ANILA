from __future__ import annotations

import base64
import copy
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "infra" / "deployment" / "backup"))

from anila_security.production_acceptance_profile import (  # noqa: E402
    PRODUCTION_ACCEPTANCE_SCHEMA,
    canonical_json,
    production_profile_content_sha256,
    verify_production_acceptance_profile,
)
from restore_drill_verifier import (  # noqa: E402
    RESTORE_EVIDENCE_STATUS,
    RestoreEvidenceError,
    verify_restore_evidence,
)


NOW = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
ROLES = ("system_owner", "data_owner", "pki_owner", "security", "operations")


def _authority():
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
            "artifact_hashes": {"release": "a" * 64},
        },
        "enabled_features": ["chat"],
        "disabled_features": ["studio"],
        "data_classification_ceiling": "營業秘密",
        "rto_rpo": {"rto_seconds": 3600, "rpo_seconds": 900},
        "slo_thresholds": {
            "ingestion_p99_ms": 10000,
            "dispatch_success_rate": 0.99,
            "queue_age_seconds": 300,
            "stuck_job_count": 0,
            "artifact_download_success_rate": 0.99,
            "auth_error_rate": 0.01,
        },
        "load_profile": {
            "profile_id": "load.synthetic",
            "concurrency": 1,
            "requests_per_second": 1.0,
            "duration_seconds": 3600,
            "workflow_ids": ["chat-basic"],
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
            }
        ],
        "p5_sample_n": 1,
        "enabled_inference_callsite_inventory": {
            "schema_version": "anila.gate5.inference-inventory.v1",
            "version": "inventory-2026-07-16.1",
            "sha256": "c" * 64,
            "callsite_ids": ["router.primary"],
        },
        "revocation_sla": {"token_seconds": 60, "card_seconds": 300},
        "pki_policy": {
            "stale_after_seconds": 3600,
            "offline_behavior": "fail_closed",
            "missing_behavior": "fail_closed",
            "refresh_failure_behavior": "fail_closed",
        },
        "severity_taxonomy": {
            "sev1": {"definition": ["auth bypass"], "ack_seconds": 300, "mitigate_seconds": 3600},
            "sev2": {"definition": ["degradation"], "ack_seconds": 1800, "mitigate_seconds": 86400},
        },
        "finding_acceptance_rule": {
            "critical": "reject",
            "high": "reject",
            "medium": "conditional_accept",
            "low": "accept",
            "blocked_categories": ["auth", "classification", "egress"],
            "conditional_requirements": ["owner", "expiry", "compensating_control", "independent_signoff"],
        },
        "revalidation_impact_matrix": {"version": "impact.synthetic", "sha256": "d" * 64},
        "valid_from": (NOW - timedelta(days=9)).isoformat(),
        "valid_until": (NOW + timedelta(days=30)).isoformat(),
        "signer_roles": list(ROLES),
    }
    profile["profile_content_sha256"] = production_profile_content_sha256(profile)
    keys = {role: Ed25519PrivateKey.generate() for role in ROLES}
    payload = dict(profile)
    profile["signatures"] = [
        {"role": role, "signature": base64.b64encode(key.sign(canonical_json(payload))).decode("ascii")}
        for role, key in keys.items()
    ]
    trust = {
        "trusted_signers": {
            role: key.public_key().public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            ).decode("ascii")
            for role, key in keys.items()
        }
    }
    return verify_production_acceptance_profile(profile, trust, now=NOW)


def _dataset(seed: str) -> dict:
    digest = (seed * 64)[:64]
    return {
        "row_counts": {"expected": {"total": 2}, "actual": {"total": 2}},
        "checksums": {"expected": {"payload": digest}, "actual": {"payload": digest}},
        "referential_integrity": {"checked": True, "violations": 0},
    }


def _report() -> dict:
    profile = _authority()
    started = NOW - timedelta(hours=1)
    completed = NOW - timedelta(minutes=30)
    return {
        "schema_version": "anila.gate6.p1.restore-evidence.v1",
        "evidence_id": "restore.synthetic.1",
        "status": "pass",
        "generated_at": NOW.isoformat(),
        "environment": "synthetic",
        "synthetic": True,
        "profile_binding": {
            "profile_id": profile.profile_id,
            "profile_version": profile.profile_version,
            "profile_content_sha256": profile.profile_content_sha256,
        },
        "restore": {
            "backup_id": "backup-20260716T100000Z-deadbeef",
            "backup_created_at": (NOW - timedelta(hours=2)).isoformat(),
            "restore_started_at": started.isoformat(),
            "restore_completed_at": completed.isoformat(),
            "restore_seconds": 1800,
            "rpo_seconds_observed": 120,
        },
        "datasets": {name: _dataset(letter) for name, letter in {
            "db": "a", "blob": "b", "artifact": "c", "vector_generation": "d"
        }.items()},
        "negative_controls": {
            "rls": [{"id": "rls-cross-collection-read", "attempted": True, "rejected": True, "observed_status": "denied"}],
            "compartment": [{"id": "cross-compartment-read", "attempted": True, "rejected": True, "observed_status": "forbidden"}],
            "revocation": [{"id": "revoked-token-read", "attempted": True, "rejected": True, "observed_status": "revoked"}],
        },
    }


class RestoreDrillVerifierTests(unittest.TestCase):
    def test_complete_restore_report_verifies_as_non_acceptance(self) -> None:
        authority = _authority()
        result = verify_restore_evidence(_report(), authority, now=NOW)

        self.assertEqual(result.status, RESTORE_EVIDENCE_STATUS)
        self.assertEqual(result.acceptance_status, "NOT_ACCEPTANCE")
        self.assertFalse(result.gate6_pass)
        self.assertFalse(result.as_dict()["production_approval"])
        self.assertEqual(result.as_dict()["environment"], "synthetic")

    def test_plain_mapping_cannot_claim_p0_verification(self) -> None:
        with self.assertRaisesRegex(RestoreEvidenceError, "VerifiedProductionAcceptanceProfile"):
            verify_restore_evidence(_report(), {"profile": "pretend"})  # type: ignore[arg-type]

    def test_mutations_fail_closed(self) -> None:
        mutations = (
            ("missing datasets", lambda r: r.pop("datasets")),
            ("rto mismatch", lambda r: r["restore"].update(restore_seconds=3601)),
            (
                "vector checksum mismatch",
                lambda r: r["datasets"]["vector_generation"]["checksums"]["actual"].update(
                    payload="e" * 64
                ),
            ),
            (
                "referential integrity violation",
                lambda r: r["datasets"]["db"]["referential_integrity"].update(violations=1),
            ),
            ("rls allowed", lambda r: r["negative_controls"]["rls"][0].update(rejected=False)),
            (
                "compartment allowed",
                lambda r: r["negative_controls"]["compartment"][0].update(
                    observed_status="allowed"
                ),
            ),
            (
                "revocation not attempted",
                lambda r: r["negative_controls"]["revocation"][0].update(attempted=False),
            ),
        )
        for name, mutation in mutations:
            with self.subTest(mutation=name):
                report = copy.deepcopy(_report())
                mutation(report)
                with self.assertRaises(RestoreEvidenceError):
                    verify_restore_evidence(report, _authority(), now=NOW)

    def test_rto_and_rpo_are_profile_thresholds(self) -> None:
        report = _report()
        report["restore"]["restore_seconds"] = 3601
        report["restore"]["restore_completed_at"] = (
            datetime.fromisoformat(report["restore"]["restore_started_at"])
            + timedelta(seconds=3601)
        ).isoformat()
        report["generated_at"] = (NOW + timedelta(seconds=1)).isoformat()
        with self.assertRaisesRegex(RestoreEvidenceError, "exceeds P0 RTO"):
            verify_restore_evidence(report, _authority(), now=NOW + timedelta(seconds=2))

    def test_duplicate_or_bom_report_is_rejected(self) -> None:
        from restore_drill_verifier import read_restore_report

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "report.json"
            path.write_text('{"status":"pass","status":"pass"}', encoding="utf-8")
            with self.assertRaisesRegex(RestoreEvidenceError, "duplicate JSON key"):
                read_restore_report(path)
            path.write_bytes(b"\xef\xbb\xbf{}")
            with self.assertRaisesRegex(RestoreEvidenceError, "BOM"):
                read_restore_report(path)
