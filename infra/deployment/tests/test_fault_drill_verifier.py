from __future__ import annotations

import base64
import copy
import json
import sys
import tempfile
import traceback
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


ROOT = Path(__file__).resolve().parents[3]
SECURITY_SRC = ROOT / "packages" / "anila-security" / "src"
FAULTDRILL_SRC = ROOT / "infra" / "deployment" / "faultdrill"
for source in (SECURITY_SRC, FAULTDRILL_SRC):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from anila_security.production_acceptance_profile import (  # noqa: E402
    PRODUCTION_ACCEPTANCE_SCHEMA,
    canonical_json,
    production_profile_content_sha256,
    verify_production_acceptance_profile,
)
from fault_drill_verifier import (  # noqa: E402
    FAULT_EVIDENCE_STATUS,
    FaultDrillEvidenceError,
    main,
    read_fault_drill_report,
    verify_fault_drill_evidence,
)
from reconcile_job_states import build_job_snapshot  # noqa: E402


NOW = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)
ROLES = ("system_owner", "data_owner", "pki_owner", "security", "operations")


def _signed_profile(
    *, rto_seconds: int = 3600, rpo_seconds: int = 900
) -> tuple[dict, dict, object]:
    profile = {
        "schema_version": PRODUCTION_ACCEPTANCE_SCHEMA,
        "profile_id": "production-acceptance.synthetic",
        "profile_version": "2026-07-17.1",
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
        "rto_rpo": {"rto_seconds": rto_seconds, "rpo_seconds": rpo_seconds},
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
            }
        ],
        "p5_sample_n": 1,
        "enabled_inference_callsite_inventory": {
            "schema_version": "anila.gate5.inference-inventory.v1",
            "version": "inventory-2026-07-17.1",
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
            "sev1": {
                "definition": ["auth bypass"],
                "ack_seconds": 300,
                "mitigate_seconds": 3600,
            },
            "sev2": {
                "definition": ["degradation"],
                "ack_seconds": 1800,
                "mitigate_seconds": 86400,
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
            "version": "impact.synthetic",
            "sha256": "d" * 64,
        },
        "valid_from": (NOW - timedelta(days=9)).isoformat(),
        "valid_until": (NOW + timedelta(days=30)).isoformat(),
        "signer_roles": list(ROLES),
    }
    profile["profile_content_sha256"] = production_profile_content_sha256(profile)
    keys = {role: Ed25519PrivateKey.generate() for role in ROLES}
    payload = dict(profile)
    profile["signatures"] = [
        {
            "role": role,
            "signature": base64.b64encode(key.sign(canonical_json(payload))).decode(
                "ascii"
            ),
        }
        for role, key in keys.items()
    ]
    trust = {
        "trusted_signers": {
            role: key.public_key()
            .public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            .decode("ascii")
            for role, key in keys.items()
        }
    }
    return profile, trust, verify_production_acceptance_profile(profile, trust, now=NOW)


def _row(job_id: int, status: str, attempts: int, dead_lettered_at: str | None = None):
    return {
        "job_id": job_id,
        "status": status,
        "attempt_count": attempts,
        "lease_token": None,
        "dead_lettered_at": dead_lettered_at,
    }


