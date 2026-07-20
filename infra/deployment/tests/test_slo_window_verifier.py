from __future__ import annotations

import base64
import copy
import dataclasses
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import traceback
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


ROOT = Path(__file__).resolve().parents[3]
SECURITY_SRC = ROOT / "packages" / "anila-security" / "src"
OBSERVABILITY_SRC = ROOT / "infra" / "deployment" / "observability"
for source in (SECURITY_SRC, OBSERVABILITY_SRC):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

import slo_window_verifier as verifier_module  # noqa: E402
from anila_security.production_acceptance_profile import (  # noqa: E402
    PRODUCTION_ACCEPTANCE_SCHEMA,
    VerifiedProductionAcceptanceProfile,
    canonical_json,
    production_profile_content_sha256,
    verify_production_acceptance_profile,
    sha256_hex,
    verify_signed_production_acceptance_profile,
)
from slo_window_verifier import (  # noqa: E402
    P3_ERROR_INVALID_EVIDENCE,
    P3_ERROR_INVALID_JSON,
    P3_ERROR_INVALID_PROFILE,
    P3_ERROR_RESOURCE_LIMIT,
    P3_INCIDENT_LIST_SCHEMA,
    P3_MAX_FILE_BYTES,
    P3_METRICS_EXPORT_SCHEMA,
    P3_MISSING_EXTERNAL_EVIDENCE,
    P3_SLO_FIELDS,
    P3_VERIFICATION_STATUS,
    SloWindowEvidenceError,
    read_incident_list,
    read_slo_metrics_export,
    verify_slo_window_files,
)


START = datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc)
END = START + timedelta(days=7)
ROLES = ("system_owner", "data_owner", "pki_owner", "security", "operations")
PASSING_VALUES = {
    "ingestion_p99_ms": 9_000,
    "dispatch_success_rate": 0.995,
    "queue_age_seconds": 200,
    "stuck_job_count": 0,
    "artifact_download_success_rate": 0.995,
    "auth_error_rate": 0.005,
}


