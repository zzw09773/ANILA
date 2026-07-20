from __future__ import annotations

import copy
import re
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
FAULTDRILL_SRC = ROOT / "infra" / "deployment" / "faultdrill"
if str(FAULTDRILL_SRC) not in sys.path:
    sys.path.insert(0, str(FAULTDRILL_SRC))

from reconcile_job_states import (  # noqa: E402
    JOB_SNAPSHOT_SQL,
    JobSnapshotError,
    build_job_snapshot,
    capture_job_snapshot,
    reconcile_job_snapshots,
    snapshot_sha256,
    verify_job_snapshot,
    write_job_snapshot,
)


NOW = datetime(2026, 7, 17, 9, 0, tzinfo=timezone.utc)


def _row(
    job_id: int,
    status: str,
    *,
    attempts: int = 0,
    lease_token: str | None = None,
    dead_lettered_at: str | None = None,
) -> dict[str, object]:
    return {
        "job_id": job_id,
        "status": status,
        "attempt_count": attempts,
        "lease_token": lease_token,
        "dead_lettered_at": dead_lettered_at,
    }


def _snapshot(rows: list[dict[str, object]], suffix: str, offset: int) -> dict:
    return build_job_snapshot(
        rows,
        snapshot_id=f"fault.synthetic.{suffix}",
        captured_at=NOW + timedelta(seconds=offset),
    )