def _report(authority=None) -> dict:
    if authority is None:
        _, _, authority = _signed_profile()
    started = NOW - timedelta(minutes=10)
    recovered = NOW - timedelta(minutes=5)
    before = build_job_snapshot(
        [_row(1, "queued", 0), _row(2, "retry_wait", 1)],
        snapshot_id="fault.synthetic.before",
        captured_at=started - timedelta(seconds=1),
    )
    after = build_job_snapshot(
        [
            _row(1, "succeeded", 1),
            _row(2, "dead_letter", 3, (recovered + timedelta(seconds=1)).isoformat()),
        ],
        snapshot_id="fault.synthetic.after",
        captured_at=recovered + timedelta(seconds=1),
    )
    targets = ["redis", "worker", "csp", "disk-full", "network-partition"]
    return {
        "schema_version": "anila.gate6.p2.fault-evidence.v1",
        "evidence_id": "fault.synthetic.1",
        "status": "complete",
        "generated_at": NOW.isoformat(),
        "environment": "synthetic",
        "synthetic": True,
        "profile_binding": {
            "profile_id": authority.profile_id,
            "profile_version": authority.profile_version,
            "profile_content_sha256": authority.profile_content_sha256,
        },
        "drill": {
            "started_at": started.isoformat(),
            "recovered_at": recovered.isoformat(),
            "rto_seconds_observed": 300,
            "rpo_seconds_observed": 1,
            "accepted_job_id_watermark": 2,
            "fault_targets": targets,
        },
        "before_snapshot": before,
        "after_snapshot": after,
        "timeline": [
            event
            for index, target in enumerate(targets)
            for event in (
                {
                    "sequence": index * 2 + 1,
                    "observed_at": (
                        started + timedelta(seconds=index * 50)
                    ).isoformat(),
                    "event_type": "fault_injected",
                    "target": target,
                    "outcome": "injected",
                },
                {
                    "sequence": index * 2 + 2,
                    "observed_at": (
                        started + timedelta(seconds=index * 50 + 10)
                    ).isoformat(),
                    "event_type": "fault_recovered",
                    "target": target,
                    "outcome": "recovered",
                },
            )
        ],
        "negative_controls": {
            "classification": [
                {
                    "id": "classification-deny",
                    "attempted": True,
                    "bypass_observed": False,
                    "observed_status": "fail_closed",
                }
            ],
            "authorization": [
                {
                    "id": "authorization-deny",
                    "attempted": True,
                    "bypass_observed": False,
                    "observed_status": "forbidden",
                }
            ],
        },
    }


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )


def _verify(report: dict, profile: dict, trust: dict):
    with tempfile.TemporaryDirectory() as temp:
        directory = Path(temp)
        profile_path = directory / "profile.json"
        trust_path = directory / "trust.json"
        _write_json(profile_path, profile)
        _write_json(trust_path, trust)
        return verify_fault_drill_evidence(report, profile_path, trust_path, now=NOW)


