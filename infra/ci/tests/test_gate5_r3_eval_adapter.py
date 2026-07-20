from __future__ import annotations

import ast
import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import unittest

from anila_contracts.routing import RouteType

from infra.ci import run_gate5_routing_contract as runner
from infra.ci.gate5_r3_eval_adapter import FormalR3EvalAdapter, build_adapter


ROOT = Path(__file__).resolve().parents[3]
DATASET_PATH = ROOT / "infra" / "policy" / "gate5" / "routing-eval.v1.json"
ADAPTER_PATH = ROOT / "infra" / "ci" / "gate5_r3_eval_adapter.py"


class Gate5FormalR3AdapterTests(unittest.TestCase):
    def test_formal_adapter_passes_all_frozen_cases(self) -> None:
        result = runner.run_contract(
            DATASET_PATH, adapter=build_adapter(), runner_commit="test"
        )

        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["observed_case_count"], 160)
        self.assertEqual(
            result["metrics"]["route_top1"],
            {
                "numerator": 60,
                "denominator": 60,
                "rate": 1.0,
                "minimum": 0.95,
            },
        )
        self.assertEqual(result["metrics"]["false_dispatch"]["numerator"], 0)
        self.assertEqual(result["metrics"]["policy_bypass"]["numerator"], 0)
        self.assertEqual(
            result["metrics"]["security_deny_exact"],
            {
                "numerator": 35,
                "denominator": 35,
                "rate": 1.0,
                "minimum": runner.SECURITY_DENY_EXACT_MIN,
            },
        )

    def test_target_hint_mutations_do_not_change_semantic_selection(self) -> None:
        document = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
        case = next(
            item for item in document["cases"] if item["id"] == "single-rag-001"
        )
        adapter = FormalR3EvalAdapter()
        for hint in (None, "image-generator", "unknown-agent"):
            context = copy.deepcopy(case["context"])
            context["requested_agent_id"] = hint
            request = runner.RoutingRequest(
                input=case["input"],
                messages=tuple(case["messages"]),
                context=context,
            )
            observation = adapter.evaluate(request)
            self.assertEqual(observation["route_type"], "single_agent")
            self.assertEqual(observation["selected_agent_id"], "rag-search")
            self.assertTrue(observation["policy_allowed"])

    def test_capability_mutation_is_denied_without_target_hint(self) -> None:
        document = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
        case = next(
            item for item in document["cases"] if item["id"] == "single-rag-001"
        )
        context = copy.deepcopy(case["context"])
        context["capabilities"] = ["unregistered-capability"]
        request = runner.RoutingRequest(
            input=case["input"],
            messages=tuple(case["messages"]),
            context=context,
        )

        observation = FormalR3EvalAdapter().evaluate(request)

        self.assertEqual(observation["route_type"], "deny")
        self.assertIsNone(observation["selected_agent_id"])
        self.assertFalse(observation["policy_allowed"])

    def test_unknown_agent_mutation_is_denied_by_formal_provider(self) -> None:
        document = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
        case = next(
            item for item in document["cases"] if item["id"] == "single-rag-001"
        )
        context = copy.deepcopy(case["context"])
        context["available_agent_ids"] = ["unknown-agent"]
        context["agent_profiles"] = {
            "unknown-agent": {
                "classification_ceiling": "機密",
                "capabilities": ["retrieval", "text"],
            }
        }
        request = runner.RoutingRequest(
            input=case["input"],
            messages=tuple(case["messages"]),
            context=context,
        )

        observation = FormalR3EvalAdapter().evaluate(request)

        self.assertNotEqual(observation["route_type"], "single_agent")
        self.assertIsNone(observation["selected_agent_id"])

    def test_missing_policy_result_is_fail_closed(self) -> None:
        document = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
        request = runner._request_from_case(document["cases"][0])
        adapter = FormalR3EvalAdapter()

        class NoPolicyRuntime:
            def execute(self, *_args: object, **_kwargs: object) -> object:
                return SimpleNamespace(
                    policy_result=None,
                    decision_result=SimpleNamespace(route_type=RouteType.DIRECT_ANSWER),
                    decision=None,
                )

        adapter.runtime = NoPolicyRuntime()  # type: ignore[assignment]
        with self.assertRaisesRegex(RuntimeError, "no PolicyGate result"):
            adapter.evaluate(request)

    def test_direct_answer_with_missing_scope_is_not_policy_allowed(self) -> None:
        document = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
        case = next(
            item for item in document["cases"] if item["id"] == "direct_answer-001"
        )
        request = runner._request_from_case(case)
        adapter = FormalR3EvalAdapter()
        output = adapter._route_payload(
            route_type=RouteType.DIRECT_ANSWER.value,
            snapshot_id=request.context["registry_snapshot_id"],
            target=None,
            required_capabilities=request.context["capabilities"],
            reason="provider_direct_answer",
            fallback=None,
            query=None,
        )

        observation = adapter.evaluate_provider_output(request, output)

        assert observation["route_type"] == RouteType.DIRECT_ANSWER.value
        assert observation["selected_agent_id"] is None
        assert observation["policy_allowed"] is False
        assert observation["fallback"] is None

    def test_general_clarify_without_authorization_denial_remains_allowed(self) -> None:
        document = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
        case = next(
            item for item in document["cases"] if item["id"] == "direct_answer-001"
        )
        context = copy.deepcopy(case["context"])
        context["scopes"].append("agent:invoke")
        request = runner.RoutingRequest(
            input=case["input"],
            messages=tuple(case["messages"]),
            context=context,
        )
        adapter = FormalR3EvalAdapter()
        output = adapter._route_payload(
            route_type=RouteType.CLARIFY.value,
            snapshot_id=request.context["registry_snapshot_id"],
            target=None,
            required_capabilities=request.context["capabilities"],
            reason="clarification_required",
            fallback="clarify",
            query=None,
        )

        observation = adapter.evaluate_provider_output(request, output)

        assert observation["route_type"] == RouteType.CLARIFY.value
        assert observation["selected_agent_id"] is None
        assert observation["policy_allowed"] is True
        assert observation["fallback"] == "clarify"

    def test_mixed_candidates_do_not_project_aggregate_denial_as_disallowed(
        self,
    ) -> None:
        document = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
        case = next(
            item for item in document["cases"] if item["id"] == "direct_answer-001"
        )
        context = copy.deepcopy(case["context"])
        context["classification"] = "機密"
        context["scopes"].append("agent:invoke")
        context["available_agent_ids"] = ["image-generator", "report-generator"]
        context["agent_profiles"]["image-generator"]["classification_ceiling"] = (
            "無機密"
        )
        request = runner.RoutingRequest(
            input=case["input"],
            messages=tuple(case["messages"]),
            context=context,
        )
        adapter = FormalR3EvalAdapter()
        output = adapter._route_payload(
            route_type=RouteType.DIRECT_ANSWER.value,
            snapshot_id=request.context["registry_snapshot_id"],
            target=None,
            required_capabilities=request.context["capabilities"],
            reason="provider_direct_answer",
            fallback=None,
            query=None,
        )

        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        runtime_result = adapter.runtime.execute(
            adapter._build_context(request),
            output,
            adapter._snapshot(request, now=now),
            now=now,
        )
        observation = adapter._observation(runtime_result)

        self.assertEqual(
            [entry.agent_id for entry in runtime_result.candidates.candidates],
            ["report-generator"],
        )
        self.assertIn(
            "CLASSIFICATION_EXCEEDS_CEILING", runtime_result.candidates.reason_codes
        )
        self.assertTrue(observation["policy_allowed"])

    def test_observation_denies_specialized_route_without_context_eligible_candidate(
        self,
    ) -> None:
        result = SimpleNamespace(
            context=SimpleNamespace(required_capabilities=("retrieval",)),
            candidates=SimpleNamespace(
                candidates=(),
                reason_codes=("TASK_TYPE_UNSUPPORTED", "NO_ELIGIBLE_CANDIDATES"),
            ),
            decision_result=SimpleNamespace(
                valid=True,
                route_type=RouteType.CLARIFY,
                reason_codes=("provider_clarify",),
            ),
            policy_result=SimpleNamespace(allowed=False),
            decision=None,
        )

        observation = FormalR3EvalAdapter._observation(result)

        self.assertEqual(observation["route_type"], RouteType.CLARIFY.value)
        self.assertFalse(observation["policy_allowed"])

    def test_adapter_source_does_not_access_case_labels(self) -> None:
        tree = ast.parse(ADAPTER_PATH.read_text(encoding="utf-8"))
        forbidden = {"expected", "category", "case_id", "requested_agent_id"}
        accessed = {
            node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and node.attr in forbidden
        }
        accessed.update(
            node.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Name) and node.id in forbidden
        )
        self.assertEqual(accessed, set())


if __name__ == "__main__":
    unittest.main()
