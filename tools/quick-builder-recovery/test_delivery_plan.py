#!/usr/bin/env python3
# ruff: noqa: E402 -- Install the sibling audit guard before test imports.
import os
import sys

sys.dont_write_bytecode = True
_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "delivery_plan.py")
_plan = {"__name__": "delivery_plan_test_module", "__file__": _path}
with open(_path, "rb") as _input:
    exec(compile(_input.read(), _path, "exec"), _plan)

import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock


def plan_fixture(complete=False):
    checksum = "1" * 64 if complete else None
    sources = [{"id": "image:" + name, "version": "1.synthetic" if complete else None,
                "artifact_sha256": checksum, "source_url": "https://example.invalid/upstream" if complete else None,
                "publisher": "Synthetic fixture" if complete else None, "license_expression": "MIT" if complete else None,
                "author_country": "US" if complete else None, "control_country": "US" if complete else None,
                "approval": {"decision": "approved" if complete else "pending", "reviewer_id": "fixture-reviewer" if complete else None,
                             "evidence_sha256": checksum}} for name in _plan["IMAGES"]]
    image_digest = "sha256:" + "2" * 64 if complete else None
    images = [{"id": name, "source_id": "image:" + name, "archive_sha256": checksum, "image_digest": image_digest,
               "sbom_sha256": checksum, "license_review_sha256": checksum} for name in _plan["IMAGES"]]
    return {"schema": _plan["SCHEMA"], "base_revision": "0" * 40, "target_revision": "1" * 40,
            "source_bundle_sha256": checksum,
            "changes": [{"path": "docs/example.md", "before_sha256": "a" * 64, "after_sha256": "b" * 64}],
            "sources": sources, "images": images,
            "worker": {"scope": "target-worker-image", "image_digest": image_digest,
                       "checks": {name: checksum for name in _plan["WORKER_CHECKS"]}},
            "acceptance": {name: checksum for name in ("source_verification_sha256", "file_restore_rehearsal_sha256",
                                                       "regression_report_sha256", "rollback_review_sha256")}}


