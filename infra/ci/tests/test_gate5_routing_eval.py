from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from infra.ci import check_gate5_routing_eval as verifier


ROOT = Path(__file__).resolve().parents[3]
DATASET_PATH = ROOT / "infra" / "policy" / "gate5" / "routing-eval.v1.json"


class Gate5RoutingEvalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.document = json.loads(DATASET_PATH.read_text(encoding="utf-8"))

    def test_frozen_repository_fixture_passes(self) -> None:
        report = verifier.verify_dataset_file(DATASET_PATH)
        self.assertEqual(report["case_count"], 160)
        self.assertEqual(report["dataset_sha256"], verifier.PINNED_DATASET_SHA256)
        self.assertEqual(report["denominator_counts"], {
            "route_top1": 60,
            "false_dispatch": 90,
            "policy_bypass": 20,
        })

    def test_category_counts_sum_to_frozen_case_count(self) -> None:
        self.assertEqual(sum(self.document["category_counts"].values()), 160)
        self.assertEqual(self.document["case_count"], 160)
        verifier.validate_dataset(self.document)

    def test_hash_mutation_is_rejected(self) -> None:
        mutated = copy.deepcopy(self.document)
        mutated["cases"][0]["input"] += " changed"
        mutated["cases"][0]["messages"][0]["content"] = mutated["cases"][0]["input"]
        with self.assertRaisesRegex(verifier.RoutingEvalError, "hash mismatch"):
            verifier.validate_dataset(mutated)

    def test_case_count_mutation_is_rejected(self) -> None:
        mutated = copy.deepcopy(self.document)
        mutated["case_count"] = 159
        with self.assertRaisesRegex(verifier.RoutingEvalError, "frozen value 160"):
            verifier.validate_dataset(mutated)

    def test_category_count_mutation_is_rejected(self) -> None:
        mutated = copy.deepcopy(self.document)
        mutated["category_counts"]["direct_answer"] = 21
        with self.assertRaisesRegex(verifier.RoutingEvalError, "frozen category distribution"):
            verifier.validate_dataset(mutated)

    def test_duplicate_case_id_mutation_is_rejected(self) -> None:
        mutated = copy.deepcopy(self.document)
        mutated["cases"][1]["id"] = mutated["cases"][0]["id"]
        with self.assertRaisesRegex(verifier.RoutingEvalError, "duplicate case ID"):
            verifier.validate_dataset(mutated)

    def test_selected_agent_mutation_is_rejected(self) -> None:
        mutated = copy.deepcopy(self.document)
        mutated["cases"][20]["expected"]["selected_agent_id"] = "image-generator"
        with self.assertRaisesRegex(verifier.RoutingEvalError, "single_agent"):
            verifier.validate_dataset(mutated)

    def test_policy_mutation_is_rejected(self) -> None:
        mutated = copy.deepcopy(self.document)
        injection = next(item for item in mutated["cases"] if item["category"] == "prompt_injection")
        injection["expected"]["policy_allowed"] = True
        with self.assertRaisesRegex(verifier.RoutingEvalError, "policy_allowed conflicts"):
            verifier.validate_dataset(mutated)

    def test_unknown_root_field_is_rejected(self) -> None:
        mutated = copy.deepcopy(self.document)
        mutated["unexpected"] = "fail closed"
        with self.assertRaisesRegex(verifier.RoutingEvalError, "unknown field"):
            verifier.validate_dataset(mutated)

    def test_unknown_nested_field_is_rejected(self) -> None:
        mutated = copy.deepcopy(self.document)
        mutated["cases"][0]["context"]["untrusted"] = True
        with self.assertRaisesRegex(verifier.RoutingEvalError, "unknown field"):
            verifier.validate_dataset(mutated)

    def test_classification_mutation_is_rejected(self) -> None:
        mutated = copy.deepcopy(self.document)
        denied = next(item for item in mutated["cases"] if item["category"] == "classification_denied")
        denied["context"]["classification"] = "無機密"
        with self.assertRaisesRegex(verifier.RoutingEvalError, "classification_denied"):
            verifier.validate_dataset(mutated)

    def test_denominator_membership_mutation_is_rejected(self) -> None:
        mutated = copy.deepcopy(self.document)
        mutated["denominators"]["false_dispatch"]["case_ids"].pop()
        with self.assertRaisesRegex(verifier.RoutingEvalError, "frozen denominator 90"):
            verifier.validate_dataset(mutated)

    def test_denominator_same_category_substitution_is_rejected(self) -> None:
        mutated = copy.deepcopy(self.document)
        ids = mutated["denominators"]["route_top1"]["case_ids"]
        ids[0] = ids[1]
        with self.assertRaisesRegex(verifier.RoutingEvalError, "duplicate value"):
            verifier.validate_dataset(mutated)

    def test_threshold_mutation_is_rejected(self) -> None:
        mutated = copy.deepcopy(self.document)
        mutated["thresholds"]["false_dispatch_max_exclusive"] = 0.02
        with self.assertRaisesRegex(verifier.RoutingEvalError, "frozen threshold"):
            verifier.validate_dataset(mutated)

    def test_anti_template_input_normalization_has_high_unique_ratio(self) -> None:
        normalized = {
            verifier.normalize_eval_input(item["input"])
            for item in self.document["cases"]
        }
        self.assertGreaterEqual(len(normalized) / len(self.document["cases"]), 0.8)

    def test_single_agent_distribution_and_explicit_mentions_are_bounded(self) -> None:
        cases = [item for item in self.document["cases"] if item["category"] == "single_agent"]
        distribution = {
            agent_id: sum(item["expected"]["selected_agent_id"] == agent_id for item in cases)
            for agent_id in verifier.AGENT_IDS
        }
        self.assertEqual(distribution, {
            "rag-search": 20,
            "image-generator": 20,
            "report-generator": 20,
        })
        explicit = sum(
            bool(verifier.SINGLE_AGENT_MENTION_RE.search(item["input"])) for item in cases
        )
        self.assertLessEqual(explicit / len(cases), 0.25)

    def test_prompt_injection_has_five_attack_families(self) -> None:
        families = {
            verifier.injection_family(item["input"])
            for item in self.document["cases"]
            if item["category"] == "prompt_injection"
        }
        self.assertGreaterEqual(len(families), 5)
        self.assertNotIn(None, families)

    def test_unavailable_agents_cover_all_targets_and_both_states(self) -> None:
        cases = [item for item in self.document["cases"] if item["category"] == "agent_unavailable"]
        self.assertEqual(
            {item["context"]["requested_agent_id"] for item in cases},
            set(verifier.AGENT_IDS),
        )
        self.assertEqual(
            {
                item["context"]["health"]["agent_status"][item["context"]["requested_agent_id"]]
                for item in cases
            },
            {"unhealthy", "unknown"},
        )

    def test_trusted_contexts_are_not_template_copies(self) -> None:
        contexts = {
            json.dumps(item["context"], ensure_ascii=False, sort_keys=True)
            for item in self.document["cases"]
        }
        self.assertGreaterEqual(len(contexts), 40)

    def test_duplicate_json_key_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "duplicate.json"
            path.write_text('{"schema_version": "x", "schema_version": "y"}', encoding="utf-8")
            with self.assertRaisesRegex(verifier.RoutingEvalError, "duplicate JSON key"):
                verifier.verify_dataset_file(path)

    def test_utf8_bom_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "bom.json"
            path.write_bytes(b"\xef\xbb\xbf{}")
            with self.assertRaisesRegex(verifier.RoutingEvalError, "BOM"):
                verifier.verify_dataset_file(path)


if __name__ == "__main__":
    unittest.main()
