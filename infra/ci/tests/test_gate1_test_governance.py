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
        workflow = (ROOT / governance.REQUIRED_WORKFLOW).read_text(encoding="utf-8")
        governance.verify_required_skip_wiring(document, workflow)
        governance.verify_studio_full_wiring(workflow)
        gate0_workflow = (ROOT / governance.GATE0_SECURITY_WORKFLOW).read_text(
            encoding="utf-8"
        )
        governance.verify_card_material_pytest_wiring(gate0_workflow)
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

    def test_required_skip_requires_concrete_ci_wiring(self) -> None:
        document = _document()
        document["allowed_skips"] = [
            {
                "path": "tests/test_pg.py",
                "kind": "pytest.mark.skipif",
                "count": 1,
                "required_execution": True,
                "reason": "The PostgreSQL runner must execute this guard.",
            }
        ]
        with self.assertRaisesRegex(governance.GovernanceError, "ci_job"):
            governance.validate_registry(document, as_of=dt.date(2026, 7, 12))

        document["allowed_skips"][0].update(
            {
                "ci_job": "postgres-rls",
                "ci_command_fragment": "pytest tests/test_pg.py",
            }
        )
        governance.validate_registry(document, as_of=dt.date(2026, 7, 12))
        with self.assertRaisesRegex(governance.GovernanceError, "not defined"):
            governance.verify_required_skip_wiring(
                document,
                "jobs:\n  another-job:\n    steps: []\n",
            )

    def test_required_skip_command_must_be_inside_declared_job(self) -> None:
        document = _document()
        document["allowed_skips"] = [
            {
                "path": "tests/test_pg.py",
                "kind": "pytest.skip",
                "count": 1,
                "required_execution": True,
                "reason": "The PostgreSQL runner must execute this guard.",
                "ci_job": "postgres-rls",
                "ci_command_fragment": "pytest tests/test_pg.py",
            }
        ]
        workflow = (
            "jobs:\n"
            "  postgres-rls:\n"
            "    steps:\n"
            "      - run: pytest tests/a_different_test.py\n"
            "  other-job:\n"
            "    steps:\n"
            "      - run: pytest tests/test_pg.py\n"
        )
        with self.assertRaisesRegex(governance.GovernanceError, "not wired"):
            governance.verify_required_skip_wiring(document, workflow)

    def test_workflow_job_parser_ignores_job_shaped_lines_in_run_blocks(self) -> None:
        workflow = (
            "jobs:\n"
            "  contract-smoke:\n"
            "    steps:\n"
            "      - run: |\n"
            "          echo preparing\n"
            "          setup:\n"
            "          echo complete\n"
            "  postgres-rls:\n"
            "    steps:\n"
            "      - run: pytest tests/test_pg.py\n"
        )

        jobs = governance._workflow_jobs(workflow)

        self.assertEqual(set(jobs), {"contract-smoke", "postgres-rls"})
        self.assertIn("setup:", jobs["contract-smoke"])

    def test_workflow_job_parser_fails_closed_on_invalid_yaml(self) -> None:
        with self.assertRaisesRegex(governance.GovernanceError, "invalid YAML"):
            governance._workflow_jobs("jobs:\n  broken: [\n")

    def test_card_material_pytest_must_remain_in_gate0_required_job(self) -> None:
        workflow = (ROOT / governance.GATE0_SECURITY_WORKFLOW).read_text(
            encoding="utf-8"
        )
        governance.verify_card_material_pytest_wiring(workflow)
        without_pytest = workflow.replace(
            "          python -m pytest "
            "infra/deployment/tests/test_card_material_scanner.py -q\n",
            "",
        )
        self.assertNotEqual(workflow, without_pytest)
        with self.assertRaisesRegex(governance.GovernanceError, "not wired"):
            governance.verify_card_material_pytest_wiring(without_pytest)

    def test_card_material_metadata_and_continue_on_error_mutations_fail(self) -> None:
        workflow = (ROOT / governance.GATE0_SECURITY_WORKFLOW).read_text(
            encoding="utf-8"
        )
        command = governance.CARD_MATERIAL_COMMAND
        run_command = f"          {command}\n"

        moved_to_env = workflow.replace(
            run_command,
            "        env:\n"
            f"          CARD_MATERIAL_COMMAND: {command}\n"
            "        run: |\n",
            1,
        )
        self.assertNotEqual(workflow, moved_to_env)
        with self.assertRaises(governance.GovernanceError):
            governance.verify_card_material_pytest_wiring(moved_to_env)

        moved_to_step_name = workflow.replace(
            f"      - name: {governance.CARD_MATERIAL_STEP_NAME}\n",
            f"      - name: {governance.CARD_MATERIAL_STEP_NAME} {command}\n",
            1,
        ).replace(run_command, "          echo card-material-command-moved\n", 1)
        self.assertNotEqual(workflow, moved_to_step_name)
        with self.assertRaises(governance.GovernanceError):
            governance.verify_card_material_pytest_wiring(moved_to_step_name)

        continue_on_error = workflow.replace(
            f"      - name: {governance.CARD_MATERIAL_STEP_NAME}\n",
            f"      - name: {governance.CARD_MATERIAL_STEP_NAME}\n"
            "        continue-on-error: true\n",
            1,
        )
        self.assertNotEqual(workflow, continue_on_error)
        with self.assertRaises(governance.GovernanceError):
            governance.verify_card_material_pytest_wiring(continue_on_error)

    def test_studio_full_redis_durability_wiring_is_required(self) -> None:
        workflow = (ROOT / governance.REQUIRED_WORKFLOW).read_text(encoding="utf-8")
        governance.verify_studio_full_wiring(workflow)
        mutations = {
            "start": ("docker run --detach", "docker create"),
            "appendonly": ("--appendonly yes", "--appendonly no"),
            "appendfsync": ("--appendfsync everysec", "--appendfsync no"),
            "health": ("redis-cli ping", "redis-cli echo ready"),
            "bounded health loop": (
                "          ready=0\n"
                "          for attempt in $(seq 1 30); do",
                "          ready=0\n"
                "          while true; do",
            ),
            "GitHub environment export": (
                '          echo "ANILA_STUDIO_REDIS_TEST_URL='
                'redis://127.0.0.1:${port}/0" \\\n'
                '            >> "$GITHUB_ENV"',
                '          echo "ANILA_STUDIO_REDIS_TEST_URL='
                'redis://127.0.0.1:${port}/0"',
            ),
            "full pytest": (
                "      - name: Run complete Studio suite\n"
                "        working-directory: services/anila-studio\n"
                "        run: python -m pytest tests -q",
                "      - name: Run complete Studio suite\n"
                "        working-directory: services/anila-studio\n"
                "        run: python -m pytest tests/test_auth.py -q",
            ),
            "always cleanup": ("if: ${{ always() }}", "if: ${{ success() }}"),
            "container cleanup": (
                'docker rm -f "$ANILA_STUDIO_REDIS_DOCKER_CONTAINER"',
                "echo cleanup-disabled",
            ),
        }
        for label, (required, replacement) in mutations.items():
            with self.subTest(label=label):
                mutated = workflow.replace(required, replacement, 1)
                self.assertNotEqual(workflow, mutated)
                with self.assertRaises(governance.GovernanceError):
                    governance.verify_studio_full_wiring(mutated)

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

    def test_ast_scan_rejects_unittest_expected_failure(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            tests = root / "tests"
            tests.mkdir()
            (tests / "test_bad.py").write_text(
                "import unittest\n\n"
                "@unittest.expectedFailure\n"
                "def test_bad(): pass\n",
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

    def test_skip_collector_handles_bare_called_and_unittest_decorators(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            tests = root / "tests"
            tests.mkdir()
            (tests / "test_skip.py").write_text(
                "import pytest\n"
                "import unittest\n\n"
                "@pytest.mark.skip\n"
                "def test_bare_pytest(): pass\n\n"
                "@pytest.mark.skip(reason='called')\n"
                "def test_called_pytest(): pass\n\n"
                "@unittest.skip('skip')\n"
                "def test_unittest_skip(): pass\n\n"
                "@unittest.skipIf(True, 'skip if')\n"
                "def test_unittest_skip_if(): pass\n\n"
                "@unittest.skipUnless(False, 'skip unless')\n"
                "def test_unittest_skip_unless(): pass\n\n"
                "class TestRuntimeSkip(unittest.TestCase):\n"
                "    def test_runtime_skip(self): self.skipTest('runtime')\n",
                encoding="utf-8",
            )
            original = governance.TEST_ROOTS
            governance.TEST_ROOTS = ("tests",)
            try:
                self.assertEqual(
                    governance.collect_skip_calls(root),
                    {
                        "tests/test_skip.py::pytest.mark.skip": 2,
                        "tests/test_skip.py::unittest.skip": 1,
                        "tests/test_skip.py::unittest.skipIf": 1,
                        "tests/test_skip.py::unittest.skipUnless": 1,
                        "tests/test_skip.py::self.skipTest": 1,
                    },
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