class DeliveryPlanTests(unittest.TestCase):
    def setUp(self):
        self.value = plan_fixture()

    def validate(self):
        return _plan["validate"](self.value)

    def invalid(self, code=None):
        with self.assertRaises(_plan["RecoveryError"]) as caught:
            self.validate()
        if code is not None:
            self.assertEqual(str(caught.exception), code)

    def test_unknown_sources_and_all_sixteen_missing_images_never_ready(self):
        result = self.validate()
        self.assertFalse(result["metadata_complete"])
        self.assertEqual(len(result["images_missing"]), 16)
        self.assertEqual(len(result["sources_missing"]), 16)
        self.assertEqual(len(result["worker_checks_missing"]), 10)
        self.assertTrue(result["source_bundle_missing"])
        self.assertFalse(result["deployment_ready"])

    def test_complete_claims_are_still_unverified_and_not_apply_permission(self):
        self.value = plan_fixture(complete=True)
        result = self.validate()
        self.assertTrue(result["metadata_complete"])
        for flag in ("evidence_references_verified", "source_approval_verified", "image_identity_verified",
                     "worker_readiness_verified", "patch_apply_permitted", "deployment_ready"):
            self.assertIs(result[flag], False)

    def test_source_only_archive_cannot_replace_sixteen_image_slots(self):
        self.value["images"] = []
        self.invalid("plan_invalid_image_slots")

    def test_duplicate_or_unknown_image_slot(self):
        self.value["images"][1] = dict(self.value["images"][0])
        self.invalid("plan_invalid_image_slots")
        self.value = plan_fixture()
        self.value["images"][0]["id"] = "source.tar"
        self.invalid("plan_invalid_image_slots")

    def test_image_slot_requires_its_own_source_record(self):
        self.value["images"][0]["source_id"] = self.value["images"][1]["source_id"]
        self.invalid("plan_missing_image_source_record")

    def test_source_record_cannot_disappear(self):
        self.value["sources"].pop()
        self.invalid("plan_missing_image_source_record")

    def test_source_archive_and_image_slot_hashes_must_match(self):
        self.value = plan_fixture(complete=True)
        self.value["images"][0]["archive_sha256"] = "f" * 64
        self.invalid("plan_image_source_hash_mismatch")

    def test_unknown_country_and_license_remain_missing_despite_approval(self):
        self.value = plan_fixture(complete=True)
        self.value["sources"][0].update(author_country=None, license_expression="unknown")
        result = self.validate()
        self.assertFalse(result["metadata_complete"])
        self.assertEqual(result["sources_missing"][0]["missing"], ["license_expression", "author_country"])

    def test_pending_and_rejected_source_are_missing(self):
        self.value = plan_fixture(complete=True)
        for decision in ("pending", "rejected"):
            self.value["sources"][0]["approval"]["decision"] = decision
            self.assertIn("approval.decision", self.validate()["sources_missing"][0]["missing"])

    def test_caller_cannot_supply_ready_claim(self):
        self.value["deployment_ready"] = True
        self.invalid("plan_invalid_fields")

    def test_revision_is_fixed_full_hash_and_must_change(self):
        self.value["base_revision"] = "main"
        self.invalid("plan_invalid_revision")
        self.value["base_revision"] = self.value["target_revision"]
        self.invalid("plan_identical_revisions")

    def test_added_and_deleted_source_entries_are_supported(self):
        self.value["changes"] = [{"path": "new.py", "before_sha256": None, "after_sha256": "1" * 64},
                                 {"path": "old.py", "before_sha256": "2" * 64, "after_sha256": None}]
        self.assertEqual(self.validate()["change_count"], 2)

    def test_changes_must_change_hash_and_have_at_least_one_side(self):
        for value in (None, "a" * 64):
            self.value["changes"][0].update(before_sha256=value, after_sha256=value)
            self.invalid("plan_unchanged_or_empty_change")

    def test_changes_cannot_traverse_or_include_protected_files(self):
        for path in ("../x.py", "secrets/a.txt", "fake.pem", ".env.example"):
            self.value["changes"][0]["path"] = path
            self.invalid("invalid_relative_path")

    def test_duplicate_or_overlapping_changes(self):
        self.value["changes"].append(dict(self.value["changes"][0], path="DOCS/EXAMPLE.MD"))
        self.invalid("plan_duplicate_or_overlapping_change")
        self.value["changes"][1]["path"] = "docs/example.md/child"
        self.invalid("plan_duplicate_or_overlapping_change")

    def test_source_url_is_metadata_only_and_rejects_credentials_or_query(self):
        for value in ("http://example.invalid", "https://u:p@example.invalid/a", "https://example.invalid/a?token=synthetic",
                      "https://example.invalid/a#secret", "file:///tmp/a", "https://example.invalid:443/a", "https://example.invalid\\x"):
            self.value["sources"][0]["source_url"] = value
            self.invalid("plan_unsafe_source_url")

    def test_source_url_accepts_reserved_host_without_dns(self):
        self.value["sources"][0]["source_url"] = "https://example.invalid/fixed-version"
        self.assertTrue(self.validate()["ok"])

    def test_source_text_is_bounded_and_control_characters_rejected(self):
        for value in ("x\nsecret", "x" * 161, " leading", ""):
            self.value["sources"][0]["publisher"] = value
            self.invalid("plan_invalid_text")

    def test_country_codes_are_claim_syntax_not_inferred_from_domains(self):
        self.value["sources"][0]["source_url"] = "https://example.invalid/a"
        result = self.validate()
        self.assertIn("author_country", result["sources_missing"][0]["missing"])
        self.value["sources"][0]["author_country"] = "unknown"
        self.invalid("plan_invalid_country_claim")

    def test_worker_evidence_cannot_claim_current_process_as_target_image(self):
        self.value["worker"]["scope"] = "current-process"
        self.invalid("plan_invalid_worker_scope")

    def test_worker_evidence_must_bind_exact_ingestion_image(self):
        self.value["worker"]["image_digest"] = "sha256:" + "f" * 64
        self.invalid("plan_worker_image_mismatch")

    def test_all_fixed_worker_check_ids_are_required(self):
        del self.value["worker"]["checks"]["chi_tra"]
        self.invalid("plan_invalid_fields")

    def test_image_tag_is_not_an_image_digest(self):
        self.value["images"][0]["image_digest"] = "anila:latest"
        self.invalid("plan_invalid_image_digest")

    def test_invalid_hashes_and_approval_fields(self):
        self.value["sources"][0]["artifact_sha256"] = "not-a-checksum"
        self.invalid("invalid_checksum")
        self.value = plan_fixture()
        self.value["sources"][0]["approval"]["decision"] = "self-certified"
        self.invalid("plan_invalid_approval")

    def test_duplicate_source_id(self):
        self.value["sources"][1]["id"] = self.value["sources"][0]["id"]
        self.invalid("plan_invalid_source_id")

    def test_empty_source_or_change_list_is_not_complete(self):
        self.value["sources"] = []
        self.invalid("plan_invalid_sources")
        self.value = plan_fixture()
        self.value["changes"] = []
        self.invalid("plan_invalid_changes")

    def test_output_does_not_echo_free_source_details(self):
        self.value["sources"][0].update(publisher="SYNTHETIC_PRIVATE_LABEL", source_url="https://example.invalid/SYNTHETIC_PRIVATE_PATH")
        result = json.dumps(self.validate())
        self.assertNotIn("SYNTHETIC_PRIVATE", result)
        self.assertNotIn("example.invalid", result)

    def test_schema_and_validator_fixed_keys_match(self):
        path = Path(_path).with_name("delivery_plan.schema.json")
        schema = json.loads(path.read_bytes())
        self.assertEqual(set(schema["required"]), set(self.value))
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["properties"]["images"]["items"]["properties"]["id"]["enum"], list(_plan["IMAGES"]))
        checks = schema["properties"]["worker"]["properties"]["checks"]
        self.assertEqual(set(checks["required"]), set(_plan["WORKER_CHECKS"]))

    def test_shipped_example_is_explicitly_incomplete(self):
        self.value = json.loads(Path(_path).with_name("delivery-plan.example.json").read_bytes())
        result = self.validate()
        self.assertFalse(result["metadata_complete"])
        self.assertEqual(len(result["images_missing"]), 16)

    def test_cli_incomplete_exit_two_and_invalid_exit_one(self):
        with tempfile.TemporaryDirectory(prefix="quick-plan-test-", dir="/tmp") as temp:
            target = Path(temp) / "plan.json"
            for value, code in ((self.value, 2), ({"unknown": "field"}, 1), (plan_fixture(complete=True), 0)):
                target.write_text(json.dumps(value))
                stream = io.StringIO()
                with mock.patch.object(sys, "argv", ["delivery_plan.py", "--plan", str(target)]), contextlib.redirect_stdout(stream):
                    self.assertEqual(_plan["main"](), code)
                self.assertIsInstance(json.loads(stream.getvalue()), dict)

    def test_inputs_are_not_mutated_by_validation(self):
        before = copy.deepcopy(self.value)
        self.validate()
        self.assertEqual(self.value, before)

    def test_invalid_cli_arguments_do_not_echo_caller_text(self):
        stream = io.StringIO()
        with mock.patch.object(sys, "argv", ["delivery_plan.py", "--SYNTHETIC_PRIVATE_ARGUMENT"]), contextlib.redirect_stdout(stream):
            with self.assertRaises(SystemExit) as caught:
                _plan["main"]()
        self.assertEqual(caught.exception.code, 1)
        self.assertEqual(json.loads(stream.getvalue()), {"ok": False, "type": "invalid_arguments"})


class SafeResult(unittest.TestResult):
    def __init__(self):
        super().__init__()
        self.nodes = []

    def addSuccess(self, test):
        super().addSuccess(test)
        self.nodes.append({"node": test.id(), "outcome": "passed"})

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.nodes.append({"node": test.id(), "outcome": "failed", "exception_type": err[0].__name__})

    def addError(self, test, err):
        super().addError(test, err)
        self.nodes.append({"node": test.id(), "outcome": "error", "exception_type": err[0].__name__})


if __name__ == "__main__":
    result = SafeResult()
    unittest.defaultTestLoader.loadTestsFromTestCase(DeliveryPlanTests).run(result)
    print(json.dumps({"tests_run": result.testsRun, "passed": sum(row["outcome"] == "passed" for row in result.nodes),
                      "failed": len(result.failures), "errors": len(result.errors), "nodes": result.nodes}, sort_keys=True))
    raise SystemExit(not result.wasSuccessful())