def _signed_profile() -> tuple[dict, dict, dict, dict[str, Ed25519PrivateKey]]:
    inventory = {
        "schema_version": "anila.gate6.p9.enabled-callsite-inventory.v1",
        "version": "inventory.synthetic.1",
        "callsite_ids": ["router.primary"],
    }
    profile = {
        "schema_version": PRODUCTION_ACCEPTANCE_SCHEMA,
        "profile_id": "production-acceptance.p3.synthetic",
        "profile_version": "2026-07-01.1",
        "enabled": True,
        "template_only": False,
        "approval_status": "approved",
        "production_topology": {
            "topology_id": "topology.p3.synthetic",
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
        "rto_rpo": {"rto_seconds": 3_600, "rpo_seconds": 900},
        "slo_thresholds": {
            "ingestion_p99_ms": 10_000,
            "dispatch_success_rate": 0.99,
            "queue_age_seconds": 300,
            "stuck_job_count": 0,
            "artifact_download_success_rate": 0.99,
            "auth_error_rate": 0.01,
        },
        "load_profile": {
            "profile_id": "load.p3.synthetic",
            "concurrency": 10,
            "requests_per_second": 2.5,
            "duration_seconds": 3_600,
            "workflow_ids": ["chat-basic"],
        },
        "observation_window": {
            "start": START.isoformat(),
            "end": END.isoformat(),
            "minimum_duration_seconds": 7 * 86_400,
            "cadence": {"interval_seconds": 86_400, "tolerance_seconds": 0},
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
            "version": inventory["version"],
            "sha256": sha256_hex(inventory),
            "callsite_ids": inventory["callsite_ids"],
        },
        "revocation_sla": {"token_seconds": 60, "card_seconds": 300},
        "pki_policy": {
            "stale_after_seconds": 3_600,
            "offline_behavior": "fail_closed",
            "missing_behavior": "fail_closed",
            "refresh_failure_behavior": "fail_closed",
        },
        "severity_taxonomy": {
            "sev1": {
                "definition": ["auth bypass", "classification bypass"],
                "ack_seconds": 300,
                "mitigate_seconds": 3_600,
            },
            "sev2": {
                "definition": ["material degradation"],
                "ack_seconds": 1_800,
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
            "version": "impact.p3.synthetic",
            "sha256": "d" * 64,
        },
        "valid_from": (START - timedelta(days=1)).isoformat(),
        "valid_until": (END + timedelta(days=1)).isoformat(),
        "signer_roles": list(ROLES),
    }
    keys = {role: Ed25519PrivateKey.generate() for role in ROLES}
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
    _resign(profile, keys)
    return profile, trust, inventory, keys


def _resign(profile: dict, keys: dict[str, Ed25519PrivateKey]) -> None:
    profile["profile_content_sha256"] = production_profile_content_sha256(profile)
    payload = dict(profile)
    payload.pop("signatures", None)
    profile["signatures"] = [
        {
            "role": role,
            "signature": base64.b64encode(
                keys[role].sign(canonical_json(payload))
            ).decode("ascii"),
        }
        for role in ROLES
    ]


def _binding(profile: dict) -> dict:
    return {
        "profile_id": profile["profile_id"],
        "profile_version": profile["profile_version"],
        "profile_content_sha256": profile["profile_content_sha256"],
    }


def _metrics(profile: dict) -> dict:
    samples = [
        {
            "timestamp": (START + timedelta(days=day)).isoformat(),
            "values": dict(PASSING_VALUES),
        }
        for day in range(8)
    ]
    return {
        "schema_version": P3_METRICS_EXPORT_SCHEMA,
        "export_id": "p3.metrics.synthetic.1",
        "profile_binding": _binding(profile),
        "load_profile": copy.deepcopy(profile["load_profile"]),
        "workflow_ids": list(profile["load_profile"]["workflow_ids"]),
        "observation_window": copy.deepcopy(profile["observation_window"]),
        "cadence": {"interval_seconds": 86_400, "tolerance_seconds": 0},
        "samples": samples,
    }


def _incidents(profile: dict, rows: list[dict] | None = None) -> dict:
    return {
        "schema_version": P3_INCIDENT_LIST_SCHEMA,
        "incident_list_id": "p3.incidents.synthetic.1",
        "profile_binding": _binding(profile),
        "observation_window": copy.deepcopy(profile["observation_window"]),
        "incidents": list(rows or []),
    }


def _incident() -> dict:
    started = START + timedelta(days=1)
    return {
        "incident_id": "incident.synthetic.1",
        "severity": "sev1",
        "taxonomy_definition": "auth bypass",
        "started_at": started.isoformat(),
        "acknowledged_at": (started + timedelta(seconds=300)).isoformat(),
        "mitigated_at": (started + timedelta(seconds=3_600)).isoformat(),
    }


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _write_bundle(
    directory: Path,
    profile: dict,
    trust: dict,
    inventory: dict,
    metrics: dict,
    incidents: dict,
) -> tuple[Path, Path, Path, Path, Path]:
    paths = tuple(
        directory / name
        for name in (
            "metrics.json",
            "incidents.json",
            "profile.json",
            "trust.json",
            "inventory.json",
        )
    )
    for path, value in zip(
        paths, (metrics, incidents, profile, trust, inventory), strict=True
    ):
        _write_json(path, value)
    return paths  # type: ignore[return-value]


class SloWindowVerifierTests(unittest.TestCase):
    def _verify(
        self,
        directory: Path,
        *,
        profile: dict | None = None,
        trust: dict | None = None,
        inventory: dict | None = None,
        metrics: dict | None = None,
        incidents: dict | None = None,
        as_of: datetime = START,
    ):
        base_profile, base_trust, base_inventory, _keys = _signed_profile()
        profile = profile or base_profile
        trust = trust or base_trust
        inventory = inventory or base_inventory
        metrics = metrics or _metrics(profile)
        incidents = incidents or _incidents(profile)
        metric_path, incident_path, profile_path, trust_path, inventory_path = (
            _write_bundle(directory, profile, trust, inventory, metrics, incidents)
        )
        return verify_slo_window_files(
            metric_path,
            incident_path,
            profile_path,
            trust_path,
            inventory_path=inventory_path,
            as_of=as_of,
        )

    def _assert_redacted_error(
        self,
        callback,
        expected_code: str,
        *forbidden_markers: str,
    ) -> None:
        with self.assertRaises(SloWindowEvidenceError) as captured:
            callback()
        self.assertEqual(str(captured.exception), expected_code)
        self.assertIsNone(captured.exception.__cause__)
        rendered = "".join(
            traceback.format_exception(
                type(captured.exception),
                captured.exception,
                captured.exception.__traceback__,
            )
        )
        for marker in forbidden_markers:
            self.assertNotIn(marker, rendered)

    def test_perfect_seven_day_fixture_is_deterministic_non_acceptance(self) -> None:
        profile, trust, inventory, _keys = _signed_profile()
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            first = self._verify(
                directory,
                profile=profile,
                trust=trust,
                inventory=inventory,
            )
            second = self._verify(
                directory,
                profile=profile,
                trust=trust,
                inventory=inventory,
            )
        self.assertEqual(first.status, P3_VERIFICATION_STATUS)
        self.assertEqual(first.acceptance_status, "NOT_ACCEPTANCE")
        self.assertFalse(first.gate6_pass)
        expected = {
            "schema_version": "anila.gate6.p3.slo-window-verification.v1",
            "status": "VERIFIED_NON_ACCEPTANCE",
            "acceptance_status": "NOT_ACCEPTANCE",
            "gate6_pass": False,
            "production_approval": False,
            "evidence_class": "non-acceptance-p3-slo-window-evidence",
            "profile_binding": {
                "profile_id": "production-acceptance.p3.synthetic",
                "profile_version": "2026-07-01.1",
                "profile_content_sha256": profile["profile_content_sha256"],
            },
            "metrics_export_id": "p3.metrics.synthetic.1",
            "incident_list_id": "p3.incidents.synthetic.1",
            "metrics_sha256": sha256_hex(_metrics(profile)),
            "incident_list_sha256": sha256_hex(_incidents(profile)),
            "observation_window": {
                "start": "2026-07-01T00:00:00+00:00",
                "end": "2026-07-08T00:00:00+00:00",
            },
            "cadence": {
                "interval_seconds": 86_400,
                "tolerance_seconds": 0,
            },
            "sample_count": 8,
            "incident_count": 0,
            "missing_external_p3_evidence": list(P3_MISSING_EXTERNAL_EVIDENCE),
            "aggregate_method": {
                "ingestion_p99_ms": "window_max",
                "dispatch_success_rate": "window_min",
                "queue_age_seconds": "window_max",
                "stuck_job_count": "window_max",
                "artifact_download_success_rate": "window_min",
                "auth_error_rate": "window_max",
            },
            "observed_aggregates": {
                "ingestion_p99_ms": 9_000.0,
                "dispatch_success_rate": 0.995,
                "queue_age_seconds": 200.0,
                "stuck_job_count": 0.0,
                "artifact_download_success_rate": 0.995,
                "auth_error_rate": 0.005,
            },
            "signed_thresholds": {
                "ingestion_p99_ms": 10_000.0,
                "dispatch_success_rate": 0.99,
                "queue_age_seconds": 300.0,
                "stuck_job_count": 0.0,
                "artifact_download_success_rate": 0.99,
                "auth_error_rate": 0.01,
            },
        }
        self.assertEqual(first.as_dict(), expected)
        self.assertEqual(second.as_dict(), expected)
        self.assertEqual(first.sample_count, 8)
        self.assertEqual(first.incident_count, 0)
        self.assertEqual(set(first.aggregates), set(P3_SLO_FIELDS))
        self.assertEqual(
            first.missing_external_p3_evidence,
            P3_MISSING_EXTERNAL_EVIDENCE,
        )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            first.missing_external_p3_evidence = ()  # type: ignore[misc]
        rendered = first.as_dict()
        rendered["missing_external_p3_evidence"].append("attacker-marker")
        self.assertEqual(second.as_dict(), expected)

    def test_private_authority_snapshots_have_strict_modes_and_always_cleanup(
        self,
    ) -> None:
        profile, trust, inventory, _keys = _signed_profile()
        original_verify_authority = verifier_module._verify_authority
        success_roots: list[Path] = []
        failure_roots: list[Path] = []

        def inspect_snapshots(paths: tuple[str | Path, ...], roots: list[Path]) -> None:
            snapshots = tuple(Path(path) for path in paths)
            root = snapshots[0].parent
            roots.append(root)
            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
            self.assertTrue(
                all(
                    snapshot.parent == root
                    and snapshot.is_file()
                    and not snapshot.is_symlink()
                    and stat.S_IMODE(snapshot.stat().st_mode) == 0o600
                    for snapshot in snapshots
                )
            )

        def inspect_then_verify(
            profile_path,
            trust_store_path,
            inventory_path,
            *,
            as_of,
        ):
            paths = (profile_path, trust_store_path, inventory_path)
            inspect_snapshots(paths, success_roots)
            return original_verify_authority(*paths, as_of=as_of)

        def inspect_then_fail(
            profile_path,
            trust_store_path,
            inventory_path,
            *,
            as_of,
        ):
            del as_of
            inspect_snapshots(
                (profile_path, trust_store_path, inventory_path), failure_roots
            )
            raise verifier_module._InvalidProfileError

        with tempfile.TemporaryDirectory() as temp:
            paths = _write_bundle(
                Path(temp),
                profile,
                trust,
                inventory,
                _metrics(profile),
                _incidents(profile),
            )
            with patch.object(
                verifier_module, "_verify_authority", inspect_then_verify
            ):
                verified = verify_slo_window_files(
                    paths[0],
                    paths[1],
                    paths[2],
                    paths[3],
                    inventory_path=paths[4],
                    as_of=START,
                )
            self.assertFalse(verified.gate6_pass)
            self.assertTrue(success_roots)
            self.assertTrue(all(not root.exists() for root in success_roots))

            with patch.object(verifier_module, "_verify_authority", inspect_then_fail):
                with self.assertRaisesRegex(
                    SloWindowEvidenceError,
                    f"^{P3_ERROR_INVALID_PROFILE}$",
                ) as captured:
                    verify_slo_window_files(
                        paths[0],
                        paths[1],
                        paths[2],
                        paths[3],
                        inventory_path=paths[4],
                        as_of=START,
                    )
            self.assertIsNone(captured.exception.__cause__)
            rendered = "".join(
                traceback.format_exception(
                    type(captured.exception),
                    captured.exception,
                    captured.exception.__traceback__,
                )
            )
            self.assertTrue(failure_roots)
            for root in failure_roots:
                self.assertFalse(root.exists())
                self.assertNotIn(str(root), rendered)

    def test_boundary_alignment_gap_duplicate_and_out_of_window_exports_fail(
        self,
    ) -> None:
        profile, trust, inventory, _keys = _signed_profile()
        base = _metrics(profile)
        mutations = []

        # A genuinely shorter signed window belongs to the later P0/cadence
        # schema task.  This fixture instead isolates the verifier's
        # first-sample boundary-alignment guard without pretending to be a
        # re-signed six-day authority.
        first_misaligned = copy.deepcopy(base)
        first_misaligned["samples"][0]["timestamp"] = (
            START + timedelta(hours=1)
        ).isoformat()
        mutations.append(("first-sample boundary alignment", first_misaligned))

        gap = copy.deepcopy(base)
        gap["samples"][2]["timestamp"] = (START + timedelta(days=3)).isoformat()
        gap["samples"][3]["timestamp"] = (
            START + timedelta(days=3, hours=12)
        ).isoformat()
        mutations.append(("gap", gap))

        duplicate = copy.deepcopy(base)
        duplicate["samples"][2]["timestamp"] = duplicate["samples"][1]["timestamp"]
        mutations.append(("duplicate timestamp", duplicate))

        outside = copy.deepcopy(base)
        outside["samples"][0]["timestamp"] = (START - timedelta(seconds=1)).isoformat()
        mutations.append(("outside", outside))

        with tempfile.TemporaryDirectory() as temp:
            for name, metrics in mutations:
                with self.subTest(name=name), self.assertRaises(SloWindowEvidenceError):
                    self._verify(
                        Path(temp),
                        profile=profile,
                        trust=trust,
                        inventory=inventory,
                        metrics=metrics,
                    )

    def test_each_slo_direction_and_non_finite_or_boolean_values_fail(self) -> None:
        profile, trust, inventory, _keys = _signed_profile()
        failing = {
            "ingestion_p99_ms": 10_001,
            "dispatch_success_rate": 0.989,
            "queue_age_seconds": 301,
            "stuck_job_count": 1,
            "artifact_download_success_rate": 0.989,
            "auth_error_rate": 0.011,
        }
        with tempfile.TemporaryDirectory() as temp:
            for metric, value in failing.items():
                metrics = _metrics(profile)
                metrics["samples"][3]["values"][metric] = value
                with (
                    self.subTest(metric=metric),
                    self.assertRaisesRegex(
                        SloWindowEvidenceError,
                        f"^{P3_ERROR_INVALID_EVIDENCE}$",
                    ),
                ):
                    self._verify(
                        Path(temp),
                        profile=profile,
                        trust=trust,
                        inventory=inventory,
                        metrics=metrics,
                    )

    def test_numeric_tokens_source_bounds_and_exact_slo_boundaries(self) -> None:
        profile, trust, inventory, _keys = _signed_profile()
        exact_boundary = _metrics(profile)
        for sample in exact_boundary["samples"]:
            sample["values"] = copy.deepcopy(profile["slo_thresholds"])

        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            self._verify(
                directory,
                profile=profile,
                trust=trust,
                inventory=inventory,
                metrics=exact_boundary,
            )

            out_of_source_bounds = {
                "ingestion_p99_ms": 86_400_001,
                "dispatch_success_rate": 1.000_001,
                "queue_age_seconds": 365 * 86_400 + 1,
                "stuck_job_count": 100_000_001,
                "artifact_download_success_rate": 1.000_001,
                "auth_error_rate": 1.000_001,
            }
            for metric, value in out_of_source_bounds.items():
                metrics = _metrics(profile)
                metrics["samples"][0]["values"][metric] = value
                with (
                    self.subTest(bound=metric),
                    self.assertRaisesRegex(
                        SloWindowEvidenceError, f"^{P3_ERROR_INVALID_EVIDENCE}$"
                    ),
                ):
                    self._verify(
                        directory,
                        profile=profile,
                        trust=trust,
                        inventory=inventory,
                        metrics=metrics,
                    )

            huge_integer = _metrics(profile)
            huge_integer["samples"][0]["values"]["ingestion_p99_ms"] = 10**400
            with self.assertRaisesRegex(
                SloWindowEvidenceError, f"^{P3_ERROR_RESOURCE_LIMIT}$"
            ):
                self._verify(
                    directory,
                    profile=profile,
                    trust=trust,
                    inventory=inventory,
                    metrics=huge_integer,
                )

            decimal_stuck_count = _metrics(profile)
            decimal_stuck_count["samples"][0]["values"]["stuck_job_count"] = 0.0
            with self.assertRaisesRegex(
                SloWindowEvidenceError, f"^{P3_ERROR_INVALID_EVIDENCE}$"
            ):
                self._verify(
                    directory,
                    profile=profile,
                    trust=trust,
                    inventory=inventory,
                    metrics=decimal_stuck_count,
                )

            paths = _write_bundle(
                directory,
                profile,
                trust,
                inventory,
                _metrics(profile),
                _incidents(profile),
            )
            raw = paths[0].read_text(encoding="utf-8")
            replacements = {
                "ingestion_p99_ms": "9e3",
                "dispatch_success_rate": "9.95e-1",
                "queue_age_seconds": "2.005e2",
                "artifact_download_success_rate": "9.95e-1",
                "auth_error_rate": "5e-3",
            }
            for metric, replacement in replacements.items():
                raw, count = re.subn(
                    rf'("{metric}"\s*:\s*)[-+0-9.eE]+',
                    rf"\g<1>{replacement}",
                    raw,
                )
                self.assertEqual(count, 8, metric)
            paths[0].write_text(raw, encoding="utf-8")
            verify_slo_window_files(
                paths[0],
                paths[1],
                paths[2],
                paths[3],
                inventory_path=paths[4],
                as_of=START,
            )

            huge_exponent = raw.replace(
                '"ingestion_p99_ms": 9e3', '"ingestion_p99_ms": 1e400'
            )
            self.assertNotEqual(huge_exponent, raw)
            paths[0].write_text(huge_exponent, encoding="utf-8")
            with self.assertRaisesRegex(
                SloWindowEvidenceError, f"^{P3_ERROR_RESOURCE_LIMIT}$"
            ):
                verify_slo_window_files(
                    paths[0],
                    paths[1],
                    paths[2],
                    paths[3],
                    inventory_path=paths[4],
                    as_of=START,
                )

            huge_decimal = raw.replace(
                '"ingestion_p99_ms": 9e3',
                '"ingestion_p99_ms": 1.' + "0" * 130,
            )
            self.assertNotEqual(huge_decimal, raw)
            paths[0].write_text(huge_decimal, encoding="utf-8")
            with self.assertRaisesRegex(
                SloWindowEvidenceError, f"^{P3_ERROR_RESOURCE_LIMIT}$"
            ):
                verify_slo_window_files(
                    paths[0],
                    paths[1],
                    paths[2],
                    paths[3],
                    inventory_path=paths[4],
                    as_of=START,
                )

            for name, value in (("nan", float("nan")), ("bool", True)):
                metrics = _metrics(profile)
                metrics["samples"][0]["values"]["ingestion_p99_ms"] = value
                with (
                    self.subTest(value=name),
                    self.assertRaises(SloWindowEvidenceError),
                ):
                    self._verify(
                        Path(temp),
                        profile=profile,
                        trust=trust,
                        inventory=inventory,
                        metrics=metrics,
                    )

    def test_profile_tamper_unsigned_disabled_and_invalid_validity_fail(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            for name in ("tamper", "unsigned", "disabled", "expired", "not-yet-valid"):
                profile, trust, inventory, keys = _signed_profile()
                if name == "tamper":
                    profile["slo_thresholds"]["queue_age_seconds"] = 999
                elif name == "unsigned":
                    profile["signatures"] = []
                elif name == "disabled":
                    profile["enabled"] = False
                    profile["template_only"] = True
                    profile["approval_status"] = "disabled_template"
                    _resign(profile, keys)
                elif name == "expired":
                    profile["valid_until"] = START.isoformat()
                    _resign(profile, keys)
                else:
                    profile["valid_from"] = (START + timedelta(seconds=1)).isoformat()
                    _resign(profile, keys)
                with self.subTest(name=name), self.assertRaises(SloWindowEvidenceError):
                    self._verify(
                        Path(temp),
                        profile=profile,
                        trust=trust,
                        inventory=inventory,
                        metrics=_metrics(profile),
                        incidents=_incidents(profile),
                    )

    def test_profile_load_workflow_window_cadence_and_as_of_bindings_fail(self) -> None:
        profile, trust, inventory, _keys = _signed_profile()
        mutations = []
        wrong_load = _metrics(profile)
        wrong_load["load_profile"]["concurrency"] += 1
        mutations.append(("load", wrong_load))
        wrong_workflow = _metrics(profile)
        wrong_workflow["workflow_ids"] = ["other"]
        mutations.append(("workflow", wrong_workflow))
        wrong_window = _metrics(profile)
        wrong_window["observation_window"]["start"] = (
            START + timedelta(seconds=1)
        ).isoformat()
        mutations.append(("window", wrong_window))
        bad_cadence = _metrics(profile)
        bad_cadence["cadence"]["tolerance_seconds"] = 86_401
        mutations.append(("cadence", bad_cadence))

        with tempfile.TemporaryDirectory() as temp:
            for name, metrics in mutations:
                with self.subTest(name=name), self.assertRaises(SloWindowEvidenceError):
                    self._verify(
                        Path(temp),
                        profile=profile,
                        trust=trust,
                        inventory=inventory,
                        metrics=metrics,
                    )
            with self.assertRaisesRegex(
                SloWindowEvidenceError, f"^{P3_ERROR_INVALID_EVIDENCE}$"
            ):
                self._verify(
                    Path(temp),
                    profile=profile,
                    trust=trust,
                    inventory=inventory,
                    as_of=START + timedelta(seconds=1),
                )

    def test_metrics_cadence_must_equal_signed_cadence(self) -> None:
        profile, trust, _inventory, _keys = _signed_profile()
        authority = verify_production_acceptance_profile(profile, trust, now=START)
        mutations = (
            (
                "interval mismatch",
                lambda metrics: metrics["cadence"].update({"interval_seconds": 86_399}),
                "metrics cadence does not match signed P0",
            ),
            (
                "tolerance mismatch",
                lambda metrics: metrics["cadence"].update({"tolerance_seconds": 1}),
                "metrics cadence does not match signed P0",
            ),
            (
                "missing cadence",
                lambda metrics: metrics.pop("cadence"),
                "metrics export has unknown or missing fields",
            ),
        )
        for name, mutation, match in mutations:
            with self.subTest(name=name):
                metrics = _metrics(profile)
                mutation(metrics)
                with self.assertRaisesRegex(
                    SloWindowEvidenceError, f"^{re.escape(match)}$"
                ):
                    verifier_module._verify_metrics(metrics, authority)

    def test_metrics_cadence_types_are_rejected_before_signed_match(self) -> None:
        profile, trust, _inventory, _keys = _signed_profile()
        authority = verify_production_acceptance_profile(profile, trust, now=START)
        cases = (
            (
                "boolean tolerance",
                lambda metrics: metrics["cadence"].update({"tolerance_seconds": False}),
                "metrics cadence.tolerance_seconds must be an integer in range",
            ),
            (
                "float tolerance",
                lambda metrics: metrics["cadence"].update({"tolerance_seconds": 0.0}),
                "metrics cadence.tolerance_seconds must be an integer in range",
            ),
            (
                "boolean interval",
                lambda metrics: metrics["cadence"].update({"interval_seconds": True}),
                "metrics cadence.interval_seconds must be an integer in range",
            ),
            (
                "float interval",
                lambda metrics: metrics["cadence"].update(
                    {"interval_seconds": 86_400.0}
                ),
                "metrics cadence.interval_seconds must be an integer in range",
            ),
        )
        for name, mutation, match in cases:
            with self.subTest(name=name):
                metrics = _metrics(profile)
                mutation(metrics)
                with self.assertRaisesRegex(
                    SloWindowEvidenceError, f"^{re.escape(match)}$"
                ):
                    verifier_module._verify_metrics(metrics, authority)

    def test_export_observation_window_cadence_echo_is_bound(self) -> None:
        profile, trust, _inventory, _keys = _signed_profile()
        authority = verify_production_acceptance_profile(profile, trust, now=START)
        metric_cases = (
            (
                "metrics missing cadence",
                lambda metrics: metrics["observation_window"].pop("cadence"),
                "metrics observation_window has unknown or missing fields",
            ),
            (
                "metrics differing cadence",
                lambda metrics: metrics["observation_window"]["cadence"].update(
                    {"tolerance_seconds": 1}
                ),
                "metrics observation_window does not match the signed P0 profile",
            ),
            (
                "metrics boolean tolerance cadence echo",
                lambda metrics: metrics["observation_window"]["cadence"].update(
                    {"tolerance_seconds": False}
                ),
                "metrics observation_window.cadence.tolerance_seconds must be an integer in range",
            ),
            (
                "metrics float tolerance cadence echo",
                lambda metrics: metrics["observation_window"]["cadence"].update(
                    {"tolerance_seconds": 0.0}
                ),
                "metrics observation_window.cadence.tolerance_seconds must be an integer in range",
            ),
            (
                "metrics boolean interval cadence echo",
                lambda metrics: metrics["observation_window"]["cadence"].update(
                    {"interval_seconds": True}
                ),
                "metrics observation_window.cadence.interval_seconds must be an integer in range",
            ),
            (
                "metrics float interval cadence echo",
                lambda metrics: metrics["observation_window"]["cadence"].update(
                    {"interval_seconds": 86_400.0}
                ),
                "metrics observation_window.cadence.interval_seconds must be an integer in range",
            ),
        )
        for name, mutation, match in metric_cases:
            with self.subTest(name=name):
                metrics = _metrics(profile)
                mutation(metrics)
                with self.assertRaisesRegex(
                    SloWindowEvidenceError, f"^{re.escape(match)}$"
                ):
                    verifier_module._verify_metrics(metrics, authority)

        incident_cases = (
            (
                "incidents missing cadence",
                lambda incidents: incidents["observation_window"].pop("cadence"),
                "incident observation_window has unknown or missing fields",
            ),
            (
                "incidents differing cadence",
                lambda incidents: incidents["observation_window"]["cadence"].update(
                    {"tolerance_seconds": 1}
                ),
                "incident observation_window does not match the signed P0 profile",
            ),
        )
        for name, mutation, match in incident_cases:
            with self.subTest(name=name):
                incidents = _incidents(profile)
                mutation(incidents)
                with self.assertRaisesRegex(
                    SloWindowEvidenceError, f"^{re.escape(match)}$"
                ):
                    verifier_module._verify_incidents(
                        incidents,
                        authority,
                        expected_start=START,
                        expected_end=END,
                    )

    def test_exporter_cannot_relax_signed_cadence_for_sparse_samples(self) -> None:
        profile, trust, _inventory, _keys = _signed_profile()
        authority = verify_production_acceptance_profile(profile, trust, now=START)
        metrics = _metrics(profile)
        metrics["cadence"] = {"interval_seconds": 86_400, "tolerance_seconds": 86_400}
        metrics["samples"] = [
            {
                "timestamp": (START + timedelta(seconds=151_200 * index)).isoformat(),
                "values": dict(PASSING_VALUES),
            }
            for index in range(5)
        ]

        with self.assertRaisesRegex(
            SloWindowEvidenceError,
            r"^metrics cadence does not match signed P0$",
        ):
            verifier_module._verify_metrics(metrics, authority)

    def test_metrics_and_incident_profile_bindings_must_match_authority(self) -> None:
        profile, trust, inventory, _keys = _signed_profile()
        mismatches = {
            "profile_id": "production-acceptance.p3.other",
            "profile_version": "2026-07-01.2",
            "profile_content_sha256": "f" * 64,
        }
        cases = []
        for document_name in ("metrics", "incidents"):
            for field, mismatch in mismatches.items():
                metrics = _metrics(profile)
                incidents = _incidents(profile)
                document = metrics if document_name == "metrics" else incidents
                document["profile_binding"][field] = mismatch
                cases.append((document_name, field, mismatch, metrics, incidents))

        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            for document_name, field, marker, metrics, incidents in cases:
                with self.subTest(document=document_name, field=field):
                    self._assert_redacted_error(
                        lambda metrics=metrics, incidents=incidents: self._verify(
                            directory,
                            profile=profile,
                            trust=trust,
                            inventory=inventory,
                            metrics=metrics,
                            incidents=incidents,
                        ),
                        P3_ERROR_INVALID_EVIDENCE,
                        marker,
                    )

    def test_metrics_samples_must_cover_both_observation_boundaries(self) -> None:
        profile, trust, inventory, _keys = _signed_profile()
        first_late = _metrics(profile)
        first_late["samples"][0]["timestamp"] = (
            START + timedelta(seconds=1)
        ).isoformat()
        last_early = _metrics(profile)
        last_early["samples"][-1]["timestamp"] = (
            END - timedelta(seconds=1)
        ).isoformat()

        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            for name, metrics in (
                ("first late", first_late),
                ("last early", last_early),
            ):
                marker = (
                    metrics["samples"][0]["timestamp"]
                    if name == "first late"
                    else metrics["samples"][-1]["timestamp"]
                )
                with self.subTest(name=name):
                    self._assert_redacted_error(
                        lambda metrics=metrics: self._verify(
                            directory,
                            profile=profile,
                            trust=trust,
                            inventory=inventory,
                            metrics=metrics,
                        ),
                        P3_ERROR_INVALID_EVIDENCE,
                        marker,
                    )

    def test_timestamp_and_resource_limits_fail_closed(self) -> None:
        profile, trust, inventory, _keys = _signed_profile()
        invalid_timestamps = (
            "2026-07-02 00:00:00+00:00",
            "2026-07-02T00:00:00",
            "2026-07-02T00:00:00.0000001+00:00",
            "2026-07-02T00:00:00+15:00",
            "2026-07-02T00:00:00-00:00",
        )
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            for timestamp in invalid_timestamps:
                metrics = _metrics(profile)
                metrics["samples"][1]["timestamp"] = timestamp
                with (
                    self.subTest(timestamp=timestamp),
                    self.assertRaisesRegex(
                        SloWindowEvidenceError, f"^{P3_ERROR_INVALID_EVIDENCE}$"
                    ),
                ):
                    self._verify(
                        directory,
                        profile=profile,
                        trust=trust,
                        inventory=inventory,
                        metrics=metrics,
                    )

            offset_metrics = _metrics(profile)
            offset_metrics["samples"][1]["timestamp"] = "2026-07-02T08:00:00+08:00"
            self._verify(
                directory,
                profile=profile,
                trust=trust,
                inventory=inventory,
                metrics=offset_metrics,
            )

            fractional_metrics = _metrics(profile)
            fractional_metrics["samples"][1]["timestamp"] = (
                "2026-07-02T00:00:00.000000+00:00"
            )
            self._verify(
                directory,
                profile=profile,
                trust=trust,
                inventory=inventory,
                metrics=fractional_metrics,
            )

            raw = directory / "resource.json"
            raw.write_text(
                '{"nested":' + "[" * 33 + "0" + "]" * 33 + "}",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                SloWindowEvidenceError, f"^{P3_ERROR_RESOURCE_LIMIT}$"
            ):
                read_slo_metrics_export(raw)

            _write_json(raw, {"oversized": "x" * 4_097})
            with self.assertRaisesRegex(
                SloWindowEvidenceError, f"^{P3_ERROR_RESOURCE_LIMIT}$"
            ):
                read_slo_metrics_export(raw)

            raw.write_bytes(b" " * (P3_MAX_FILE_BYTES + 1))
            with self.assertRaisesRegex(
                SloWindowEvidenceError, f"^{P3_ERROR_RESOURCE_LIMIT}$"
            ):
                read_slo_metrics_export(raw)

            oversized_container = _metrics(profile)
            oversized_container["samples"] = [{}] * 100_001
            with self.assertRaisesRegex(
                SloWindowEvidenceError, f"^{P3_ERROR_RESOURCE_LIMIT}$"
            ):
                self._verify(
                    directory,
                    profile=profile,
                    trust=trust,
                    inventory=inventory,
                    metrics=oversized_container,
                )

    def test_json_structure_limits_reject_before_materialization(self) -> None:
        """The byte scanner must stop floods before ``json.loads`` allocates."""

        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            raw = directory / "structure.json"
            cases = (
                (
                    "container flood",
                    b'{"items":[[],[],[],[],[]]}',
                    "containers",
                ),
                (
                    "node flood",
                    b'{"items":[1,2,3]}',
                    "nodes",
                ),
                (
                    "member flood",
                    b'{"items":[1,2,3]}',
                    "members",
                ),
            )
            for name, payload, budget in cases:
                with self.subTest(name=name):
                    raw.write_bytes(payload)
                    limits = {}
                    if budget == "containers":
                        limits["P3_MAX_CONTAINERS"] = 4
                    elif budget == "nodes":
                        limits["P3_MAX_TOTAL_NODES"] = 5
                    else:
                        limits["P3_MAX_CONTAINER_ITEMS"] = 2
                    with (
                        patch.multiple(verifier_module, **limits),
                        patch.object(
                            verifier_module.json,
                            "loads",
                            side_effect=AssertionError("json.loads was reached"),
                        ) as loads,
                        self.assertRaisesRegex(
                            SloWindowEvidenceError,
                            f"^{P3_ERROR_RESOURCE_LIMIT}$",
                        ),
                    ):
                        read_slo_metrics_export(raw)
                    loads.assert_not_called()

            escaped = directory / "escaped.json"
            escaped.write_bytes(
                b'{"nested":[{"empty":[]}],"text":"[]{}\\"\\\\\\u005b"}'
            )
            parsed = read_slo_metrics_export(escaped)
            self.assertEqual(parsed["nested"][0]["empty"], [])
            self.assertEqual(parsed["text"], '[]{}"\\[')

            malformed = directory / "malformed.json"
            malformed.write_bytes(b'{"text":"unterminated}')
            with self.assertRaisesRegex(
                SloWindowEvidenceError, f"^{P3_ERROR_INVALID_JSON}$"
            ):
                read_slo_metrics_export(malformed)

    def test_public_errors_are_content_free_and_cause_free(self) -> None:
        profile, trust, inventory, _keys = _signed_profile()
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            missing_marker = "secret-missing-evidence-marker"
            missing_path = directory / missing_marker
            self._assert_redacted_error(
                lambda: read_slo_metrics_export(missing_path),
                P3_ERROR_INVALID_JSON,
                missing_marker,
                str(missing_path),
                "FileNotFoundError",
            )

            utf8_marker = "secret-bad-utf8-marker"
            bad_utf8_path = directory / f"{utf8_marker}.json"
            bad_utf8_path.write_bytes(b'{"private":"\xff"}')
            self._assert_redacted_error(
                lambda: read_slo_metrics_export(bad_utf8_path),
                P3_ERROR_INVALID_JSON,
                utf8_marker,
                str(bad_utf8_path),
                "UnicodeDecodeError",
            )

            malformed_marker = "secret-malformed-json-marker"
            malformed_path = directory / f"{malformed_marker}.json"
            malformed_path.write_text(
                '{"private":"secret-malformed-body",', encoding="utf-8"
            )
            self._assert_redacted_error(
                lambda: read_slo_metrics_export(malformed_path),
                P3_ERROR_INVALID_JSON,
                malformed_marker,
                str(malformed_path),
                "secret-malformed-body",
                "JSONDecodeError",
            )

            duplicate_marker = "secret-duplicate-key-marker"
            duplicate_path = directory / f"{duplicate_marker}.json"
            duplicate_path.write_text(
                '{"secret-duplicate-key-marker":1,"secret-duplicate-key-marker":2}',
                encoding="utf-8",
            )
            self._assert_redacted_error(
                lambda: read_slo_metrics_export(duplicate_path),
                P3_ERROR_INVALID_JSON,
                duplicate_marker,
                str(duplicate_path),
                "_InvalidJsonError",
            )

            paths = _write_bundle(
                directory,
                profile,
                trust,
                inventory,
                _metrics(profile),
                _incidents(profile),
            )
            profile_marker = "secret-profile-signature-marker"
            profile["signatures"][0]["signature"] = profile_marker
            marked_profile_path = directory / f"{profile_marker}.json"
            _write_json(marked_profile_path, profile)
            self._assert_redacted_error(
                lambda: verify_slo_window_files(
                    paths[0],
                    paths[1],
                    marked_profile_path,
                    paths[3],
                    inventory_path=paths[4],
                    as_of=START,
                ),
                P3_ERROR_INVALID_PROFILE,
                profile_marker,
                str(marked_profile_path),
                "ProductionAcceptanceProfileError",
                "invalid signature",
            )

            resource_marker = "secret-resource-limit-marker"
            resource_path = directory / f"{resource_marker}.json"
            resource_path.write_bytes(b" " * (P3_MAX_FILE_BYTES + 1))
            self._assert_redacted_error(
                lambda: read_slo_metrics_export(resource_path),
                P3_ERROR_RESOURCE_LIMIT,
                resource_marker,
                str(resource_path),
                "_ResourceLimitError",
            )

            incident_json_marker = "secret-incident-json-marker"
            invalid_incident_path = directory / f"{incident_json_marker}.json"
            invalid_incident_path.write_text(
                '{"private":"secret-incident-body",', encoding="utf-8"
            )
            self._assert_redacted_error(
                lambda: read_incident_list(invalid_incident_path),
                P3_ERROR_INVALID_JSON,
                incident_json_marker,
                str(invalid_incident_path),
                "secret-incident-body",
                "JSONDecodeError",
            )

            incident_resource_marker = "secret-incident-resource-marker"
            oversized_incident_path = directory / f"{incident_resource_marker}.json"
            oversized_incident_path.write_bytes(b" " * (P3_MAX_FILE_BYTES + 1))
            self._assert_redacted_error(
                lambda: read_incident_list(oversized_incident_path),
                P3_ERROR_RESOURCE_LIMIT,
                incident_resource_marker,
                str(oversized_incident_path),
                "_ResourceLimitError",
            )

            unexpected_marker = "secret-unexpected-internal-marker"
            with patch.object(
                verifier_module,
                "_verify_slo_window_files",
                side_effect=RuntimeError(unexpected_marker),
            ):
                self._assert_redacted_error(
                    lambda: verify_slo_window_files(
                        paths[0],
                        paths[1],
                        paths[2],
                        paths[3],
                        inventory_path=paths[4],
                        as_of=START,
                    ),
                    P3_ERROR_INVALID_EVIDENCE,
                    unexpected_marker,
                    "RuntimeError",
                )

    def test_authority_inputs_use_bounded_redacted_preflight(self) -> None:
        profile, trust, inventory, _keys = _signed_profile()
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            resource_cases = (
                (
                    1,
                    b'{"private":"incident-oversized-marker"}'
                    + b" " * P3_MAX_FILE_BYTES,
                    "incident-oversized-marker",
                ),
                (
                    2,
                    b'{"private":"profile-oversized-marker"}'
                    + b" " * P3_MAX_FILE_BYTES,
                    "profile-oversized-marker",
                ),
                (
                    3,
                    (
                        '{"private":"trust-depth-marker","nested":'
                        + "[" * 33
                        + "0"
                        + "]" * 33
                        + "}"
                    ).encode(),
                    "trust-depth-marker",
                ),
                (
                    4,
                    (
                        '{"private":"inventory-token-marker","value":' + "1" * 129 + "}"
                    ).encode(),
                    "inventory-token-marker",
                ),
            )
            for path_index, payload, marker in resource_cases:
                paths = _write_bundle(
                    directory,
                    profile,
                    trust,
                    inventory,
                    _metrics(profile),
                    _incidents(profile),
                )
                paths[path_index].write_bytes(payload)
                with self.subTest(marker=marker):
                    self._assert_redacted_error(
                        lambda paths=paths: verify_slo_window_files(
                            paths[0],
                            paths[1],
                            paths[2],
                            paths[3],
                            inventory_path=paths[4],
                            as_of=START,
                        ),
                        P3_ERROR_RESOURCE_LIMIT,
                        marker,
                        str(paths[path_index]),
                        "_ResourceLimitError",
                    )

    def test_symlink_and_special_file_inputs_fail_without_blocking(self) -> None:
        profile, trust, inventory, _keys = _signed_profile()
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            target = directory / "metrics-target.json"
            _write_json(target, _metrics(profile))
            symlink_marker = "secret-symlink-marker"
            symlink = directory / symlink_marker
            symlink.symlink_to(target)
            self._assert_redacted_error(
                lambda: read_slo_metrics_export(symlink),
                P3_ERROR_INVALID_JSON,
                symlink_marker,
                str(symlink),
            )

            if not hasattr(os, "mkfifo"):
                self.skipTest("FIFO creation is unavailable on this platform")
            fifo_marker = "secret-fifo-marker"
            fifo = directory / fifo_marker
            os.mkfifo(fifo)
            env = os.environ.copy()
            env["PYTHONPATH"] = os.pathsep.join(
                (str(SECURITY_SRC), str(OBSERVABILITY_SRC))
            )
            script = """
import sys
import traceback
from slo_window_verifier import (
    P3_ERROR_INVALID_JSON,
    SloWindowEvidenceError,
    read_slo_metrics_export,
)
try:
    read_slo_metrics_export(sys.argv[1])
except SloWindowEvidenceError as exc:
    rendered = "".join(
        traceback.format_exception(type(exc), exc, exc.__traceback__)
    )
    if (
        str(exc) == P3_ERROR_INVALID_JSON
        and exc.__cause__ is None
        and sys.argv[1] not in rendered
        and "secret-fifo-marker" not in rendered
    ):
        raise SystemExit(0)
raise SystemExit(1)
"""
            completed = subprocess.run(
                [sys.executable, "-c", script, str(fifo)],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_incident_timing_taxonomy_duplicates_and_unresolved_fail(self) -> None:
        profile, trust, inventory, _keys = _signed_profile()
        valid = _incident()
        late_ack = copy.deepcopy(valid)
        late_ack["acknowledged_at"] = (
            datetime.fromisoformat(valid["started_at"]) + timedelta(seconds=301)
        ).isoformat()
        late_ack["incident_id"] = "incident.secret-late-ack"
        late_mitigation = copy.deepcopy(valid)
        late_mitigation["mitigated_at"] = (
            datetime.fromisoformat(valid["started_at"]) + timedelta(seconds=3_601)
        ).isoformat()
        late_mitigation["incident_id"] = "incident.secret-late-mitigation"
        wrong_taxonomy = copy.deepcopy(valid)
        wrong_taxonomy["taxonomy_definition"] = "attacker-selected"
        wrong_taxonomy["incident_id"] = "incident.secret-taxonomy"
        partial_taxonomy = copy.deepcopy(valid)
        partial_taxonomy["taxonomy_definition"] = "auth"
        partial_taxonomy["incident_id"] = "incident.secret-partial-taxonomy"
        unresolved = copy.deepcopy(valid)
        del unresolved["mitigated_at"]
        unresolved["incident_id"] = "incident.secret-unresolved"
        inconsistent = copy.deepcopy(valid)
        inconsistent["acknowledged_at"] = (
            datetime.fromisoformat(valid["started_at"]) - timedelta(seconds=1)
        ).isoformat()
        inconsistent["incident_id"] = "incident.secret-inconsistent"
        before_window = copy.deepcopy(valid)
        before_window["started_at"] = (START - timedelta(seconds=1)).isoformat()
        before_window["incident_id"] = "incident.secret-before-window"
        after_window = copy.deepcopy(valid)
        after_window["mitigated_at"] = (END + timedelta(seconds=1)).isoformat()
        after_window["incident_id"] = "incident.secret-after-window"
        mitigated_before_ack = copy.deepcopy(valid)
        incident_start = datetime.fromisoformat(valid["started_at"])
        mitigated_before_ack["acknowledged_at"] = (
            incident_start + timedelta(seconds=200)
        ).isoformat()
        mitigated_before_ack["mitigated_at"] = (
            incident_start + timedelta(seconds=100)
        ).isoformat()
        mitigated_before_ack["incident_id"] = "incident.secret-reversed-chain"
        duplicate = copy.deepcopy(valid)
        duplicate["incident_id"] = "incident.secret-duplicate"
        mutations = (
            ("late ack", [late_ack], late_ack["incident_id"]),
            (
                "late mitigation",
                [late_mitigation],
                late_mitigation["incident_id"],
            ),
            ("taxonomy", [wrong_taxonomy], wrong_taxonomy["incident_id"]),
            (
                "partial taxonomy",
                [partial_taxonomy],
                partial_taxonomy["incident_id"],
            ),
            ("unresolved", [unresolved], unresolved["incident_id"]),
            ("inconsistent", [inconsistent], inconsistent["incident_id"]),
            ("before window", [before_window], before_window["incident_id"]),
            ("after window", [after_window], after_window["incident_id"]),
            (
                "mitigated before acknowledged",
                [mitigated_before_ack],
                mitigated_before_ack["incident_id"],
            ),
            (
                "duplicate",
                [duplicate, copy.deepcopy(duplicate)],
                duplicate["incident_id"],
            ),
        )
        with tempfile.TemporaryDirectory() as temp:
            for name, rows, marker in mutations:
                with self.subTest(name=name):
                    self._assert_redacted_error(
                        lambda rows=rows: self._verify(
                            Path(temp),
                            profile=profile,
                            trust=trust,
                            inventory=inventory,
                            incidents=_incidents(profile, rows),
                        ),
                        P3_ERROR_INVALID_EVIDENCE,
                        marker,
                    )

            result = self._verify(
                Path(temp),
                profile=profile,
                trust=trust,
                inventory=inventory,
                incidents=_incidents(profile, [valid]),
            )
            self.assertEqual(result.incident_count, 1)

    def test_incident_timing_accepts_window_and_adjacent_equalities(self) -> None:
        profile, trust, inventory, _keys = _signed_profile()
        boundary_rows = [
            {
                "incident_id": "incident.boundary-start",
                "severity": "sev1",
                "taxonomy_definition": "auth bypass",
                "started_at": START.isoformat(),
                "acknowledged_at": START.isoformat(),
                "mitigated_at": START.isoformat(),
            },
            {
                "incident_id": "incident.boundary-end",
                "severity": "sev1",
                "taxonomy_definition": "classification bypass",
                "started_at": END.isoformat(),
                "acknowledged_at": END.isoformat(),
                "mitigated_at": END.isoformat(),
            },
        ]
        incidents = _incidents(profile, boundary_rows)
        metrics = _metrics(profile)
        with tempfile.TemporaryDirectory() as temp:
            result = self._verify(
                Path(temp),
                profile=profile,
                trust=trust,
                inventory=inventory,
                metrics=metrics,
                incidents=incidents,
            )

        expected = {
            "schema_version": "anila.gate6.p3.slo-window-verification.v1",
            "status": "VERIFIED_NON_ACCEPTANCE",
            "acceptance_status": "NOT_ACCEPTANCE",
            "gate6_pass": False,
            "production_approval": False,
            "evidence_class": "non-acceptance-p3-slo-window-evidence",
            "profile_binding": {
                "profile_id": "production-acceptance.p3.synthetic",
                "profile_version": "2026-07-01.1",
                "profile_content_sha256": profile["profile_content_sha256"],
            },
            "metrics_export_id": "p3.metrics.synthetic.1",
            "incident_list_id": "p3.incidents.synthetic.1",
            "metrics_sha256": sha256_hex(metrics),
            "incident_list_sha256": sha256_hex(incidents),
            "observation_window": {
                "start": "2026-07-01T00:00:00+00:00",
                "end": "2026-07-08T00:00:00+00:00",
            },
            "cadence": {
                "interval_seconds": 86_400,
                "tolerance_seconds": 0,
            },
            "sample_count": 8,
            "incident_count": 2,
            "missing_external_p3_evidence": list(P3_MISSING_EXTERNAL_EVIDENCE),
            "aggregate_method": {
                "ingestion_p99_ms": "window_max",
                "dispatch_success_rate": "window_min",
                "queue_age_seconds": "window_max",
                "stuck_job_count": "window_max",
                "artifact_download_success_rate": "window_min",
                "auth_error_rate": "window_max",
            },
            "observed_aggregates": {
                "ingestion_p99_ms": 9_000.0,
                "dispatch_success_rate": 0.995,
                "queue_age_seconds": 200.0,
                "stuck_job_count": 0.0,
                "artifact_download_success_rate": 0.995,
                "auth_error_rate": 0.005,
            },
            "signed_thresholds": {
                "ingestion_p99_ms": 10_000.0,
                "dispatch_success_rate": 0.99,
                "queue_age_seconds": 300.0,
                "stuck_job_count": 0.0,
                "artifact_download_success_rate": 0.99,
                "auth_error_rate": 0.01,
            },
        }
        self.assertEqual(result.as_dict(), expected)
        self.assertEqual(result.incident_count, 2)

    def test_sev2_signed_taxonomy_and_sla_boundaries(self) -> None:
        profile, trust, inventory, _keys = _signed_profile()
        started = START + timedelta(days=1)
        valid = {
            "incident_id": "incident.sev2-boundary",
            "severity": "sev2",
            "taxonomy_definition": "material degradation",
            "started_at": started.isoformat(),
            "acknowledged_at": (started + timedelta(seconds=1_800)).isoformat(),
            "mitigated_at": (started + timedelta(seconds=86_400)).isoformat(),
        }
        incident_list = _incidents(profile, [valid])
        metrics = _metrics(profile)
        late_ack = copy.deepcopy(valid)
        late_ack["incident_id"] = "incident.secret-sev2-late-ack"
        late_ack["acknowledged_at"] = (started + timedelta(seconds=1_801)).isoformat()
        late_mitigation = copy.deepcopy(valid)
        late_mitigation["incident_id"] = "incident.secret-sev2-late-mitigation"
        late_mitigation["mitigated_at"] = (
            started + timedelta(seconds=86_401)
        ).isoformat()

        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            result = self._verify(
                directory,
                profile=profile,
                trust=trust,
                inventory=inventory,
                metrics=metrics,
                incidents=incident_list,
            )
            for name, incident in (
                ("late ack", late_ack),
                ("late mitigation", late_mitigation),
            ):
                with self.subTest(name=name):
                    self._assert_redacted_error(
                        lambda incident=incident: self._verify(
                            directory,
                            profile=profile,
                            trust=trust,
                            inventory=inventory,
                            incidents=_incidents(profile, [incident]),
                        ),
                        P3_ERROR_INVALID_EVIDENCE,
                        incident["incident_id"],
                    )

        serialized = result.as_dict()
        self.assertEqual(
            {
                key: serialized[key]
                for key in (
                    "schema_version",
                    "status",
                    "acceptance_status",
                    "gate6_pass",
                    "production_approval",
                    "incident_list_sha256",
                    "incident_count",
                )
            },
            {
                "schema_version": "anila.gate6.p3.slo-window-verification.v1",
                "status": "VERIFIED_NON_ACCEPTANCE",
                "acceptance_status": "NOT_ACCEPTANCE",
                "gate6_pass": False,
                "production_approval": False,
                "incident_list_sha256": sha256_hex(incident_list),
                "incident_count": 1,
            },
        )
        self.assertEqual(result.incident_count, 1)

    def test_unknown_fields_duplicate_keys_bom_and_bad_utf8_fail(self) -> None:
        profile, trust, inventory, _keys = _signed_profile()
        metrics = _metrics(profile)
        metrics["unknown"] = True
        incidents = _incidents(profile)
        incidents["unknown"] = True
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            for name, kwargs in (
                ("metrics", {"metrics": metrics}),
                ("incidents", {"incidents": incidents}),
            ):
                with self.subTest(name=name), self.assertRaises(SloWindowEvidenceError):
                    self._verify(
                        directory,
                        profile=profile,
                        trust=trust,
                        inventory=inventory,
                        **kwargs,
                    )

            raw = directory / "raw.json"
            raw.write_text('{"schema_version":"one","schema_version":"two"}')
            with self.assertRaisesRegex(
                SloWindowEvidenceError, f"^{P3_ERROR_INVALID_JSON}$"
            ):
                read_slo_metrics_export(raw)
            raw.write_bytes(b"\xef\xbb\xbf{}")
            with self.assertRaisesRegex(
                SloWindowEvidenceError, f"^{P3_ERROR_INVALID_JSON}$"
            ):
                read_slo_metrics_export(raw)
            raw.write_bytes(b'{"marker":"\xff"}')
            with self.assertRaisesRegex(
                SloWindowEvidenceError, f"^{P3_ERROR_INVALID_JSON}$"
            ):
                read_slo_metrics_export(raw)

    def test_public_api_rejects_boundary_forged_authority(self) -> None:
        profile, trust, inventory, _keys = _signed_profile()
        with tempfile.TemporaryDirectory() as temp:
            paths = _write_bundle(
                Path(temp),
                profile,
                trust,
                inventory,
                _metrics(profile),
                _incidents(profile),
            )
            real_authority = verify_signed_production_acceptance_profile(
                paths[2],
                paths[3],
                inventory_path=paths[4],
                now=START,
            )
            clone_kwargs = {
                field.name: getattr(real_authority, field.name)
                for field in dataclasses.fields(real_authority)
            }

            class ForgedAuthority(VerifiedProductionAcceptanceProfile):
                __slots__ = ()

            forged = ForgedAuthority(**clone_kwargs)
            self.assertIsInstance(forged, VerifiedProductionAcceptanceProfile)
            self.assertIsNot(type(forged), VerifiedProductionAcceptanceProfile)

            # Exercise the actual authority boundary and assert the internal
            # guard reason, rather than stopping at the path-type preflight.
            with patch.object(
                verifier_module,
                "verify_signed_production_acceptance_profile",
                return_value=forged,
            ) as boundary_mock:
                with self.assertRaisesRegex(
                    SloWindowEvidenceError,
                    "^signed P0 verifier returned an invalid authority type$",
                ):
                    verifier_module._verify_authority(
                        paths[2],
                        paths[3],
                        paths[4],
                        as_of=START,
                    )
            boundary_mock.assert_called_once()

            # The public boundary must flatten that precise internal reason to
            # the stable, content-free error code.
            with patch.object(
                verifier_module,
                "verify_signed_production_acceptance_profile",
                return_value=forged,
            ) as public_boundary_mock:
                self._assert_redacted_error(
                    lambda: verify_slo_window_files(
                        paths[0],
                        paths[1],
                        paths[2],
                        paths[3],
                        inventory_path=paths[4],
                        as_of=START,
                    ),
                    P3_ERROR_INVALID_EVIDENCE,
                    "ForgedAuthority",
                )
            public_boundary_mock.assert_called_once()

    def test_internal_authority_type_guard_rejects_wrong_verifier_return(self) -> None:
        profile, trust, inventory, _keys = _signed_profile()
        with tempfile.TemporaryDirectory() as temp:
            paths = _write_bundle(
                Path(temp),
                profile,
                trust,
                inventory,
                _metrics(profile),
                _incidents(profile),
            )

            real_authority = verify_signed_production_acceptance_profile(
                paths[2],
                paths[3],
                inventory_path=paths[4],
                now=START,
            )
            clone_kwargs = {
                field.name: getattr(real_authority, field.name)
                for field in dataclasses.fields(real_authority)
            }

            class SecretSubclassedAuthority(VerifiedProductionAcceptanceProfile):
                __slots__ = ()

            subclassed_authority = SecretSubclassedAuthority(**clone_kwargs)
            self.assertIsInstance(
                subclassed_authority, VerifiedProductionAcceptanceProfile
            )
            self.assertIsNot(
                type(subclassed_authority), VerifiedProductionAcceptanceProfile
            )

            with patch.object(
                verifier_module,
                "verify_signed_production_acceptance_profile",
                return_value=VerifiedProductionAcceptanceProfile(**clone_kwargs),
            ) as exact_mock:
                result = verify_slo_window_files(
                    paths[0],
                    paths[1],
                    paths[2],
                    paths[3],
                    inventory_path=paths[4],
                    as_of=START,
                )
            exact_mock.assert_called_once()
            self.assertEqual(result.status, P3_VERIFICATION_STATUS)
            self.assertEqual(result.acceptance_status, "NOT_ACCEPTANCE")
            self.assertFalse(result.gate6_pass)

            with patch.object(
                verifier_module,
                "verify_signed_production_acceptance_profile",
                return_value=subclassed_authority,
            ) as subclass_mock:
                self._assert_redacted_error(
                    lambda: verify_slo_window_files(
                        paths[0],
                        paths[1],
                        paths[2],
                        paths[3],
                        inventory_path=paths[4],
                        as_of=START,
                    ),
                    P3_ERROR_INVALID_EVIDENCE,
                    "SecretSubclassedAuthority",
                    profile["profile_id"],
                )
            subclass_mock.assert_called_once()

    def test_internal_authority_type_guard_redacts_wrong_object(self) -> None:
        profile, trust, inventory, _keys = _signed_profile()
        marker = "secret-wrong-authority-type"
        with tempfile.TemporaryDirectory() as temp:
            paths = _write_bundle(
                Path(temp),
                profile,
                trust,
                inventory,
                _metrics(profile),
                _incidents(profile),
            )
            with patch.object(
                verifier_module,
                "verify_signed_production_acceptance_profile",
                return_value={"private": marker},
            ) as mocked_verify:
                self._assert_redacted_error(
                    lambda: verify_slo_window_files(
                        paths[0],
                        paths[1],
                        paths[2],
                        paths[3],
                        inventory_path=paths[4],
                        as_of=START,
                    ),
                    P3_ERROR_INVALID_EVIDENCE,
                    marker,
                )
            mocked_verify.assert_called_once()


if __name__ == "__main__":
    unittest.main()
