#!/usr/bin/env python3
"""Fail-closed verification of Gate 6 P1 restore-drill evidence.

This module verifies an *already captured* restore report.  It does not run a
restore, contact a service, mutate a database, or write evidence.  The input
report is deliberately an exact, small JSON contract so that a missing field
cannot be mistaken for a passing check.

The verifier accepts only :class:`VerifiedProductionAcceptanceProfile` from
``anila-security``.  A regular mapping (even one containing fields that look
like a verified profile) is rejected; P0 RTO/RPO thresholds therefore cannot be
chosen by the restore report itself.

The returned object is explicitly non-acceptance evidence.  It always carries
``gate6_pass=False`` and no caller can use this helper to create a signature,
five-party approval, or a production Go decision.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any


# Allow this standalone deployment helper to run from a source checkout while
# still using the shared P0 verifier/canonical hashing implementation.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_SECURITY_SRC = _REPO_ROOT / "packages" / "anila-security" / "src"
if _SECURITY_SRC.is_dir() and str(_SECURITY_SRC) not in sys.path:
    sys.path.insert(0, str(_SECURITY_SRC))

from anila_security.production_acceptance_profile import (  # noqa: E402
    VerifiedProductionAcceptanceProfile,
    canonical_json,
    production_profile_content_sha256,
    validate_production_acceptance_profile,
    verify_signed_production_acceptance_profile,
)


P1_RESTORE_EVIDENCE_SCHEMA = "anila.gate6.p1.restore-evidence.v1"
RESTORE_REPORT_SCHEMA = P1_RESTORE_EVIDENCE_SCHEMA
RESTORE_EVIDENCE_STATUS = "VERIFIED_NON_ACCEPTANCE"
RESTORE_ACCEPTANCE_STATUS = "NOT_ACCEPTANCE"

_REPORT_FIELDS = frozenset(
    {
        "schema_version",
        "evidence_id",
        "status",
        "generated_at",
        "environment",
        "synthetic",
        "profile_binding",
        "restore",
        "datasets",
        "negative_controls",
    }
)
_PROFILE_BINDING_FIELDS = frozenset(
    {"profile_id", "profile_version", "profile_content_sha256"}
)
_RESTORE_FIELDS = frozenset(
    {
        "backup_id",
        "backup_created_at",
        "restore_started_at",
        "restore_completed_at",
        "restore_seconds",
        "rpo_seconds_observed",
    }
)
_DATASET_FIELDS = frozenset(
    {"row_counts", "checksums", "referential_integrity"}
)
_EXPECTED_ACTUAL_FIELDS = frozenset({"expected", "actual"})
_REFERENTIAL_FIELDS = frozenset({"checked", "violations"})
_CONTROL_FIELDS = frozenset(
    {"id", "attempted", "rejected", "observed_status"}
)
_DATASET_NAMES = frozenset({"db", "blob", "artifact", "vector_generation"})
_CONTROL_NAMES = frozenset({"rls", "compartment", "revocation"})
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_SHA256_RE = re.compile(r"^(?:sha256:)?[0-9a-f]{64}$")
_BACKUP_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,127}$")
_REJECTED_OUTCOMES = frozenset(
    {
        "blocked",
        "denied",
        "fail_closed",
        "forbidden",
        "not_found",
        "rejected",
        "revoked",
        "unauthorized",
    }
)
_ENVIRONMENTS = frozenset({"synthetic", "non-production", "production-equivalent"})


class RestoreEvidenceError(ValueError):
    """Raised when restore evidence is incomplete, inconsistent, or unsafe."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RestoreEvidenceError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def read_restore_report(path: str | Path) -> dict[str, Any]:
    """Read one strict UTF-8 restore report without mutating the source."""

    report_path = Path(path)
    try:
        raw = report_path.read_bytes()
    except OSError as exc:
        raise RestoreEvidenceError(f"cannot read restore report: {report_path}") from exc
    if raw.startswith(b"\xef\xbb\xbf"):
        raise RestoreEvidenceError("UTF-8 BOM is forbidden in restore report")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RestoreEvidenceError(f"invalid restore report JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise RestoreEvidenceError("restore report root must be an object")
    return value


def _exact_object(value: Any, expected: frozenset[str], field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != expected:
        missing = sorted(expected - set(value)) if isinstance(value, Mapping) else sorted(expected)
        extra = sorted(set(value) - expected) if isinstance(value, Mapping) else []
        raise RestoreEvidenceError(f"{field} has unknown or missing fields; missing={missing}, extra={extra}")
    return value


def _non_empty_string(value: Any, field: str, *, max_length: int = 256) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > max_length:
        raise RestoreEvidenceError(f"{field} must be a non-empty trimmed string")
    return value


def _identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
        raise RestoreEvidenceError(f"{field} must be an identifier")
    return value


def _sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise RestoreEvidenceError(f"{field} must be a SHA-256 digest")
    return value


def _non_negative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RestoreEvidenceError(f"{field} must be a non-negative integer")
    return value


def _parse_rfc3339(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise RestoreEvidenceError(f"{field} must be RFC3339 with timezone")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RestoreEvidenceError(f"{field} must be RFC3339 with timezone") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RestoreEvidenceError(f"{field} must include timezone")
    return parsed.astimezone(timezone.utc)


def _normalise_now(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise RestoreEvidenceError("now must include timezone")
    return current.astimezone(timezone.utc)


def _verify_p0_authority(
    authority: VerifiedProductionAcceptanceProfile,
    *,
    profile_binding: Mapping[str, Any],
    restore_started_at: datetime,
    restore_completed_at: datetime,
) -> tuple[int, int]:
    """Re-check immutable authority metadata before using P0 thresholds.

    The dataclass is the only accepted input type, but nested mappings can be
    mutated by a careless caller.  Recomputing the shared P0 content hash and
    checking its validity window prevents using a tampered authority object.
    Cryptographic signer verification remains the responsibility of the P0
    verifier that produced this authority.
    """

    if not isinstance(authority, VerifiedProductionAcceptanceProfile):
        raise RestoreEvidenceError(
            "P0 authority must be VerifiedProductionAcceptanceProfile from the signed verifier"
        )
    profile = authority.profile
    if not isinstance(profile, Mapping):
        raise RestoreEvidenceError("verified P0 authority lacks profile content")
    try:
        validate_production_acceptance_profile(profile, now=restore_started_at)
    except Exception as exc:  # noqa: BLE001 - boundary must fail closed
        raise RestoreEvidenceError(f"verified P0 authority is no longer valid: {exc}") from exc
    try:
        content_hash = production_profile_content_sha256(profile)
    except Exception as exc:  # noqa: BLE001 - malformed nested mapping
        raise RestoreEvidenceError("verified P0 authority content hash cannot be recomputed") from exc
    if content_hash != authority.profile_content_sha256:
        raise RestoreEvidenceError("verified P0 authority content hash mismatch")
    expected_binding = {
        "profile_id": authority.profile_id,
        "profile_version": authority.profile_version,
        "profile_content_sha256": authority.profile_content_sha256,
    }
    if dict(profile_binding) != expected_binding:
        raise RestoreEvidenceError("restore report is bound to a different P0 profile")
    if not authority.valid_from <= restore_started_at < authority.valid_until:
        raise RestoreEvidenceError("restore started outside the P0 profile validity window")
    if not authority.valid_from < restore_completed_at <= authority.valid_until:
        raise RestoreEvidenceError("restore completed outside the P0 profile validity window")
    rto_rpo = profile.get("rto_rpo")
    if not isinstance(rto_rpo, Mapping) or set(rto_rpo) != {"rto_seconds", "rpo_seconds"}:
        raise RestoreEvidenceError("verified P0 authority lacks an exact rto_rpo contract")
    rto = rto_rpo["rto_seconds"]
    rpo = rto_rpo["rpo_seconds"]
    if isinstance(rto, bool) or not isinstance(rto, int) or rto < 1:
        raise RestoreEvidenceError("verified P0 rto_seconds is invalid")
    if isinstance(rpo, bool) or not isinstance(rpo, int) or rpo < 1:
        raise RestoreEvidenceError("verified P0 rpo_seconds is invalid")
    return rto, rpo


def _verify_profile_binding(value: Any) -> Mapping[str, Any]:
    binding = _exact_object(value, _PROFILE_BINDING_FIELDS, "profile_binding")
    _identifier(binding["profile_id"], "profile_binding.profile_id")
    _non_empty_string(binding["profile_version"], "profile_binding.profile_version")
    _sha256(binding["profile_content_sha256"], "profile_binding.profile_content_sha256")
    return binding


def _verify_restore_timing(value: Any) -> tuple[Mapping[str, Any], datetime, datetime, int, int]:
    restore = _exact_object(value, _RESTORE_FIELDS, "restore")
    backup_id = restore["backup_id"]
    if not isinstance(backup_id, str) or not _BACKUP_ID_RE.fullmatch(backup_id):
        raise RestoreEvidenceError("restore.backup_id is invalid")
    backup_created = _parse_rfc3339(restore["backup_created_at"], "restore.backup_created_at")
    started = _parse_rfc3339(restore["restore_started_at"], "restore.restore_started_at")
    completed = _parse_rfc3339(restore["restore_completed_at"], "restore.restore_completed_at")
    if backup_created > started:
        raise RestoreEvidenceError("restore.backup_created_at must precede restore start")
    if completed <= started:
        raise RestoreEvidenceError("restore.restore_completed_at must follow restore start")
    duration = _non_negative_int(restore["restore_seconds"], "restore.restore_seconds")
    observed_elapsed = (completed - started).total_seconds()
    if observed_elapsed != duration:
        raise RestoreEvidenceError(
            "restore.restore_seconds must equal restore_completed_at - restore_started_at"
        )
    rpo_observed = _non_negative_int(
        restore["rpo_seconds_observed"], "restore.rpo_seconds_observed"
    )
    return restore, started, completed, duration, rpo_observed


def _verify_count_map(value: Any, field: str) -> dict[str, int]:
    if not isinstance(value, Mapping) or not value:
        raise RestoreEvidenceError(f"{field} must be a non-empty object")
    result: dict[str, int] = {}
    for key, count in value.items():
        _identifier(key, f"{field} key")
        result[key] = _non_negative_int(count, f"{field}.{key}")
    return result


def _verify_checksum_map(value: Any, field: str) -> dict[str, str]:
    if not isinstance(value, Mapping) or not value:
        raise RestoreEvidenceError(f"{field} must be a non-empty object")
    result: dict[str, str] = {}
    for key, digest in value.items():
        _identifier(key, f"{field} key")
        result[key] = _sha256(digest, f"{field}.{key}")
    return result


def _verify_dataset(value: Any, name: str) -> dict[str, Any]:
    dataset = _exact_object(value, _DATASET_FIELDS, f"datasets.{name}")
    counts = _exact_object(dataset["row_counts"], _EXPECTED_ACTUAL_FIELDS, f"datasets.{name}.row_counts")
    expected_counts = _verify_count_map(counts["expected"], f"datasets.{name}.row_counts.expected")
    actual_counts = _verify_count_map(counts["actual"], f"datasets.{name}.row_counts.actual")
    if set(expected_counts) != set(actual_counts) or expected_counts != actual_counts:
        raise RestoreEvidenceError(f"datasets.{name} row counts do not match")
    checksums = _exact_object(dataset["checksums"], _EXPECTED_ACTUAL_FIELDS, f"datasets.{name}.checksums")
    expected_checksums = _verify_checksum_map(checksums["expected"], f"datasets.{name}.checksums.expected")
    actual_checksums = _verify_checksum_map(checksums["actual"], f"datasets.{name}.checksums.actual")
    if set(expected_checksums) != set(actual_checksums) or expected_checksums != actual_checksums:
        raise RestoreEvidenceError(f"datasets.{name} checksums do not match")
    integrity = _exact_object(
        dataset["referential_integrity"], _REFERENTIAL_FIELDS, f"datasets.{name}.referential_integrity"
    )
    if integrity["checked"] is not True:
        raise RestoreEvidenceError(f"datasets.{name} referential integrity was not checked")
    violations = _non_negative_int(integrity["violations"], f"datasets.{name}.referential_integrity.violations")
    if violations != 0:
        raise RestoreEvidenceError(f"datasets.{name} referential integrity has violations")
    return {
        "row_counts": {"expected": expected_counts, "actual": actual_counts},
        "checksums": {"expected": expected_checksums, "actual": actual_checksums},
        "referential_integrity": {"checked": True, "violations": 0},
    }


def _verify_datasets(value: Any) -> dict[str, Any]:
    datasets = _exact_object(value, _DATASET_NAMES, "datasets")
    return {name: _verify_dataset(datasets[name], name) for name in sorted(_DATASET_NAMES)}


def _verify_controls(value: Any) -> dict[str, list[dict[str, Any]]]:
    controls = _exact_object(value, _CONTROL_NAMES, "negative_controls")
    result: dict[str, list[dict[str, Any]]] = {}
    for category in sorted(_CONTROL_NAMES):
        entries = controls[category]
        if not isinstance(entries, list) or not entries:
            raise RestoreEvidenceError(f"negative_controls.{category} must be a non-empty list")
        seen: set[str] = set()
        verified: list[dict[str, Any]] = []
        for index, raw in enumerate(entries):
            item = _exact_object(raw, _CONTROL_FIELDS, f"negative_controls.{category}[{index}]")
            control_id = _identifier(item["id"], f"negative_controls.{category}[{index}].id")
            if control_id in seen:
                raise RestoreEvidenceError(f"negative_controls.{category} has duplicate id")
            seen.add(control_id)
            if item["attempted"] is not True:
                raise RestoreEvidenceError(f"negative_controls.{category}.{control_id} was not attempted")
            if item["rejected"] is not True:
                raise RestoreEvidenceError(f"negative_controls.{category}.{control_id} was not rejected")
            observed_status = item["observed_status"]
            if not isinstance(observed_status, str) or observed_status not in _REJECTED_OUTCOMES:
                raise RestoreEvidenceError(
                    f"negative_controls.{category}.{control_id} has non-rejecting outcome"
                )
            verified.append(
                {
                    "id": control_id,
                    "attempted": True,
                    "rejected": True,
                    "observed_status": observed_status,
                }
            )
        marker = {
            "rls": ("rls", "collection"),
            "compartment": ("compartment",),
            "revocation": ("revoc", "revok"),
        }[category]
        if not any(any(token in item["id"].lower() for token in marker) for item in verified):
            raise RestoreEvidenceError(
                f"negative_controls.{category} lacks a category-specific control"
            )
        result[category] = verified
    return result


@dataclass(frozen=True, slots=True)
class VerifiedRestoreEvidence:
    """Read-only P1 evidence result; never a Gate 6 approval."""

    evidence_id: str
    report_sha256: str
    profile_id: str
    profile_version: str
    profile_content_sha256: str
    environment: str
    synthetic: bool
    restore_seconds: int
    rpo_seconds_observed: int
    rto_limit_seconds: int
    rpo_limit_seconds: int
    datasets: Mapping[str, Any]
    negative_controls: Mapping[str, Any]

    @property
    def status(self) -> str:
        return RESTORE_EVIDENCE_STATUS

    @property
    def acceptance_status(self) -> str:
        return RESTORE_ACCEPTANCE_STATUS

    @property
    def gate6_pass(self) -> bool:
        return False

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-safe, explicit non-acceptance projection."""

        return {
            "schema_version": P1_RESTORE_EVIDENCE_SCHEMA,
            "status": RESTORE_EVIDENCE_STATUS,
            "acceptance_status": RESTORE_ACCEPTANCE_STATUS,
            "gate6_pass": False,
            "production_approval": False,
            "evidence_class": "non-production-restore-evidence",
            "evidence_id": self.evidence_id,
            "report_sha256": self.report_sha256,
            "environment": self.environment,
            "synthetic": self.synthetic,
            "profile_binding": {
                "profile_id": self.profile_id,
                "profile_version": self.profile_version,
                "profile_content_sha256": self.profile_content_sha256,
            },
            "rto_rpo": {
                "restore_seconds": self.restore_seconds,
                "rto_limit_seconds": self.rto_limit_seconds,
                "rpo_seconds_observed": self.rpo_seconds_observed,
                "rpo_limit_seconds": self.rpo_limit_seconds,
            },
            "datasets": dict(self.datasets),
            "negative_controls": dict(self.negative_controls),
        }


def verify_restore_evidence(
    report: Mapping[str, Any],
    verified_profile: VerifiedProductionAcceptanceProfile,
    *,
    now: datetime | None = None,
) -> VerifiedRestoreEvidence:
    """Verify a complete restore report against an already verified P0 profile.

    The function is pure/read-only.  It raises :class:`RestoreEvidenceError`
    for every missing, malformed, mismatched, or non-rejecting assertion.
    """

    if not isinstance(report, Mapping):
        raise RestoreEvidenceError("restore report must be an object")
    # Check the authority type before parsing report timestamps.  A caller
    # cannot smuggle a self-described mapping past the P0 boundary, even when
    # the report itself is malformed or uses a future synthetic timestamp.
    if not isinstance(verified_profile, VerifiedProductionAcceptanceProfile):
        raise RestoreEvidenceError(
            "P0 authority must be VerifiedProductionAcceptanceProfile from the signed verifier"
        )
    if set(report) != _REPORT_FIELDS:
        raise RestoreEvidenceError("restore report has unknown or missing fields")
    if report["schema_version"] != P1_RESTORE_EVIDENCE_SCHEMA:
        raise RestoreEvidenceError("unknown restore evidence schema")
    evidence_id = _identifier(report["evidence_id"], "evidence_id")
    if report["status"] != "pass":
        raise RestoreEvidenceError("restore report status must be pass")
    generated_at = _parse_rfc3339(report["generated_at"], "generated_at")
    current = _normalise_now(now)
    if generated_at > current:
        raise RestoreEvidenceError("generated_at cannot be in the future")
    environment = report["environment"]
    if environment not in _ENVIRONMENTS:
        raise RestoreEvidenceError("environment must be synthetic, non-production, or production-equivalent")
    synthetic = report["synthetic"]
    if not isinstance(synthetic, bool):
        raise RestoreEvidenceError("synthetic must be boolean")
    if environment == "synthetic" and synthetic is not True:
        raise RestoreEvidenceError("synthetic environment must set synthetic=true")
    if environment == "production-equivalent" and synthetic is True:
        raise RestoreEvidenceError("production-equivalent evidence cannot be marked synthetic")
    profile_binding = _verify_profile_binding(report["profile_binding"])
    _, restore_started_at, restore_completed_at, duration, rpo_observed = _verify_restore_timing(report["restore"])
    if generated_at < restore_completed_at:
        raise RestoreEvidenceError("generated_at must follow restore completion")
    rto_limit, rpo_limit = _verify_p0_authority(
        verified_profile,
        profile_binding=profile_binding,
        restore_started_at=restore_started_at,
        restore_completed_at=restore_completed_at,
    )
    if duration > rto_limit:
        raise RestoreEvidenceError(
            f"restore duration {duration}s exceeds P0 RTO {rto_limit}s"
        )
    if rpo_observed > rpo_limit:
        raise RestoreEvidenceError(
            f"observed RPO {rpo_observed}s exceeds P0 RPO {rpo_limit}s"
        )
    datasets = _verify_datasets(report["datasets"])
    negative_controls = _verify_controls(report["negative_controls"])
    report_hash = hashlib.sha256(canonical_json(report)).hexdigest()
    return VerifiedRestoreEvidence(
        evidence_id=evidence_id,
        report_sha256=report_hash,
        profile_id=verified_profile.profile_id,
        profile_version=verified_profile.profile_version,
        profile_content_sha256=verified_profile.profile_content_sha256,
        environment=environment,
        synthetic=synthetic,
        restore_seconds=duration,
        rpo_seconds_observed=rpo_observed,
        rto_limit_seconds=rto_limit,
        rpo_limit_seconds=rpo_limit,
        datasets=MappingProxyType(datasets),
        negative_controls=MappingProxyType(negative_controls),
    )


def verify_restore_report(
    report: Mapping[str, Any],
    verified_profile: VerifiedProductionAcceptanceProfile,
    *,
    now: datetime | None = None,
) -> VerifiedRestoreEvidence:
    """Compatibility spelling for callers that use ``restore_report``."""

    return verify_restore_evidence(report, verified_profile, now=now)


def verify_restore_report_file(
    report_path: str | Path,
    verified_profile: VerifiedProductionAcceptanceProfile,
    *,
    now: datetime | None = None,
) -> VerifiedRestoreEvidence:
    """Read and verify one report file without writing a result file."""

    return verify_restore_evidence(read_restore_report(report_path), verified_profile, now=now)


def _parse_cli_time(value: str) -> datetime:
    return _parse_rfc3339(value, "--now")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--p0-profile", type=Path, required=True)
    parser.add_argument("--p0-trust-store", type=Path, required=True)
    parser.add_argument("--p0-inventory", type=Path)
    parser.add_argument("--now", type=_parse_cli_time)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        authority = verify_signed_production_acceptance_profile(
            args.p0_profile,
            args.p0_trust_store,
            inventory_path=args.p0_inventory,
            now=args.now,
        )
        evidence = verify_restore_report_file(args.report, authority, now=args.now)
    except Exception as exc:  # noqa: BLE001 - CLI is a fail-closed boundary
        print(f"FAIL: restore evidence verification: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(evidence.as_dict(), ensure_ascii=False, sort_keys=True))
    return 0


__all__ = [
    "P1_RESTORE_EVIDENCE_SCHEMA",
    "RESTORE_REPORT_SCHEMA",
    "RESTORE_EVIDENCE_STATUS",
    "RESTORE_ACCEPTANCE_STATUS",
    "RestoreEvidenceError",
    "VerifiedRestoreEvidence",
    "read_restore_report",
    "verify_restore_evidence",
    "verify_restore_report",
    "verify_restore_report_file",
    "build_parser",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
