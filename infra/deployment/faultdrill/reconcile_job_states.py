#!/usr/bin/env python3
"""Deterministic Gate 6 P2 ingestion-job snapshots and reconciliation.

This module is engineering tooling only.  It does not inject a fault, claim a
production drill ran, or create Gate 6 acceptance evidence.  A caller may use
``capture_job_snapshot`` with a database connection before and after a real
operator-run drill; the strict functions then reject malformed, duplicated,
lost, or non-terminal accepted jobs.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


JOB_SNAPSHOT_SCHEMA = "anila.gate6.p2.job-snapshot.v1"
JOB_RECONCILIATION_SCHEMA = "anila.gate6.p2.job-reconciliation.v1"
TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled", "dead_letter"})
JOB_STATUSES = frozenset(
    {
        "dispatch_pending",
        "queued",
        "running",
        "retry_wait",
        *TERMINAL_STATUSES,
    }
)
JOB_SNAPSHOT_SQL = """
SELECT
  id AS job_id,
  status,
  attempt_count,
  lease_token,
  dead_lettered_at
FROM ingestion_jobs
ORDER BY id
""".strip()

_SNAPSHOT_FIELDS = frozenset({"schema_version", "snapshot_id", "captured_at", "jobs"})
_JOB_FIELDS = frozenset(
    {"job_id", "status", "attempt_count", "lease_token", "dead_lettered_at"}
)
_SNAPSHOT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")


class JobSnapshotError(ValueError):
    """Raised when a snapshot or transition is incomplete or inconsistent."""


def canonical_json(value: Any) -> bytes:
    """Return the deterministic JSON representation used for snapshot hashes."""

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise JobSnapshotError("snapshot value is not canonical JSON") from exc


def _parse_rfc3339(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise JobSnapshotError(f"{field} must be RFC3339 with timezone")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise JobSnapshotError(f"{field} must be RFC3339 with timezone") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise JobSnapshotError(f"{field} must include timezone")
    return parsed.astimezone(timezone.utc)


def _normalise_time(value: datetime | str, field: str) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise JobSnapshotError(f"{field} must include timezone")
        parsed = value.astimezone(timezone.utc)
    else:
        parsed = _parse_rfc3339(value, field)
    return parsed.isoformat()


def _exact_mapping(value: Any, fields: frozenset[str], field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise JobSnapshotError(f"{field} has unknown or missing fields")
    return value


def _job_row(value: Any, field: str) -> dict[str, Any]:
    row = _exact_mapping(value, _JOB_FIELDS, field)
    job_id = row["job_id"]
    if isinstance(job_id, bool) or not isinstance(job_id, int) or job_id < 1:
        raise JobSnapshotError(f"{field}.job_id must be a positive integer")
    status = row["status"]
    if status not in JOB_STATUSES:
        raise JobSnapshotError(f"{field}.status is invalid")
    attempt_count = row["attempt_count"]
    if (
        isinstance(attempt_count, bool)
        or not isinstance(attempt_count, int)
        or attempt_count < 0
    ):
        raise JobSnapshotError(f"{field}.attempt_count must be a non-negative integer")
    lease_token = row["lease_token"]
    if lease_token is not None and (
        not isinstance(lease_token, str)
        or not lease_token
        or lease_token != lease_token.strip()
        or len(lease_token) > 256
    ):
        raise JobSnapshotError(f"{field}.lease_token must be null or trimmed text")
    if status == "running" and lease_token is None:
        raise JobSnapshotError(f"{field}.running job must carry a lease_token")
    if status != "running" and lease_token is not None:
        raise JobSnapshotError(f"{field}.non-running job must not carry a lease_token")
    dead_lettered_at = row["dead_lettered_at"]
    if dead_lettered_at is not None:
        dead_lettered_at = _normalise_time(
            dead_lettered_at, f"{field}.dead_lettered_at"
        )
    if status == "dead_letter" and dead_lettered_at is None:
        raise JobSnapshotError(f"{field}.dead_letter job must carry dead_lettered_at")
    if status != "dead_letter" and dead_lettered_at is not None:
        raise JobSnapshotError(
            f"{field}.dead_lettered_at is only valid for dead_letter"
        )
    return {
        "job_id": job_id,
        "status": status,
        "attempt_count": attempt_count,
        "lease_token": lease_token,
        "dead_lettered_at": dead_lettered_at,
    }


def build_job_snapshot(
    rows: Sequence[Mapping[str, Any]],
    *,
    snapshot_id: str,
    captured_at: datetime | str,
) -> dict[str, Any]:
    """Validate and sort all supplied ingestion-job rows into one snapshot."""

    if not isinstance(snapshot_id, str) or not _SNAPSHOT_ID_RE.fullmatch(snapshot_id):
        raise JobSnapshotError("snapshot_id must be an identifier")
    if isinstance(rows, (str, bytes, bytearray)) or not isinstance(rows, Sequence):
        raise JobSnapshotError("rows must be a sequence")
    captured = _normalise_time(captured_at, "captured_at")
    jobs = [_job_row(row, f"jobs[{index}]") for index, row in enumerate(rows)]
    jobs.sort(key=lambda item: item["job_id"])
    ids = [item["job_id"] for item in jobs]
    if len(ids) != len(set(ids)):
        raise JobSnapshotError("snapshot contains duplicate job_id")
    captured_time = _parse_rfc3339(captured, "captured_at")
    for item in jobs:
        if (
            item["dead_lettered_at"] is not None
            and _parse_rfc3339(item["dead_lettered_at"], "jobs[].dead_lettered_at")
            > captured_time
        ):
            raise JobSnapshotError("dead_lettered_at cannot postdate snapshot capture")
    return {
        "schema_version": JOB_SNAPSHOT_SCHEMA,
        "snapshot_id": snapshot_id,
        "captured_at": captured,
        "jobs": jobs,
    }


def verify_job_snapshot(value: Any) -> dict[str, Any]:
    """Fail closed on an externally supplied job snapshot."""

    snapshot = _exact_mapping(value, _SNAPSHOT_FIELDS, "snapshot")
    if snapshot["schema_version"] != JOB_SNAPSHOT_SCHEMA:
        raise JobSnapshotError("unknown job snapshot schema")
    jobs = snapshot["jobs"]
    if not isinstance(jobs, list):
        raise JobSnapshotError("snapshot.jobs must be a list")
    return build_job_snapshot(
        jobs,
        snapshot_id=snapshot["snapshot_id"],
        captured_at=snapshot["captured_at"],
    )


def snapshot_sha256(snapshot: Mapping[str, Any]) -> str:
    """Hash a fully revalidated snapshot."""

    return hashlib.sha256(canonical_json(verify_job_snapshot(snapshot))).hexdigest()


async def capture_job_snapshot(
    connection: Any,
    *,
    snapshot_id: str,
    captured_at: datetime | str,
) -> dict[str, Any]:
    """Read every ingestion job through one ordered query and build a snapshot."""

    rows = await connection.fetch(JOB_SNAPSHOT_SQL)
    return build_job_snapshot(
        [dict(row) for row in rows],
        snapshot_id=snapshot_id,
        captured_at=captured_at,
    )


def write_job_snapshot(path: str | Path, snapshot: Mapping[str, Any]) -> None:
    """Write canonical, revalidated snapshot bytes to an explicit evidence path."""

    output = Path(path)
    with output.open("xb") as stream:
        stream.write(canonical_json(verify_job_snapshot(snapshot)) + b"\n")


@dataclass(frozen=True, slots=True)
class JobReconciliation:
    """Deterministic before/after result; clean does not mean P2 acceptance."""

    before_snapshot_sha256: str
    after_snapshot_sha256: str
    accepted_job_id_watermark: int
    accepted_job_count: int
    terminal_job_count: int
    lost_job_ids: tuple[int, ...]
    nonterminal_job_ids: tuple[int, ...]

    @property
    def clean(self) -> bool:
        return not self.lost_job_ids and not self.nonterminal_job_ids

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": JOB_RECONCILIATION_SCHEMA,
            "status": "reconciled" if self.clean else "failed",
            "gate6_pass": False,
            "before_snapshot_sha256": self.before_snapshot_sha256,
            "after_snapshot_sha256": self.after_snapshot_sha256,
            "accepted_job_id_watermark": self.accepted_job_id_watermark,
            "accepted_job_count": self.accepted_job_count,
            "terminal_job_count": self.terminal_job_count,
            "lost_job_ids": list(self.lost_job_ids),
            "nonterminal_job_ids": list(self.nonterminal_job_ids),
        }


def reconcile_job_snapshots(
    before_value: Mapping[str, Any],
    after_value: Mapping[str, Any],
    *,
    accepted_job_id_watermark: int,
) -> JobReconciliation:
    """Require every job accepted at the explicit ID watermark to terminate.

    The watermark must equal the largest ID in the complete before snapshot.
    Rows first seen in the after snapshot are accepted when their ID is at or
    below that boundary; larger IDs are unambiguously post-cutoff background
    traffic and do not affect this drill population.
    """

    before = verify_job_snapshot(before_value)
    after = verify_job_snapshot(after_value)
    before_time = _parse_rfc3339(before["captured_at"], "before.captured_at")
    after_time = _parse_rfc3339(after["captured_at"], "after.captured_at")
    if after_time <= before_time:
        raise JobSnapshotError("after snapshot must be captured after before snapshot")
    before_by_id = {row["job_id"]: row for row in before["jobs"]}
    after_by_id = {row["job_id"]: row for row in after["jobs"]}
    if not before_by_id:
        raise JobSnapshotError("before snapshot must contain at least one accepted job")
    if (
        isinstance(accepted_job_id_watermark, bool)
        or not isinstance(accepted_job_id_watermark, int)
        or accepted_job_id_watermark < 1
    ):
        raise JobSnapshotError("accepted_job_id_watermark must be a positive integer")
    if accepted_job_id_watermark != max(before_by_id):
        raise JobSnapshotError(
            "accepted_job_id_watermark must equal the largest before-snapshot job_id"
        )
    accepted_ids = {
        job_id
        for job_id in set(before_by_id) | set(after_by_id)
        if job_id <= accepted_job_id_watermark
    }
    lost = tuple(sorted(accepted_ids - set(after_by_id)))
    nonterminal: list[int] = []
    terminal_count = 0
    for job_id in sorted(accepted_ids):
        previous = before_by_id.get(job_id)
        current = after_by_id.get(job_id)
        if current is None:
            continue
        if (
            previous is not None
            and current["attempt_count"] < previous["attempt_count"]
        ):
            raise JobSnapshotError(f"job {job_id} attempt_count regressed")
        if (
            previous is not None
            and previous["status"] in TERMINAL_STATUSES
            and current["status"] != previous["status"]
        ):
            raise JobSnapshotError(f"terminal job {job_id} changed status")
        if current["status"] not in TERMINAL_STATUSES:
            nonterminal.append(job_id)
        else:
            terminal_count += 1
    return JobReconciliation(
        before_snapshot_sha256=snapshot_sha256(before),
        after_snapshot_sha256=snapshot_sha256(after),
        accepted_job_id_watermark=accepted_job_id_watermark,
        accepted_job_count=len(accepted_ids),
        terminal_job_count=terminal_count,
        lost_job_ids=lost,
        nonterminal_job_ids=tuple(sorted(nonterminal)),
    )


__all__ = [
    "JOB_RECONCILIATION_SCHEMA",
    "JOB_SNAPSHOT_SCHEMA",
    "JOB_SNAPSHOT_SQL",
    "JOB_STATUSES",
    "TERMINAL_STATUSES",
    "JobReconciliation",
    "JobSnapshotError",
    "build_job_snapshot",
    "capture_job_snapshot",
    "reconcile_job_snapshots",
    "snapshot_sha256",
    "verify_job_snapshot",
    "write_job_snapshot",
]
