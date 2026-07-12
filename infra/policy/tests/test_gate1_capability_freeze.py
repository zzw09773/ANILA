from __future__ import annotations

import datetime as dt
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[3]
CHECK_PATH = ROOT / "infra/policy/gate1/check_capability_freeze.py"
SPEC = importlib.util.spec_from_file_location("gate1_capability_freeze", CHECK_PATH)
assert SPEC is not None and SPEC.loader is not None
policy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(policy)


def _load(relative: str) -> dict:
    return json.loads((ROOT / relative).read_text(encoding="utf-8"))


class CapabilityFreezePolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.baseline = _load("infra/policy/gate1/capability-freeze-baseline.json")
        self.exceptions = _load("infra/policy/gate1/capability-freeze-exceptions.json")
        self.today = dt.date.today()
        self.current = policy.collect_inventory(ROOT, self.baseline)

    def test_reviewed_repository_inventory_passes(self) -> None:
        result = policy.evaluate(
            self.baseline, self.current, self.exceptions, self.today
        )
        self.assertTrue(result["passed"], result["violations"])
        self.assertEqual(result["violations"], [])

    def test_unregistered_additions_fail_for_every_frozen_category(self) -> None:
        additions = {
            "agent.runtime_types": "test_agent_runtime",
            "artifact.types": "test_artifact",
            "memory.core_types": "test_memory",
            "multiagent.coordinator_methods": "Coordinator.spawn_unbounded_workers",
        }
        changed = {key: list(values) for key, values in self.current.items()}
        for surface, value in additions.items():
            changed[surface].append(value)

        result = policy.evaluate(
            self.baseline, changed, self.exceptions, self.today
        )

        self.assertFalse(result["passed"])
        self.assertEqual(
            {item["change"] for item in result["violations"]},
            {f"{surface}::{value}" for surface, value in additions.items()},
        )

    def test_exact_active_exception_allows_only_named_change(self) -> None:
        changed = {key: list(values) for key, values in self.current.items()}
        changed["artifact.types"].extend(["approved_test_type", "unapproved_test_type"])
        registry = {
            "schema_version": 1,
            "exceptions": [
                {
                    "id": "F7-EXAMPLE-1",
                    "architecture_owner": "Lin Mei-Hua",
                    "rationale": "Pilot compatibility requires this temporary artifact type.",
                    "ticket": "ANILA-1234",
                    "approved_on": self.today.isoformat(),
                    "expires_on": (self.today + dt.timedelta(days=7)).isoformat(),
                    "changes": ["artifact.types::approved_test_type"],
                }
            ],
        }

        result = policy.evaluate(self.baseline, changed, registry, self.today)

        self.assertFalse(result["passed"])
        self.assertEqual(
            [item["change"] for item in result["violations"]],
            ["artifact.types::unapproved_test_type"],
        )
        self.assertEqual(
            [item["change"] for item in result["approved_exceptions"]],
            ["artifact.types::approved_test_type"],
        )

    def test_expired_exception_fails_closed(self) -> None:
        registry = {
            "schema_version": 1,
            "exceptions": [
                {
                    "id": "F7-EXPIRED-1",
                    "architecture_owner": "Lin Mei-Hua",
                    "rationale": "This deliberately expired test exception is sufficiently specific.",
                    "ticket": "ANILA-1235",
                    "approved_on": (self.today - dt.timedelta(days=2)).isoformat(),
                    "expires_on": (self.today - dt.timedelta(days=1)).isoformat(),
                    "changes": ["artifact.types::expired_test_type"],
                }
            ],
        }
        with self.assertRaisesRegex(policy.PolicyError, "expired"):
            policy.evaluate(self.baseline, self.current, registry, self.today)

    def test_missing_named_architecture_owner_fails_closed(self) -> None:
        registry = {
            "schema_version": 1,
            "exceptions": [
                {
                    "id": "F7-NO-OWNER-1",
                    "architecture_owner": "TBD",
                    "rationale": "This test verifies that an unnamed approval cannot authorize work.",
                    "ticket": "ANILA-1236",
                    "approved_on": self.today.isoformat(),
                    "expires_on": (self.today + dt.timedelta(days=7)).isoformat(),
                    "changes": ["artifact.types::ownerless_test_type"],
                }
            ],
        }
        with self.assertRaisesRegex(policy.PolicyError, "architecture_owner"):
            policy.evaluate(self.baseline, self.current, registry, self.today)

    def test_enum_collector_observes_a_real_source_addition(self) -> None:
        relative = "controlled/contracts.py"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / relative
            path.parent.mkdir(parents=True)
            path.write_text(
                "from enum import Enum\n"
                "class ArtifactType(str, Enum):\n"
                "    REPORT = 'report'\n"
                "    AUDIO = 'audio'\n",
                encoding="utf-8",
            )
            mini_baseline = {
                "schema_version": 1,
                "surfaces": [
                    {
                        "id": "artifact.types",
                        "collector": {
                            "type": "python_enum",
                            "path": relative,
                            "symbol": "ArtifactType",
                        },
                    }
                ],
            }
            inventory = policy.collect_inventory(root, mini_baseline)
        self.assertEqual(inventory["artifact.types"], ["audio", "report"])

    def test_pr_cannot_bless_an_addition_by_rewriting_baseline(self) -> None:
        branch_baseline = json.loads(json.dumps(self.baseline))
        branch_baseline["reviewed_inventory"]["artifact.types"].append("audio")
        with self.assertRaisesRegex(policy.PolicyError, "trusted PR base"):
            policy.enforce_trusted_baseline(
                branch_baseline, self.baseline, "origin/main:capability-freeze-baseline.json"
            )

    def test_missing_git_fails_closed_with_policy_error(self) -> None:
        relative = "infra/policy/gate1/capability-freeze-baseline.json"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / relative
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(self.baseline), encoding="utf-8")
            with mock.patch.object(
                policy.subprocess,
                "run",
                side_effect=FileNotFoundError("git executable missing"),
            ):
                with self.assertRaisesRegex(policy.PolicyError, "cannot execute git"):
                    policy._baseline_for_run(
                        root,
                        relative,
                        "origin/main",
                        bootstrap_if_missing=False,
                    )


if __name__ == "__main__":
    unittest.main()