class FaultDrillVerifierTests(unittest.TestCase):
    def _assert_report_rejected(
        self,
        callback,
        expected_error: str,
        *forbidden_markers: str,
    ) -> None:
        with self.assertRaises(FaultDrillEvidenceError) as captured:
            callback()
        self.assertEqual(str(captured.exception), expected_error)
        self.assertIsNone(captured.exception.__cause__)
        rendered = "".join(
            traceback.format_exception(
                type(captured.exception),
                captured.exception,
                captured.exception.__traceback__,
            )
        )
        self.assertNotIn(FAULT_EVIDENCE_STATUS, rendered)
        self.assertNotIn("NOT_ACCEPTANCE", rendered)
        for marker in forbidden_markers:
            self.assertNotIn(marker, rendered)

    def test_signed_profile_verifies_only_non_acceptance_evidence(self) -> None:
        profile, trust, authority = _signed_profile()
        result = _verify(_report(authority), profile, trust)

        self.assertEqual(result.status, FAULT_EVIDENCE_STATUS)
        self.assertEqual(result.acceptance_status, "NOT_ACCEPTANCE")
        self.assertFalse(result.gate6_pass)
        self.assertFalse(result.as_dict()["production_approval"])
        self.assertEqual(result.as_dict()["reconciliation"]["accepted_job_count"], 2)
        self.assertEqual(result.as_dict()["reconciliation"]["terminal_job_count"], 2)

    def test_profile_binding_must_exactly_match_valid_signed_authority(self) -> None:
        profile, trust, authority = _signed_profile()
        mismatches = (
            ("profile_id", "production-acceptance.secret-other"),
            ("profile_version", "secret-profile-version-marker"),
            ("profile_content_sha256", "e" * 64),
        )
        for field, mismatch in mismatches:
            with self.subTest(field=field):
                report = _report(authority)
                report["profile_binding"][field] = mismatch
                self._assert_report_rejected(
                    lambda report=report: _verify(report, profile, trust),
                    "fault-drill report is bound to a different P0 profile",
                    mismatch,
                )

    def test_negative_controls_are_complete_unique_and_fail_closed(self) -> None:
        profile, trust, authority = _signed_profile()

        def report_with_denied_controls() -> dict:
            report = _report(authority)
            for entries in report["negative_controls"].values():
                for entry in entries:
                    entry["observed_status"] = "denied"
            return report

        def remove_category(report: dict) -> None:
            report["negative_controls"].pop("classification")

        def add_category(report: dict) -> None:
            report["negative_controls"]["unexpected"] = []

        def empty_category(report: dict) -> None:
            report["negative_controls"]["classification"] = []

        def duplicate_id(report: dict) -> None:
            report["negative_controls"]["classification"].append(
                copy.deepcopy(report["negative_controls"]["classification"][0])
            )

        def not_attempted(report: dict) -> None:
            report["negative_controls"]["classification"][0]["attempted"] = False

        def bypass_observed(report: dict) -> None:
            report["negative_controls"]["authorization"][0]["bypass_observed"] = True

        def non_rejecting_status(report: dict) -> None:
            report["negative_controls"]["classification"][0]["observed_status"] = (
                "allowed"
            )

        def extra_item_field(report: dict) -> None:
            report["negative_controls"]["classification"][0]["unexpected"] = True

        mutations = (
            (
                "missing category",
                remove_category,
                "negative_controls has unknown or missing fields",
            ),
            (
                "extra category",
                add_category,
                "negative_controls has unknown or missing fields",
            ),
            (
                "empty category",
                empty_category,
                "negative_controls.classification must be non-empty",
            ),
            (
                "duplicate id",
                duplicate_id,
                "negative_controls.classification has duplicate id",
            ),
            (
                "not attempted",
                not_attempted,
                "negative_controls.classification.classification-deny did not prove fail-closed",
            ),
            (
                "bypass observed",
                bypass_observed,
                "negative_controls.authorization.authorization-deny did not prove fail-closed",
            ),
            (
                "non-rejecting status",
                non_rejecting_status,
                "negative_controls.classification.classification-deny has non-rejecting outcome",
            ),
            (
                "extra item field",
                extra_item_field,
                "negative_controls.classification[0] has unknown or missing fields",
            ),
        )
        baseline = _verify(report_with_denied_controls(), profile, trust)
        self.assertEqual(baseline.status, FAULT_EVIDENCE_STATUS)
        self.assertFalse(baseline.gate6_pass)
        for name, mutation, expected_error in mutations:
            with self.subTest(name=name):
                report = report_with_denied_controls()
                mutation(report)
                self._assert_report_rejected(
                    lambda report=report: _verify(report, profile, trust),
                    expected_error,
                )

    def test_plain_mapping_unsigned_disabled_and_template_profiles_are_rejected(
        self,
    ) -> None:
        profile, trust, authority = _signed_profile()
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            report_path = directory / "report.json"
            profile_path = directory / "profile.json"
            trust_path = directory / "trust.json"
            _write_json(report_path, _report(authority))
            _write_json(trust_path, trust)

            forged = replace(authority, signer_roles=())
            with self.assertRaisesRegex(
                FaultDrillEvidenceError, "signed P0 verification failed"
            ):
                verify_fault_drill_evidence(
                    _report(authority),
                    forged,
                    trust_path,
                    now=NOW,  # type: ignore[arg-type]
                )

            unsigned = copy.deepcopy(profile)
            unsigned["signatures"] = []
            _write_json(profile_path, unsigned)
            with self.assertRaisesRegex(
                FaultDrillEvidenceError, "signed P0 verification failed"
            ):
                verify_fault_drill_evidence(
                    _report(authority), profile_path, trust_path, now=NOW
                )
            self.assertEqual(
                main(
                    [
                        "--report",
                        str(report_path),
                        "--p0-profile",
                        str(profile_path),
                        "--p0-trust-store",
                        str(trust_path),
                        "--now",
                        NOW.isoformat(),
                    ]
                ),
                1,
            )

            disabled_template = (
                ROOT
                / "infra"
                / "policy"
                / "gate6"
                / "production-acceptance-profile.disabled-template.json"
            )
            self.assertEqual(
                main(
                    [
                        "--report",
                        str(report_path),
                        "--p0-profile",
                        str(disabled_template),
                        "--p0-trust-store",
                        str(trust_path),
                        "--now",
                        NOW.isoformat(),
                    ]
                ),
                1,
            )

    def test_test_signed_ed25519_cli_readback_remains_non_acceptance(self) -> None:
        profile, trust, authority = _signed_profile()
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            report_path = directory / "report.json"
            profile_path = directory / "profile.json"
            trust_path = directory / "trust.json"
            _write_json(report_path, _report(authority))
            _write_json(profile_path, profile)
            _write_json(trust_path, trust)
            self.assertEqual(
                main(
                    [
                        "--report",
                        str(report_path),
                        "--p0-profile",
                        str(profile_path),
                        "--p0-trust-store",
                        str(trust_path),
                        "--now",
                        NOW.isoformat(),
                    ]
                ),
                0,
            )

    def test_rto_rpo_loss_duplicate_and_nonterminal_fail_closed(self) -> None:
        profile, trust, authority = _signed_profile()
        mutations = (
            ("RPO", lambda report: report["drill"].update(rpo_seconds_observed=0)),
            ("lost", lambda report: report["after_snapshot"]["jobs"].pop()),
            (
                "duplicate",
                lambda report: report["after_snapshot"]["jobs"].append(
                    copy.deepcopy(report["after_snapshot"]["jobs"][0])
                ),
            ),
            (
                "nonterminal",
                lambda report: report["after_snapshot"]["jobs"][0].update(
                    status="queued"
                ),
            ),
        )
        for name, mutation in mutations:
            with self.subTest(name=name):
                report = _report(authority)
                mutation(report)
                with self.assertRaises(FaultDrillEvidenceError):
                    _verify(report, profile, trust)

    def test_observed_rto_and_rpo_are_bound_to_signed_profile_limits(self) -> None:
        rto_profile, rto_trust, rto_authority = _signed_profile(rto_seconds=299)
        with self.assertRaisesRegex(FaultDrillEvidenceError, "exceeds P0 RTO"):
            _verify(_report(rto_authority), rto_profile, rto_trust)

        rpo_profile, rpo_trust, rpo_authority = _signed_profile(rpo_seconds=29)
        report = _report(rpo_authority)
        started = datetime.fromisoformat(report["drill"]["started_at"])
        report["before_snapshot"]["captured_at"] = (
            started - timedelta(seconds=30)
        ).isoformat()
        report["drill"]["rpo_seconds_observed"] = 30
        with self.assertRaisesRegex(FaultDrillEvidenceError, "exceeds P0 RPO"):
            _verify(report, rpo_profile, rpo_trust)

    def test_after_only_accepted_and_incomplete_fault_outcomes_fail_closed(
        self,
    ) -> None:
        profile, trust, authority = _signed_profile()

        def make_after_only_job_accepted(report: dict) -> None:
            report["before_snapshot"]["jobs"][1] = _row(3, "retry_wait", 1)
            report["after_snapshot"]["jobs"][1] = _row(2, "queued", 0)
            report["after_snapshot"]["jobs"].append(_row(3, "succeeded", 2))
            report["drill"]["accepted_job_id_watermark"] = 3

        mutations = (
            (
                "after-only accepted queued",
                make_after_only_job_accepted,
            ),
            (
                "skipped",
                lambda report: report["timeline"][0].update(outcome="skipped"),
            ),
            (
                "not injected",
                lambda report: report["timeline"][0].update(outcome="not_injected"),
            ),
            (
                "recovery failed",
                lambda report: report["timeline"][1].update(outcome="failure"),
            ),
            (
                "missing recovery",
                lambda report: report["timeline"].pop(1),
            ),
        )
        for name, mutation in mutations:
            with self.subTest(name=name):
                report = _report(authority)
                mutation(report)
                for sequence, event in enumerate(report["timeline"], start=1):
                    event["sequence"] = sequence
                with self.assertRaises(FaultDrillEvidenceError):
                    _verify(report, profile, trust)

    def test_duplicate_json_and_bom_reports_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "report.json"
            path.write_text(
                '{"status":"complete","status":"complete"}', encoding="utf-8"
            )
            with self.assertRaisesRegex(FaultDrillEvidenceError, "duplicate JSON key"):
                read_fault_drill_report(path)
            path.write_bytes(b"\xef\xbb\xbf{}")
            with self.assertRaisesRegex(FaultDrillEvidenceError, "BOM"):
                read_fault_drill_report(path)


if __name__ == "__main__":
    unittest.main()