class JobSnapshotContractTests(unittest.TestCase):
    def test_snapshot_is_sorted_deterministic_and_writable(self) -> None:
        first = _snapshot(
            [_row(2, "running", attempts=1, lease_token="lease-2"), _row(1, "queued")],
            "before",
            0,
        )
        second = _snapshot(
            [_row(1, "queued"), _row(2, "running", attempts=1, lease_token="lease-2")],
            "before",
            0,
        )

        self.assertEqual(first, second)
        self.assertEqual([row["job_id"] for row in first["jobs"]], [1, 2])
        self.assertEqual(snapshot_sha256(first), snapshot_sha256(second))
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "snapshot.json"
            write_job_snapshot(output, first)
            self.assertTrue(output.read_bytes().endswith(b"\n"))
            with self.assertRaises(FileExistsError):
                write_job_snapshot(output, first)

    def test_duplicate_and_invalid_rows_fail_closed(self) -> None:
        invalid_cases = (
            [_row(1, "queued"), _row(1, "failed")],
            [_row(1, "unknown")],
            [_row(1, "running")],
            [_row(1, "queued", lease_token="orphan")],
            [_row(1, "dead_letter")],
            [_row(1, "failed", dead_lettered_at=NOW.isoformat())],
        )
        for rows in invalid_cases:
            with self.subTest(rows=rows), self.assertRaises(JobSnapshotError):
                _snapshot(rows, "invalid", 0)

    def test_external_snapshot_schema_and_mapping_mutations_have_exact_errors(
        self,
    ) -> None:
        canonical = _snapshot([_row(1, "queued")], "mapping", 0)
        mutations = []

        wrong_schema = copy.deepcopy(canonical)
        wrong_schema["schema_version"] = "anila.gate6.p2.wrong"
        mutations.append(("wrong schema", wrong_schema, "unknown job snapshot schema"))

        root_extra = copy.deepcopy(canonical)
        root_extra["extra"] = True
        mutations.append(
            ("root extra", root_extra, "snapshot has unknown or missing fields")
        )
        root_missing = copy.deepcopy(canonical)
        del root_missing["snapshot_id"]
        mutations.append(
            ("root missing", root_missing, "snapshot has unknown or missing fields")
        )

        row_extra = copy.deepcopy(canonical)
        row_extra["jobs"][0]["extra"] = True
        mutations.append(
            ("row extra", row_extra, "jobs[0] has unknown or missing fields")
        )
        row_missing = copy.deepcopy(canonical)
        del row_missing["jobs"][0]["status"]
        mutations.append(
            ("row missing", row_missing, "jobs[0] has unknown or missing fields")
        )

        for name, value, message in mutations:
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(JobSnapshotError, f"^{re.escape(message)}$"),
            ):
                verify_job_snapshot(value)

    def test_external_snapshot_timestamp_and_integer_mutations_have_exact_errors(
        self,
    ) -> None:
        canonical = _snapshot([_row(1, "queued")], "types", 0)
        mutations: list[tuple[str, dict, str]] = []

        non_rfc3339 = copy.deepcopy(canonical)
        non_rfc3339["captured_at"] = "ATTACKER-NOT-A-TIME"
        mutations.append(
            (
                "captured_at non-rfc3339",
                non_rfc3339,
                "captured_at must be RFC3339 with timezone",
            )
        )
        naive_time = copy.deepcopy(canonical)
        naive_time["captured_at"] = "2026-07-17T09:00:00"
        mutations.append(
            (
                "captured_at naive",
                naive_time,
                "captured_at must include timezone",
            )
        )

        for job_id in (True, 0, -1):
            invalid_job_id = copy.deepcopy(canonical)
            invalid_job_id["jobs"][0]["job_id"] = job_id
            mutations.append(
                (
                    f"job_id {job_id!r}",
                    invalid_job_id,
                    "jobs[0].job_id must be a positive integer",
                )
            )

        negative_attempts = copy.deepcopy(canonical)
        negative_attempts["jobs"][0]["attempt_count"] = -1
        mutations.append(
            (
                "negative attempt_count",
                negative_attempts,
                "jobs[0].attempt_count must be a non-negative integer",
            )
        )

        for name, value, message in mutations:
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(JobSnapshotError, f"^{re.escape(message)}$"),
            ):
                verify_job_snapshot(value)

    def test_snapshot_id_jobs_list_and_lease_token_have_exact_redacted_errors(
        self,
    ) -> None:
        canonical = _snapshot([_row(1, "queued")], "strict-fields", 0)

        for snapshot_id in (
            "",
            " hostile-snapshot-id ",
            "hostile@snapshot",
            "x" * 257,
            None,
        ):
            mutated = copy.deepcopy(canonical)
            mutated["snapshot_id"] = snapshot_id
            with self.subTest(field="snapshot_id", value=type(snapshot_id).__name__):
                with self.assertRaises(JobSnapshotError) as captured:
                    verify_job_snapshot(mutated)
                self.assertEqual(
                    str(captured.exception), "snapshot_id must be an identifier"
                )
                self.assertNotIn("hostile", str(captured.exception))
                self.assertIsNone(captured.exception.__cause__)

        for jobs in (None, {}, (), "hostile-jobs-list"):
            mutated = copy.deepcopy(canonical)
            mutated["jobs"] = jobs
            with self.subTest(field="jobs", value=type(jobs).__name__):
                with self.assertRaises(JobSnapshotError) as captured:
                    verify_job_snapshot(mutated)
                self.assertEqual(
                    str(captured.exception), "snapshot.jobs must be a list"
                )
                self.assertNotIn("hostile", str(captured.exception))
                self.assertIsNone(captured.exception.__cause__)

        for lease_token in ("", " hostile-lease-token ", "\tsecret\n", "x" * 257):
            mutated = copy.deepcopy(canonical)
            mutated["jobs"][0] = _row(
                1,
                "running",
                lease_token=lease_token,
            )
            with self.subTest(field="lease_token", length=len(lease_token)):
                with self.assertRaises(JobSnapshotError) as captured:
                    verify_job_snapshot(mutated)
                self.assertEqual(
                    str(captured.exception),
                    "jobs[0].lease_token must be null or trimmed text",
                )
                self.assertNotIn("secret", str(captured.exception))
                self.assertNotIn("hostile", str(captured.exception))
                self.assertIsNone(captured.exception.__cause__)

        for lease_token in ("x", "x" * 256):
            mutated = copy.deepcopy(canonical)
            mutated["jobs"][0] = _row(
                1,
                "running",
                lease_token=lease_token,
            )
            with self.subTest(field="lease_token", accepted_length=len(lease_token)):
                verified = verify_job_snapshot(mutated)
                self.assertEqual(verified["jobs"][0]["lease_token"], lease_token)

        for snapshot_id in ("A", "a" * 256):
            valid = copy.deepcopy(canonical)
            valid["snapshot_id"] = snapshot_id
            self.assertEqual(verify_job_snapshot(valid)["snapshot_id"], snapshot_id)

    def test_reconciliation_detects_loss_and_nonterminal_jobs(self) -> None:
        before = _snapshot([_row(1, "queued"), _row(2, "retry_wait")], "before", 0)
        lost = _snapshot([_row(1, "succeeded", attempts=1)], "after", 60)
        result = reconcile_job_snapshots(before, lost, accepted_job_id_watermark=2)
        self.assertFalse(result.clean)
        self.assertEqual(result.lost_job_ids, (2,))

        nonterminal = _snapshot(
            [_row(1, "succeeded", attempts=1), _row(2, "queued", attempts=1)],
            "after",
            60,
        )
        result = reconcile_job_snapshots(
            before, nonterminal, accepted_job_id_watermark=2
        )
        self.assertFalse(result.clean)
        self.assertEqual(result.nonterminal_job_ids, (2,))

    def test_clean_reconciliation_requires_one_terminal_row_per_accepted_job(
        self,
    ) -> None:
        before = _snapshot([_row(1, "queued"), _row(2, "retry_wait")], "before", 0)
        after = _snapshot(
            [
                _row(1, "succeeded", attempts=1),
                _row(
                    2,
                    "dead_letter",
                    attempts=3,
                    dead_lettered_at=(NOW + timedelta(seconds=60)).isoformat(),
                ),
                _row(3, "queued"),
            ],
            "after",
            60,
        )
        result = reconcile_job_snapshots(before, after, accepted_job_id_watermark=2)

        self.assertTrue(result.clean)
        self.assertEqual(result.accepted_job_id_watermark, 2)
        self.assertEqual(result.accepted_job_count, 2)
        self.assertEqual(result.terminal_job_count, 2)
        self.assertFalse(result.as_dict()["gate6_pass"])

    def test_after_only_job_at_or_below_watermark_cannot_hide_as_background(
        self,
    ) -> None:
        before = _snapshot([_row(1, "queued"), _row(3, "queued")], "before", 0)
        after = _snapshot(
            [
                _row(1, "succeeded", attempts=1),
                _row(2, "queued"),
                _row(3, "succeeded", attempts=1),
                _row(4, "queued"),
            ],
            "after",
            60,
        )

        result = reconcile_job_snapshots(before, after, accepted_job_id_watermark=3)

        self.assertFalse(result.clean)
        self.assertEqual(result.accepted_job_count, 3)
        self.assertEqual(result.nonterminal_job_ids, (2,))
        with self.assertRaisesRegex(JobSnapshotError, "largest before-snapshot"):
            reconcile_job_snapshots(before, after, accepted_job_id_watermark=1)

    def test_dead_letter_timestamp_after_capture_is_rejected(self) -> None:
        with self.assertRaisesRegex(JobSnapshotError, "postdate snapshot"):
            _snapshot(
                [
                    _row(
                        1,
                        "dead_letter",
                        dead_lettered_at=(NOW + timedelta(seconds=1)).isoformat(),
                    )
                ],
                "future-dead-letter",
                0,
            )

    def test_after_snapshot_must_be_strictly_later(self) -> None:
        before = _snapshot([_row(1, "queued")], "before", 0)
        after = _snapshot([_row(1, "succeeded")], "after", 0)
        with self.assertRaisesRegex(JobSnapshotError, "captured after"):
            reconcile_job_snapshots(before, after, accepted_job_id_watermark=1)

    def test_empty_before_snapshot_is_rejected(self) -> None:
        before = _snapshot([], "before-empty", 0)
        after = _snapshot([_row(1, "succeeded")], "after", 60)
        with self.assertRaisesRegex(JobSnapshotError, "at least one accepted job"):
            reconcile_job_snapshots(before, after, accepted_job_id_watermark=1)

    def test_invalid_watermark_values_are_rejected(self) -> None:
        before = _snapshot([_row(1, "queued")], "before", 0)
        after = _snapshot([_row(1, "succeeded")], "after", 60)
        for watermark in (True, 0, -1):
            with (
                self.subTest(watermark=watermark),
                self.assertRaisesRegex(
                    JobSnapshotError,
                    r"^accepted_job_id_watermark must be a positive integer$",
                ),
            ):
                reconcile_job_snapshots(
                    before,
                    after,
                    accepted_job_id_watermark=watermark,
                )

    def test_attempt_count_regression_is_rejected(self) -> None:
        before = _snapshot([_row(1, "queued", attempts=2)], "before", 0)
        after = _snapshot([_row(1, "succeeded", attempts=1)], "after", 60)
        with self.assertRaisesRegex(JobSnapshotError, "attempt_count regressed"):
            reconcile_job_snapshots(before, after, accepted_job_id_watermark=1)

    def test_terminal_status_rewrite_is_rejected(self) -> None:
        before = _snapshot([_row(1, "succeeded", attempts=1)], "before", 0)
        after = _snapshot([_row(1, "failed", attempts=1)], "after", 60)
        with self.assertRaisesRegex(JobSnapshotError, "changed status"):
            reconcile_job_snapshots(before, after, accepted_job_id_watermark=1)


class JobSnapshotCaptureTests(unittest.IsolatedAsyncioTestCase):
    async def test_capture_uses_one_ordered_all_jobs_query(self) -> None:
        class Connection:
            def __init__(self) -> None:
                self.query = ""

            async def fetch(self, query: str):
                self.query = query
                return [_row(1, "queued")]

        connection = Connection()
        snapshot = await capture_job_snapshot(
            connection,
            snapshot_id="fault.synthetic.capture",
            captured_at=NOW,
        )

        self.assertEqual(verify_job_snapshot(snapshot), snapshot)
        self.assertEqual(connection.query, JOB_SNAPSHOT_SQL)
        for field in (
            "id AS job_id",
            "status",
            "attempt_count",
            "lease_token",
            "dead_lettered_at",
            "ORDER BY id",
        ):
            self.assertIn(field, connection.query)


if __name__ == "__main__":
    unittest.main()
