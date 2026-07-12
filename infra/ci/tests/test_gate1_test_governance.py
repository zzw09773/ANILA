from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path

from infra.ci import check_gate1_test_governance as governance


ROOT = Path(__file__).resolve().parents[3]


def _document() -> dict:
    return {
        "schema_version": 1,
        "required_suites": [
            {
                "id": suite_id,
                "required": True,
                "status": "passing",
                "command": f"run {suite_id}",
                "evidence": f"{suite_id} passed",
            }
            for suite_id in sorted(governance.REQUIRED_SUITES)
        ],
        "historical_csp_baseline": {
            "commit": "f661f5e8c9d2d70d4647ef3f67eb5b9624ffc93c",
            "command": "python -m pytest -q",
            "evidence": "isolated historical snapshot rerun",
            "result": dict(governance.HISTORICAL_CSP_RESULT),
            "outcome_groups": [
                {
                    "id": "HIST-CSP-FAILURES",
                    "outcome": "failure",
                    "classification": "test-staleness",
                    "disposition": "resolved",
                    "cause": "stale test",
                    "cases": [
                        f"tests/test_example.py::test_failure_{index}"
                        for index in range(governance.HISTORICAL_CSP_RESULT["failed"])
                    ],
                    "resolution": "updated the stale tests",
                },
                {
                    "id": "HIST-CSP-ERRORS",
                    "outcome": "error",
                    "classification": "platform",
                    "disposition": "resolved",
                    "cause": "test environment",
                    "cases": [
                        f"tests/test_example.py::test_error_{index}"
                        for index in range(governance.HISTORICAL_CSP_RESULT["errors"])
                    ],
                    "resolution": "made the test environment deterministic",
                },
            ],
        },
        "observed_failures": [
            {
                "id": "F3-TEST",
                "classification": "test-staleness",
                "disposition": "resolved",
                "cases": ["tests/test_example.py::test_case"],
                "resolution": "updated the stale fixture",
            }
        ],
        "allowed_skips": [],
        "quarantine": [],
    }


class RegistryTests(unittest.TestCase):
    def test_repository_registry_and_xfail_policy_pass(self) -> None:
        import json

        document = json.loads(
            (ROOT / "infra/ci/gate1-test-baseline.json").read_text(encoding="utf-8")
        )
        governance.validate_registry(document, as_of=dt.date(2026, 7, 12))
        self.assertEqual(governance.find_xfail_calls(ROOT), [])

    def test_required_suite_cannot_be_non_passing(self) -> None:
        document = _document()
        document["required_suites"][0]["status"] = "quarantined"
        with self.assertRaisesRegex(governance.GovernanceError, "must be passing"):
            governance.validate_registry(document, as_of=dt.date(2026, 7, 12))

    def test_historical_csp_outcome_count_cannot_drift(self) -> None:
        document = _document()
        document["historical_csp_baseline"]["outcome_groups"][0]["cases"].pop()
        with self.assertRaisesRegex(governance.GovernanceError, "outcome count mismatch"):
            governance.validate_registry(document, as_of=dt.date(2026, 7, 12))

    def test_quarantine_requires_ticket_owner_and_expiry(self) -> None:
        document = _document()
        document["quarantine"] = [
            {
                "nodeid": "tests/test_old.py::test_old",
                "required": False,
                "classification": "platform",
                "owner": "CI Owner",
                "ticket": "GH-123",
                "expires_on": "2026-07-31",
                "reason": "temporary runner incompatibility",
            }
        ]
        governance.validate_registry(document, as_of=dt.date(2026, 7, 12))
        document["quarantine"][0]["expires_on"] = "2026-07-01"
        with self.assertRaisesRegex(governance.GovernanceError, "expired"):
            governance.validate_registry(document, as_of=dt.date(2026, 7, 12))

    def test_ast_scan_rejects_xfail(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            tests = root / "tests"
            tests.mkdir()
            (tests / "test_bad.py").write_text(
                "import pytest\n\n@pytest.mark.xfail\ndef test_bad(): pass\n",
                encoding="utf-8",
            )
            original = governance.TEST_ROOTS
            governance.TEST_ROOTS = ("tests",)
            try:
                self.assertEqual(
                    governance.find_xfail_calls(root), ["tests/test_bad.py:3"]
                )
            finally:
                governance.TEST_ROOTS = original

    def test_skip_collector_exposes_new_skip_callsite(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            tests = root / "tests"
            tests.mkdir()
            (tests / "test_skip.py").write_text(
                "import pytest\n\ndef test_skip(): pytest.skip('hidden red')\n",
                encoding="utf-8",
            )
            original = governance.TEST_ROOTS
            governance.TEST_ROOTS = ("tests",)
            try:
                self.assertEqual(
                    governance.collect_skip_calls(root),
                    {"tests/test_skip.py::pytest.skip": 1},
                )
            finally:
                governance.TEST_ROOTS = original

    def test_skip_collector_fails_closed_on_unparseable_source(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            tests = root / "tests"
            tests.mkdir()
            original = governance.TEST_ROOTS
            governance.TEST_ROOTS = ("tests",)
            try:
                for filename, payload in (
                    ("test_syntax.py", b"def broken(:\n"),
                    ("test_encoding.py", b"\xff\xfe\x00"),
                ):
                    path = tests / filename
                    path.write_bytes(payload)
                    with self.subTest(filename=filename):
                        with self.assertRaisesRegex(
                            governance.GovernanceError, "cannot inspect"
                        ):
                            governance.collect_skip_calls(root)
                    path.unlink()
            finally:
                governance.TEST_ROOTS = original


if __name__ == "__main__":
    unittest.main()
