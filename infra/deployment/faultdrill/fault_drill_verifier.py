#!/usr/bin/env python3
"""Fail-closed Gate 6 P2 fault-drill evidence verifier.

The verifier consumes an already captured report and a cryptographically
verified P0 authority.  It never injects faults or creates approval.  Even a
complete report returns non-acceptance engineering evidence with
``gate6_pass=False``; real execution, named-owner attestation, and five-party
Gate 6 approval remain external obligations.
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

try:  # pragma: no cover - package spelling depends on invocation mode
    from .reconcile_job_states import (
        JobSnapshotError,
        reconcile_job_snapshots,
        verify_job_snapshot,
    )
except ImportError:  # pragma: no cover - direct script invocation
    from reconcile_job_states import (  # type: ignore[no-redef]
        JobSnapshotError,
        reconcile_job_snapshots,
        verify_job_snapshot,
    )


P2_FAULT_EVIDENCE_SCHEMA = "anila.gate6.p2.fault-evidence.v1"
FAULT_EVIDENCE_STATUS = "VERIFIED_NON_ACCEPTANCE"
FAULT_ACCEPTANCE_STATUS = "NOT_ACCEPTANCE"
REQUIRED_FAULT_TARGETS = frozenset(
    {"redis", "worker", "csp", "disk-full", "network-partition"}
)

_REPORT_FIELDS = frozenset(
    {
        "schema_version",
        "evidence_id",
        "status",
        "generated_at",
        "environment",
        "synthetic",
        "profile_binding",
        "drill",
        "before_snapshot",
        "after_snapshot",
        "timeline",
        "negative_controls",
    }
)
_PROFILE_BINDING_FIELDS = frozenset(
    {"profile_id", "profile_version", "profile_content_sha256"}
)
_DRILL_FIELDS = frozenset(
    {
        "started_at",
        "recovered_at",
        "rto_seconds_observed",
        "rpo_seconds_observed",
        "accepted_job_id_watermark",
        "fault_targets",
    }
)
_TIMELINE_FIELDS = frozenset(
    {"sequence", "observed_at", "event_type", "target", "outcome"}
)
_CONTROL_FIELDS = frozenset({"id", "attempted", "bypass_observed", "observed_status"})
_CONTROL_CATEGORIES = frozenset({"classification", "authorization"})
_REJECTING_OUTCOMES = frozenset(
    {"blocked", "denied", "fail_closed", "forbidden", "rejected", "unauthorized"}
)
_ENVIRONMENTS = frozenset({"synthetic", "non-production", "production-equivalent"})
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class FaultDrillEvidenceError(ValueError):
    """Raised for incomplete, contradictory, or unauthorized P2 evidence."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise FaultDrillEvidenceError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def read_fault_drill_report(path: str | Path) -> dict[str, Any]:
    report_path = Path(path)
    try:
        raw = report_path.read_bytes()
    except OSError as exc:
        raise FaultDrillEvidenceError(
            f"cannot read fault-drill report: {report_path}"
        ) from exc
    if raw.startswith(b"\xef\xbb\xbf"):
        raise FaultDrillEvidenceError("UTF-8 BOM is forbidden in fault-drill report")
    try:
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FaultDrillEvidenceError(
            f"invalid fault-drill report JSON: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise FaultDrillEvidenceError("fault-drill report root must be an object")
    return value


def _exact(value: Any, fields: frozenset[str], field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise FaultDrillEvidenceError(f"{field} has unknown or missing fields")
    return value


def _identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
        raise FaultDrillEvidenceError(f"{field} must be an identifier")
    return value


def _parse_time(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise FaultDrillEvidenceError(f"{field} must be RFC3339 with timezone")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FaultDrillEvidenceError(f"{field} must be RFC3339 with timezone") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise FaultDrillEvidenceError(f"{field} must include timezone")
    return parsed.astimezone(timezone.utc)


def _normalise_now(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise FaultDrillEvidenceError("now must include timezone")
    return current.astimezone(timezone.utc)


def _non_negative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise FaultDrillEvidenceError(f"{field} must be a non-negative integer")
    return value


def _profile_binding(value: Any) -> Mapping[str, Any]:
    binding = _exact(value, _PROFILE_BINDING_FIELDS, "profile_binding")
    _identifier(binding["profile_id"], "profile_binding.profile_id")
    if (
        not isinstance(binding["profile_version"], str)
        or not binding["profile_version"].strip()
        or binding["profile_version"] != binding["profile_version"].strip()
    ):
        raise FaultDrillEvidenceError("profile_binding.profile_version is invalid")
    digest = binding["profile_content_sha256"]
    if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
        raise FaultDrillEvidenceError(
            "profile_binding.profile_content_sha256 is invalid"
        )
    return binding


def _verify_p0(
    authority: VerifiedProductionAcceptanceProfile,
    *,
    binding: Mapping[str, Any],
    started_at: datetime,
    recovered_at: datetime,
) -> tuple[int, int]:
    if not isinstance(authority, VerifiedProductionAcceptanceProfile):
        raise FaultDrillEvidenceError(
            "P0 authority must be VerifiedProductionAcceptanceProfile from the signed verifier"
        )
    profile = authority.profile
    if not isinstance(profile, Mapping):
        raise FaultDrillEvidenceError("verified P0 authority lacks profile content")
    try:
        validate_production_acceptance_profile(profile, now=started_at)
        digest = production_profile_content_sha256(profile)
    except Exception as exc:  # noqa: BLE001 - trust boundary fails closed
        raise FaultDrillEvidenceError(
            f"verified P0 authority is invalid: {exc}"
        ) from exc
    if digest != authority.profile_content_sha256:
        raise FaultDrillEvidenceError("verified P0 authority content hash mismatch")
    expected = {
        "profile_id": authority.profile_id,
        "profile_version": authority.profile_version,
        "profile_content_sha256": authority.profile_content_sha256,
    }
    if dict(binding) != expected:
        raise FaultDrillEvidenceError(
            "fault-drill report is bound to a different P0 profile"
        )
    if not authority.valid_from <= started_at < recovered_at <= authority.valid_until:
        raise FaultDrillEvidenceError("fault drill is outside the P0 validity window")
    limits = profile.get("rto_rpo")
    if not isinstance(limits, Mapping) or set(limits) != {"rto_seconds", "rpo_seconds"}:
        raise FaultDrillEvidenceError("verified P0 authority lacks exact rto_rpo")
    rto, rpo = limits["rto_seconds"], limits["rpo_seconds"]
    if isinstance(rto, bool) or not isinstance(rto, int) or rto < 1:
        raise FaultDrillEvidenceError("verified P0 rto_seconds is invalid")
    if isinstance(rpo, bool) or not isinstance(rpo, int) or rpo < 1:
        raise FaultDrillEvidenceError("verified P0 rpo_seconds is invalid")
    return rto, rpo


def _drill(value: Any) -> tuple[dict[str, Any], datetime, datetime, int, int, int]:
    drill = _exact(value, _DRILL_FIELDS, "drill")
    started = _parse_time(drill["started_at"], "drill.started_at")
    recovered = _parse_time(drill["recovered_at"], "drill.recovered_at")
    if recovered <= started:
        raise FaultDrillEvidenceError("drill.recovered_at must follow drill.started_at")
    observed_rto = _non_negative_int(
        drill["rto_seconds_observed"], "drill.rto_seconds_observed"
    )
    if (recovered - started).total_seconds() != observed_rto:
        raise FaultDrillEvidenceError(
            "drill.rto_seconds_observed must equal recovered_at - started_at"
        )
    observed_rpo = _non_negative_int(
        drill["rpo_seconds_observed"], "drill.rpo_seconds_observed"
    )
    accepted_job_id_watermark = _non_negative_int(
        drill["accepted_job_id_watermark"], "drill.accepted_job_id_watermark"
    )
    if accepted_job_id_watermark < 1:
        raise FaultDrillEvidenceError(
            "drill.accepted_job_id_watermark must be a positive integer"
        )
    targets = drill["fault_targets"]
    if (
        not isinstance(targets, list)
        or any(not isinstance(target, str) for target in targets)
        or len(targets) != len(set(targets))
        or set(targets) != REQUIRED_FAULT_TARGETS
    ):
        raise FaultDrillEvidenceError(
            "drill.fault_targets must cover each required P2 fault"
        )
    return (
        dict(drill),
        started,
        recovered,
        observed_rto,
        observed_rpo,
        accepted_job_id_watermark,
    )


def _timeline(
    value: Any, *, started_at: datetime, recovered_at: datetime
) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, list) or not value:
        raise FaultDrillEvidenceError("timeline must be a non-empty list")
    verified: list[Mapping[str, Any]] = []
    target_events: dict[str, dict[str, datetime]] = {
        target: {} for target in REQUIRED_FAULT_TARGETS
    }
    previous_time: datetime | None = None
    for index, raw in enumerate(value):
        item = _exact(raw, _TIMELINE_FIELDS, f"timeline[{index}]")
        if (
            isinstance(item["sequence"], bool)
            or not isinstance(item["sequence"], int)
            or item["sequence"] != index + 1
        ):
            raise FaultDrillEvidenceError(
                "timeline sequence must be contiguous from one"
            )
        observed = _parse_time(item["observed_at"], f"timeline[{index}].observed_at")
        if not started_at <= observed <= recovered_at:
            raise FaultDrillEvidenceError(
                "timeline event falls outside the drill window"
            )
        if previous_time is not None and observed < previous_time:
            raise FaultDrillEvidenceError("timeline events must be chronological")
        previous_time = observed
        event_type = _identifier(item["event_type"], f"timeline[{index}].event_type")
        target = item["target"]
        if not isinstance(target, str) or target not in REQUIRED_FAULT_TARGETS:
            raise FaultDrillEvidenceError(f"timeline[{index}].target is invalid")
        outcome = _identifier(item["outcome"], f"timeline[{index}].outcome")
        expected_outcome = {
            "fault_injected": "injected",
            "fault_recovered": "recovered",
        }.get(event_type)
        if expected_outcome is None or outcome != expected_outcome:
            raise FaultDrillEvidenceError(
                f"timeline[{index}] must prove an injected or recovered fault outcome"
            )
        if event_type in target_events[target]:
            raise FaultDrillEvidenceError(
                f"timeline has duplicate {event_type} event for {target}"
            )
        target_events[target][event_type] = observed
        verified.append(MappingProxyType(dict(item)))
    for target, events in target_events.items():
        if set(events) != {"fault_injected", "fault_recovered"}:
            raise FaultDrillEvidenceError(
                f"timeline must include injected and recovered events for {target}"
            )
        if events["fault_recovered"] <= events["fault_injected"]:
            raise FaultDrillEvidenceError(
                f"timeline recovery must follow fault injection for {target}"
            )
    return tuple(verified)


def _negative_controls(value: Any) -> Mapping[str, tuple[Mapping[str, Any], ...]]:
    controls = _exact(value, _CONTROL_CATEGORIES, "negative_controls")
    verified: dict[str, tuple[Mapping[str, Any], ...]] = {}
    for category in sorted(_CONTROL_CATEGORIES):
        entries = controls[category]
        if not isinstance(entries, list) or not entries:
            raise FaultDrillEvidenceError(
                f"negative_controls.{category} must be non-empty"
            )
        seen: set[str] = set()
        category_items: list[Mapping[str, Any]] = []
        for index, raw in enumerate(entries):
            item = _exact(
                raw, _CONTROL_FIELDS, f"negative_controls.{category}[{index}]"
            )
            control_id = _identifier(
                item["id"], f"negative_controls.{category}[{index}].id"
            )
            if control_id in seen:
                raise FaultDrillEvidenceError(
                    f"negative_controls.{category} has duplicate id"
                )
            seen.add(control_id)
            if item["attempted"] is not True or item["bypass_observed"] is not False:
                raise FaultDrillEvidenceError(
                    f"negative_controls.{category}.{control_id} did not prove fail-closed"
                )
            if item["observed_status"] not in _REJECTING_OUTCOMES:
                raise FaultDrillEvidenceError(
                    f"negative_controls.{category}.{control_id} has non-rejecting outcome"
                )
            category_items.append(MappingProxyType(dict(item)))
        verified[category] = tuple(category_items)
    return MappingProxyType(verified)


@dataclass(frozen=True, slots=True)
class VerifiedFaultDrillEvidence:
    evidence_id: str
    report_sha256: str
    profile_id: str
    profile_version: str
    profile_content_sha256: str
    environment: str
    synthetic: bool
    rto_seconds_observed: int
    rpo_seconds_observed: int
    rto_limit_seconds: int
    rpo_limit_seconds: int
    reconciliation: Mapping[str, Any]

    @property
    def status(self) -> str:
        return FAULT_EVIDENCE_STATUS

    @property
    def acceptance_status(self) -> str:
        return FAULT_ACCEPTANCE_STATUS

    @property
    def gate6_pass(self) -> bool:
        return False

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": P2_FAULT_EVIDENCE_SCHEMA,
            "status": FAULT_EVIDENCE_STATUS,
            "acceptance_status": FAULT_ACCEPTANCE_STATUS,
            "gate6_pass": False,
            "production_approval": False,
            "evidence_class": "non-acceptance-fault-drill-evidence",
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
                "rto_seconds_observed": self.rto_seconds_observed,
                "rto_limit_seconds": self.rto_limit_seconds,
                "rpo_seconds_observed": self.rpo_seconds_observed,
                "rpo_limit_seconds": self.rpo_limit_seconds,
            },
            "reconciliation": dict(self.reconciliation),
        }


def _verify_fault_drill_evidence(
    report: Mapping[str, Any],
    verified_profile: VerifiedProductionAcceptanceProfile,
    *,
    now: datetime | None = None,
) -> VerifiedFaultDrillEvidence:
    """Verify strict P2 evidence without producing an acceptance decision."""

    if not isinstance(report, Mapping) or set(report) != _REPORT_FIELDS:
        raise FaultDrillEvidenceError(
            "fault-drill report has unknown or missing fields"
        )
    if not isinstance(verified_profile, VerifiedProductionAcceptanceProfile):
        raise FaultDrillEvidenceError(
            "P0 authority must be VerifiedProductionAcceptanceProfile from the signed verifier"
        )
    if report["schema_version"] != P2_FAULT_EVIDENCE_SCHEMA:
        raise FaultDrillEvidenceError("unknown fault-drill evidence schema")
    evidence_id = _identifier(report["evidence_id"], "evidence_id")
    if report["status"] != "complete":
        raise FaultDrillEvidenceError("fault-drill report status must be complete")
    generated_at = _parse_time(report["generated_at"], "generated_at")
    if generated_at > _normalise_now(now):
        raise FaultDrillEvidenceError("generated_at cannot be in the future")
    environment = report["environment"]
    if environment not in _ENVIRONMENTS:
        raise FaultDrillEvidenceError("environment is invalid")
    synthetic = report["synthetic"]
    if not isinstance(synthetic, bool):
        raise FaultDrillEvidenceError("synthetic must be boolean")
    if (environment == "synthetic") != synthetic:
        raise FaultDrillEvidenceError("environment and synthetic flag are inconsistent")
    binding = _profile_binding(report["profile_binding"])
    (
        _,
        started,
        recovered,
        observed_rto,
        observed_rpo,
        accepted_job_id_watermark,
    ) = _drill(report["drill"])
    if generated_at < recovered:
        raise FaultDrillEvidenceError("generated_at must follow drill recovery")
    rto_limit, rpo_limit = _verify_p0(
        verified_profile,
        binding=binding,
        started_at=started,
        recovered_at=recovered,
    )
    if observed_rto > rto_limit:
        raise FaultDrillEvidenceError(
            f"observed RTO {observed_rto}s exceeds P0 RTO {rto_limit}s"
        )
    try:
        before = verify_job_snapshot(report["before_snapshot"])
        after = verify_job_snapshot(report["after_snapshot"])
        reconciliation = reconcile_job_snapshots(
            before,
            after,
            accepted_job_id_watermark=accepted_job_id_watermark,
        )
    except JobSnapshotError as exc:
        raise FaultDrillEvidenceError(f"job reconciliation failed: {exc}") from exc
    before_captured = _parse_time(before["captured_at"], "before_snapshot.captured_at")
    after_captured = _parse_time(after["captured_at"], "after_snapshot.captured_at")
    if before_captured > started:
        raise FaultDrillEvidenceError("before snapshot must precede drill start")
    evidence_rpo_seconds = (started - before_captured).total_seconds()
    if evidence_rpo_seconds != observed_rpo:
        raise FaultDrillEvidenceError(
            "drill.rpo_seconds_observed must equal drill.started_at - "
            "before_snapshot.captured_at"
        )
    if observed_rpo > rpo_limit:
        raise FaultDrillEvidenceError(
            f"observed RPO {observed_rpo}s exceeds P0 RPO {rpo_limit}s"
        )
    if after_captured < recovered:
        raise FaultDrillEvidenceError("after snapshot must follow drill recovery")
    if after_captured > generated_at:
        raise FaultDrillEvidenceError(
            "after snapshot cannot postdate report generation"
        )
    if not reconciliation.clean:
        raise FaultDrillEvidenceError(
            "accepted jobs must have exactly one terminal after-state without loss"
        )
    _timeline(report["timeline"], started_at=started, recovered_at=recovered)
    _negative_controls(report["negative_controls"])
    report_hash = hashlib.sha256(canonical_json(report)).hexdigest()
    return VerifiedFaultDrillEvidence(
        evidence_id=evidence_id,
        report_sha256=report_hash,
        profile_id=verified_profile.profile_id,
        profile_version=verified_profile.profile_version,
        profile_content_sha256=verified_profile.profile_content_sha256,
        environment=environment,
        synthetic=synthetic,
        rto_seconds_observed=observed_rto,
        rpo_seconds_observed=observed_rpo,
        rto_limit_seconds=rto_limit,
        rpo_limit_seconds=rpo_limit,
        reconciliation=MappingProxyType(reconciliation.as_dict()),
    )


def verify_fault_drill_evidence(
    report: Mapping[str, Any],
    profile_path: str | Path,
    trust_store_path: str | Path,
    *,
    inventory_path: str | Path | None = None,
    now: datetime | None = None,
) -> VerifiedFaultDrillEvidence:
    """Verify P2 evidence against raw signed P0 inputs at the public boundary."""

    try:
        verified_profile = verify_signed_production_acceptance_profile(
            profile_path,
            trust_store_path,
            inventory_path=inventory_path,
            now=now,
        )
    except Exception as exc:  # noqa: BLE001 - public trust boundary fails closed
        raise FaultDrillEvidenceError(f"signed P0 verification failed: {exc}") from exc
    return _verify_fault_drill_evidence(report, verified_profile, now=now)


def verify_fault_drill_report_file(
    report_path: str | Path,
    profile_path: str | Path,
    trust_store_path: str | Path,
    *,
    inventory_path: str | Path | None = None,
    now: datetime | None = None,
) -> VerifiedFaultDrillEvidence:
    return verify_fault_drill_evidence(
        read_fault_drill_report(report_path),
        profile_path,
        trust_store_path,
        inventory_path=inventory_path,
        now=now,
    )


def _cli_time(value: str) -> datetime:
    return _parse_time(value, "--now")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--p0-profile", type=Path, required=True)
    parser.add_argument("--p0-trust-store", type=Path, required=True)
    parser.add_argument("--p0-inventory", type=Path)
    parser.add_argument("--now", type=_cli_time)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        evidence = verify_fault_drill_report_file(
            args.report,
            args.p0_profile,
            args.p0_trust_store,
            inventory_path=args.p0_inventory,
            now=args.now,
        )
    except Exception as exc:  # noqa: BLE001 - fail-closed CLI boundary
        print(f"FAIL: fault-drill evidence verification: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(evidence.as_dict(), ensure_ascii=False, sort_keys=True))
    return 0


__all__ = [
    "FAULT_ACCEPTANCE_STATUS",
    "FAULT_EVIDENCE_STATUS",
    "P2_FAULT_EVIDENCE_SCHEMA",
    "REQUIRED_FAULT_TARGETS",
    "FaultDrillEvidenceError",
    "VerifiedFaultDrillEvidence",
    "build_parser",
    "main",
    "read_fault_drill_report",
    "verify_fault_drill_evidence",
    "verify_fault_drill_report_file",
]


if __name__ == "__main__":
    raise SystemExit(main())
