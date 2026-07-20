from __future__ import annotations

import os
import unittest
from dataclasses import fields
from pathlib import Path

from infra.ci import run_gate5_routing_contract as runner


ROOT = Path(__file__).resolve().parents[3]
DATASET_PATH = ROOT / "infra" / "policy" / "gate5" / "routing-eval.v1.json"


class Gate5RoutingContractTests(unittest.TestCase):
    def test_missing_r3_adapter_is_explicit_skip_without_metrics(self) -> None:
        result = runner.run_contract(DATASET_PATH)

        self.assertEqual(result["status"], "skipped")
        self.assertNotIn("metrics", result)
        self.assertEqual(
            result["runner_commit"],
            os.environ.get("GITHUB_SHA") or "uncommitted-local",
        )
        self.assertIn("R3 runtime adapter", result["reason"])

    def test_adapter_receives_no_expected_prediction_and_scores_observation(
        self,
    ) -> None:
        seen: list[runner.RoutingRequest] = []

        def adapter(request: runner.RoutingRequest) -> dict[str, object]:
            seen.append(request)
            self.assertEqual(
                {field.name for field in fields(request)},
                {"input", "messages", "context"},
            )
            self.assertFalse(hasattr(request, "expected"))
            self.assertFalse(hasattr(request, "case_id"))
            self.assertFalse(hasattr(request, "must_not_route_to"))
            with self.assertRaises(TypeError):
                request.context["classification"] = "極機密"  # type: ignore[index]
            return {
                "route_type": "direct_answer",
                "selected_agent_id": None,
                "policy_allowed": True,
                "policy_reason_codes": ["adapter_observed"],
                "fallback": None,
            }

        result = runner.run_contract(DATASET_PATH, adapter=adapter)

        self.assertEqual(result["observed_case_count"], 160)
        self.assertEqual(len(seen), 160)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["metrics"]["route_top1"]["numerator"], 0)
        self.assertEqual(result["metrics"]["false_dispatch"]["numerator"], 0)
        self.assertEqual(result["metrics"]["policy_bypass"]["numerator"], 20)
        self.assertEqual(seen[0].context["classification"], "無機密")

    def test_malformed_observation_fails_closed(self) -> None:
        def adapter(_: runner.RoutingRequest) -> dict[str, object]:
            return {"route_type": "direct_answer"}

        with self.assertRaisesRegex(runner.RoutingContractError, "missing field"):
            runner.run_contract(DATASET_PATH, adapter=adapter)

    def test_deny_observation_requires_null_selection_and_false_policy(self) -> None:
        for selected_agent_id, policy_allowed in (
            ("unexpected-agent", False),
            (None, True),
        ):
            with self.subTest(
                selected_agent_id=selected_agent_id, policy_allowed=policy_allowed
            ):
                with self.assertRaisesRegex(
                    runner.RoutingContractError,
                    "deny observation requires",
                ):
                    runner.RoutingObservation.from_value(
                        {
                            "route_type": "deny",
                            "selected_agent_id": selected_agent_id,
                            "policy_allowed": policy_allowed,
                            "policy_reason_codes": ["contract_test"],
                            "fallback": "deny",
                        },
                        path="observation",
                    )

    def test_async_adapter_is_not_silently_accepted(self) -> None:
        async def adapter(_: runner.RoutingRequest) -> dict[str, object]:
            return {
                "route_type": "direct_answer",
                "selected_agent_id": None,
                "policy_allowed": False,
                "policy_reason_codes": ["adapter_observed"],
                "fallback": None,
            }

        with self.assertRaisesRegex(runner.RoutingContractError, "synchronous"):
            runner.run_contract(DATASET_PATH, adapter=adapter)

    def test_cli_without_adapter_returns_skip_code(self) -> None:
        self.assertEqual(runner.main([str(DATASET_PATH)]), 2)

    def test_adapter_that_reads_prediction_labels_is_blocked(self) -> None:
        def mutated_adapter(request: runner.RoutingRequest) -> object:
            # A mutation that attempts to recover the frozen label must not
            # have a hidden field to read from the request projection.
            return getattr(request, "expected")

        with self.assertRaisesRegex(
            runner.RoutingContractError, "case index 0: adapter failed"
        ):
            runner.run_contract(DATASET_PATH, adapter=mutated_adapter)

    def test_permissive_policy_mutation_fails_the_exit_metrics(self) -> None:
        def mutated_adapter(_: runner.RoutingRequest) -> dict[str, object]:
            # Simulates removing the policy gate: every input is dispatched.
            return {
                "route_type": "single_agent",
                "selected_agent_id": "rag-search",
                "policy_allowed": True,
                "policy_reason_codes": ["mutation"],
                "fallback": None,
            }

        result = runner.run_contract(DATASET_PATH, adapter=mutated_adapter)

        self.assertEqual(result["status"], "failed")
        self.assertGreater(result["metrics"]["false_dispatch"]["numerator"], 0)
        self.assertGreater(result["metrics"]["policy_bypass"]["numerator"], 0)

    def test_unknown_agent_mutation_fails_route_metric(self) -> None:
        def mutated_adapter(_: runner.RoutingRequest) -> dict[str, object]:
            return {
                "route_type": "single_agent",
                "selected_agent_id": "unknown-agent",
                "policy_allowed": True,
                "policy_reason_codes": ["mutation"],
                "fallback": None,
            }

        result = runner.run_contract(DATASET_PATH, adapter=mutated_adapter)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["metrics"]["route_top1"]["numerator"], 0)


if __name__ == "__main__":
    unittest.main()
