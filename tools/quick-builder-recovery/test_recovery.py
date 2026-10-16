#!/usr/bin/env python3
# ruff: noqa: E402 -- Load the sibling tool's guard before test imports.
import os
import sys

sys.dont_write_bytecode = True
_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recovery.py")
_tool = {"__name__": "recovery_test_module", "__file__": _path}
with open(_path, "rb") as _input:
    exec(compile(_input.read(), _path, "exec"), _tool)

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="quick-recovery-test-", dir="/tmp")
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.source = self.base / "source"
        self.source.mkdir()
        self.output = self.base / "bundle"
        self.scratch = self.base / "scratch"
        self.scratch.mkdir()
        self.selection = self.base / "selection.json"
        self.rows = [
            self.file("artifact", "artifact1", "reports/報告.pdf", b"synthetic artifact bytes"),
            self.file("ingestion", "source1", "uploads/source.txt", b"synthetic source bytes"),
        ]
        self.save()

    def file(self, category, logical_id, path, data):
        target = self.source / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return {"category": category, "logical_id": logical_id, "path": path,
                "size": len(data), "sha256": _tool["digest"](data)}

    def save(self):
        self.selection.write_bytes(_tool["canonical"]({"schema": _tool["SELECTION_SCHEMA"], "entries": self.rows}))

    def build(self, **kwargs):
        return _tool["backup"](str(self.source), str(self.selection), str(kwargs.get("output", self.output)))

    def verify(self, result):
        return _tool["verify"](str(self.output), result["manifest_sha256"])

    def rehearse(self, result):
        return _tool["rehearse"](str(self.output), result["manifest_sha256"], str(self.scratch))

    def invalid_selection(self, code=None):
        self.save()
        with self.assertRaises(_tool["RecoveryError"]) as caught:
            self.build()
        if code is not None:
            self.assertEqual(str(caught.exception), code)
        self.assertFalse((self.output / "COMPLETE.json").exists())

    def test_round_trip_rechecks_both_categories(self):
        result = self.build()
        self.assertTrue(self.verify(result)["ok"])
        report = self.rehearse(result)
        restored = self.scratch / ("rehearsal-" + result["manifest_sha256"]) / "data"
        for row in self.rows:
            self.assertEqual((restored / row["category"] / row["logical_id"] / row["path"]).read_bytes(),
                             (self.source / row["path"]).read_bytes())
        self.assertEqual(report["file_count"], 2)
        for key in ("database_consistency_verified", "authorization_restored", "worker_readiness_verified", "deployment_ready"):
            self.assertIs(report[key], False)

    def test_bundle_is_deterministic_and_independent_of_selection_order(self):
        first = self.build()
        self.rows.reverse()
        self.save()
        second_path = self.base / "second"
        second = self.build(output=second_path)
        self.assertEqual(first, second)
        for name in ("manifest.json", "COMPLETE.json"):
            self.assertEqual((self.output / name).read_bytes(), (second_path / name).read_bytes())

    def test_duplicate_content_has_one_blob_but_every_source_is_checked(self):
        self.rows[1] = self.file("ingestion", "source1", "uploads/source.txt", b"synthetic artifact bytes")
        self.save()
        result = self.build()
        self.assertEqual(len(list((self.output / "blobs").iterdir())), 1)
        self.assertTrue(self.rehearse(result)["ok"])

    def test_duplicate_content_bad_second_source_is_not_skipped(self):
        self.rows[1].update(size=self.rows[0]["size"], sha256=self.rows[0]["sha256"])
        self.invalid_selection()

    def test_source_checksum_mismatch_leaves_incomplete_bundle(self):
        self.rows[0]["sha256"] = "f" * 64
        self.invalid_selection("content_mismatch")

    def test_source_size_mismatch_is_rejected(self):
        self.rows[0]["size"] += 1
        self.invalid_selection("size_mismatch")

    def test_missing_source_is_not_a_successful_backup(self):
        (self.source / self.rows[0]["path"]).unlink()
        with self.assertRaises(FileNotFoundError):
            self.build()
        self.assertFalse((self.output / "COMPLETE.json").exists())

    def test_missing_expected_hash_or_size_is_rejected(self):
        del self.rows[0]["sha256"]
        self.invalid_selection("invalid_entry")

    def test_checksum_pin_mismatch(self):
        self.build()
        with self.assertRaisesRegex(_tool["RecoveryError"], "manifest_pin_mismatch"):
            _tool["verify"](str(self.output), "0" * 64)

    def test_invalid_checksum_pin(self):
        self.build()
        for pin in ("", "f" * 63, "F" * 64, None):
            with self.subTest(pin=pin), self.assertRaises(_tool["RecoveryError"]):
                _tool["verify"](str(self.output), pin)

    def test_blob_tamper_is_rejected_before_restore_directory_created(self):
        result = self.build()
        blob = self.output / "blobs" / self.rows[0]["sha256"]
        blob.write_bytes(b"X" * self.rows[0]["size"])
        with self.assertRaisesRegex(_tool["RecoveryError"], "content_mismatch"):
            self.rehearse(result)
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_manifest_tamper_cannot_be_hidden_by_rewritten_complete_marker(self):
        result = self.build()
        data = (self.output / "manifest.json").read_bytes() + b" "
        (self.output / "manifest.json").write_bytes(data)
        (self.output / "COMPLETE.json").write_bytes(_tool["canonical"]({
            "schema": "anila.file-backup-complete/v1", "manifest_sha256": _tool["digest"](data)}))
        with self.assertRaisesRegex(_tool["RecoveryError"], "manifest_pin_mismatch"):
            self.verify(result)

    def test_missing_or_invalid_completion_marker(self):
        result = self.build()
        (self.output / "COMPLETE.json").write_bytes(b"{}\n")
        with self.assertRaisesRegex(_tool["RecoveryError"], "incomplete_backup"):
            self.verify(result)

    def test_claim_cannot_be_upgraded_or_retyped(self):
        self.build()
        original = json.loads((self.output / "manifest.json").read_bytes())
        for flag in (True, 0, None, "false"):
            value = dict(original, deployment_ready=flag)
            encoded = _tool["canonical"](value)
            pin = _tool["digest"](encoded)
            (self.output / "manifest.json").write_bytes(encoded)
            (self.output / "COMPLETE.json").write_bytes(_tool["canonical"]({
                "schema": "anila.file-backup-complete/v1", "manifest_sha256": pin}))
            with self.subTest(flag=flag), self.assertRaises(_tool["RecoveryError"]):
                _tool["verify"](str(self.output), pin)

    def test_destination_is_never_overwritten(self):
        self.output.mkdir()
        canary = self.output / "keep.txt"
        canary.write_bytes(b"keep")
        with self.assertRaises(FileExistsError):
            self.build()
        self.assertEqual(canary.read_bytes(), b"keep")

    def test_nonempty_scratch_and_second_rehearsal_are_rejected(self):
        result = self.build()
        self.rehearse(result)
        with self.assertRaisesRegex(_tool["RecoveryError"], "scratch_not_empty"):
            self.rehearse(result)

    def test_source_and_output_roots_cannot_overlap(self):
        for output in (self.source / "bundle", self.base):
            with self.subTest(output=str(output)), self.assertRaisesRegex(_tool["RecoveryError"], "roots_overlap"):
                self.build(output=output)

    def test_bundle_and_scratch_cannot_overlap(self):
        result = self.build()
        with self.assertRaisesRegex(_tool["RecoveryError"], "roots_overlap"):
            _tool["rehearse"](str(self.output), result["manifest_sha256"], str(self.output / "scratch"))

    def test_relative_or_ambiguous_root_is_rejected(self):
        for path in ("relative", "/", "/tmp/../tmp/x", "/tmp//x", "/tmp/x/", "/tmp/.env-example"):
            with self.subTest(path=path), self.assertRaises(_tool["RecoveryError"]):
                _tool["Root"](path)

    def test_protected_and_traversal_paths_fail_before_open(self):
        for path in ("../x", "/x", "a//b", "./x", "x\\y", "x/../y", "x\x00y", "a/.env.test", "Secrets/a", "x.KEY", "x.pem", "logs/a", "a/.npmrc"):
            with self.subTest(path=path), mock.patch.object(os, "open", side_effect=AssertionError("opened")):
                with self.assertRaises(_tool["RecoveryError"]):
                    _tool["relative"](path)

    def test_audit_hook_rejects_protected_files_even_with_direct_open(self):
        for path in (str(self.base / ".env.test"), str(self.base / "secrets" / "value"), str(self.base / "fake.pem")):
            with self.subTest(path=path), self.assertRaises(PermissionError):
                open(path, "rb")

    def test_audit_hook_rejects_network_dns_spawn_and_native_load(self):
        for event in ("socket.__new__", "socket.getaddrinfo", "socket.connect", "subprocess.Popen", "os.system", "os.fork", "ctypes.dlopen"):
            with self.subTest(event=event), self.assertRaises(PermissionError):
                sys.audit(event, "synthetic")

    def test_symlink_source_and_symlink_ancestor_are_rejected(self):
        path = self.source / self.rows[0]["path"]
        path.unlink()
        path.symlink_to(self.source / self.rows[1]["path"])
        with self.assertRaises(OSError):
            self.build()
        linked = self.base / "linked"
        linked.symlink_to(self.source, target_is_directory=True)
        with self.assertRaises(OSError):
            _tool["Root"](str(linked))

    def test_symlink_blob_is_rejected(self):
        result = self.build()
        blob = self.output / "blobs" / self.rows[0]["sha256"]
        blob.unlink()
        blob.symlink_to(self.source / self.rows[0]["path"])
        with self.assertRaises(OSError):
            self.verify(result)

    def test_hardlink_source_is_rejected(self):
        os.link(self.source / self.rows[0]["path"], self.base / "linked.bin")
        with self.assertRaisesRegex(_tool["RecoveryError"], "unsafe_or_oversized_file"):
            self.build()

    def test_hardlink_blob_is_rejected(self):
        result = self.build()
        os.link(self.output / "blobs" / self.rows[0]["sha256"], self.base / "linked.bin")
        with self.assertRaisesRegex(_tool["RecoveryError"], "unsafe_or_oversized_file"):
            self.verify(result)

    def test_fifo_is_rejected_without_blocking(self):
        path = self.source / self.rows[0]["path"]
        path.unlink()
        os.mkfifo(path)
        with self.assertRaisesRegex(_tool["RecoveryError"], "unsafe_or_oversized_file"):
            self.build()

    def test_unknown_category_is_not_backed_up(self):
        self.rows[0]["category"] = "database"
        self.invalid_selection("invalid_category")

    def test_duplicate_casefolded_path_is_rejected(self):
        self.rows[1]["path"] = self.rows[0]["path"].upper()
        self.invalid_selection("duplicate_entry")

    def test_duplicate_casefolded_logical_id_is_rejected(self):
        self.rows[1].update(category="artifact", logical_id="ARTIFACT1")
        self.invalid_selection("duplicate_entry")

    def test_overlapping_file_directory_paths_are_rejected(self):
        self.rows[1]["path"] = self.rows[0]["path"] + "/child"
        self.invalid_selection("overlapping_path")

    def test_invalid_logical_id(self):
        self.rows[0]["logical_id"] = "../escape"
        self.invalid_selection("invalid_logical_id")

    def test_non_normalized_unicode_path_is_rejected(self):
        self.rows[0]["path"] = "cafe\u0301.txt"
        self.invalid_selection("invalid_relative_path")

    def test_twelve_level_source_path_can_be_restored(self):
        self.rows[0] = self.file("artifact", "deep", "/".join(["folder"] * 11 + ["file.txt"]), b"deep")
        self.save()
        self.assertTrue(self.rehearse(self.build())["ok"])

    def test_long_or_deep_paths_are_rejected(self):
        for path in ("x" * 121, "/".join(["a"] * 13)):
            self.rows[0]["path"] = path
            self.invalid_selection("invalid_relative_path")

    def test_bool_negative_and_oversized_size_are_rejected(self):
        for size in (True, -1, _tool["MAX_FILE"] + 1):
            self.rows[0]["size"] = size
            self.invalid_selection("invalid_size")

    def test_selection_file_size_limit(self):
        self.selection.write_bytes(b" " * (_tool["MAX_JSON"] + 1))
        with self.assertRaisesRegex(_tool["RecoveryError"], "unsafe_or_oversized_file"):
            self.build()

    def test_empty_and_too_many_entries_are_rejected(self):
        for count in (0, _tool["MAX_ENTRIES"] + 1):
            value = [self.rows[0]] * count
            with self.subTest(count=count), self.assertRaisesRegex(_tool["RecoveryError"], "invalid_entries"):
                _tool["entries"](value)

    def test_total_size_limit(self):
        rows = [dict(self.rows[0], logical_id="id" + str(i), path=str(i) + ".bin", size=_tool["MAX_FILE"],
                     sha256=f"{i:064x}") for i in range(5)]
        with self.assertRaisesRegex(_tool["RecoveryError"], "total_limit"):
            _tool["entries"](rows)

    def test_inconsistent_size_for_same_blob_is_rejected(self):
        self.rows[1].update(sha256=self.rows[0]["sha256"], size=self.rows[0]["size"] + 1)
        self.invalid_selection("inconsistent_blob_size")

    def test_unknown_fields_and_duplicate_json_keys_are_rejected(self):
        self.rows[0]["user_id"] = "not-trusted"
        self.invalid_selection("invalid_entry")
        with self.assertRaisesRegex(_tool["RecoveryError"], "duplicate_json_key"):
            _tool["decode"](b'{"entries":[],"entries":[]}')

    def test_invalid_json_is_bounded_and_rejected(self):
        for value in (b"{", b"\xff", b"NaN", b"[" * 2000):
            with self.subTest(length=len(value)), self.assertRaises(_tool["RecoveryError"]):
                _tool["decode"](value)

    def test_input_changed_during_copy_is_rejected(self):
        original = _tool["stable"]
        def changed(handle, before):
            if before.st_size == self.rows[0]["size"]:
                path = self.source / self.rows[0]["path"]
                path.write_bytes(b"X" * before.st_size)
                # Some test filesystems coarsen immediate write timestamps.
                os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 2_000_000_000))
            return original(handle, before)
        with mock.patch.dict(_tool, stable=changed), self.assertRaisesRegex(_tool["RecoveryError"], "input_changed"):
            self.build()
        self.assertFalse((self.output / "COMPLETE.json").exists())

    def test_source_growth_after_open_is_bounded(self):
        original = _tool["Root"].open_read
        def grow(root, path, maximum):
            result = original(root, path, maximum)
            if path == self.rows[0]["path"]:
                with (self.source / path).open("ab") as target:
                    target.write(b"new bytes")
            return result
        with mock.patch.object(_tool["Root"], "open_read", grow), self.assertRaisesRegex(_tool["RecoveryError"], "input_changed"):
            self.build()
        self.assertFalse((self.output / "COMPLETE.json").exists())

    def test_failure_during_restore_has_no_success_marker(self):
        result = self.build()
        original = _tool["copy_checked"]
        def fail_on_restore(source, path, row, output=None, output_path=None):
            if output_path and output_path.startswith("data/"):
                _tool["reject"]("content_mismatch")
            return original(source, path, row, output, output_path)
        with mock.patch.dict(_tool, copy_checked=fail_on_restore), self.assertRaises(_tool["RecoveryError"]):
            self.rehearse(result)
        self.assertFalse(any(self.scratch.glob("*/REHEARSAL.json")))

    def test_blob_changed_after_initial_verify_is_rechecked_during_restore(self):
        result = self.build()
        original = _tool["verify_root"]
        def tamper(root, expected):
            value = original(root, expected)
            (self.output / "blobs" / self.rows[0]["sha256"]).write_bytes(b"X" * self.rows[0]["size"])
            return value
        with mock.patch.dict(_tool, verify_root=tamper), self.assertRaisesRegex(_tool["RecoveryError"], "content_mismatch"):
            self.rehearse(result)
        self.assertFalse(any(self.scratch.glob("*/REHEARSAL.json")))

    def test_empty_file_is_an_explicit_file_not_a_document_parsing_claim(self):
        self.rows[0] = self.file("artifact", "empty", "reports/empty.bin", b"")
        self.save()
        self.assertTrue(self.rehearse(self.build())["ok"])

    def test_cli_returns_safe_code_without_path_or_exception_details(self):
        stream = io.StringIO()
        with mock.patch.object(sys, "argv", ["recovery.py", "verify", "--bundle", str(self.base / "absent"),
                                           "--manifest-sha256", "f" * 64]), contextlib.redirect_stdout(stream):
            self.assertEqual(_tool["main"](), 1)
        result = json.loads(stream.getvalue())
        self.assertEqual(result, {"ok": False, "type": "unsafe_or_unavailable_input"})
        self.assertNotIn(str(self.base), stream.getvalue())

    def test_cli_backup_verify_rehearse_round_trip(self):
        def command(arguments):
            stream = io.StringIO()
            with mock.patch.object(sys, "argv", ["recovery.py", *arguments]), contextlib.redirect_stdout(stream):
                self.assertEqual(_tool["main"](), 0)
            return json.loads(stream.getvalue())
        result = command(["backup", "--source-root", str(self.source), "--selection", str(self.selection), "--out", str(self.output)])
        common = ["--bundle", str(self.output), "--manifest-sha256", result["manifest_sha256"]]
        self.assertTrue(command(["verify", *common])["ok"])
        self.assertTrue(command(["rehearse", *common, "--scratch", str(self.scratch)])["ok"])

    def test_invalid_cli_arguments_do_not_echo_caller_text(self):
        stream = io.StringIO()
        with mock.patch.object(sys, "argv", ["recovery.py", "SYNTHETIC_PRIVATE_ARGUMENT"]), contextlib.redirect_stdout(stream):
            with self.assertRaises(SystemExit) as caught:
                _tool["main"]()
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
    unittest.defaultTestLoader.loadTestsFromTestCase(RecoveryTests).run(result)
    print(json.dumps({"tests_run": result.testsRun, "passed": sum(row["outcome"] == "passed" for row in result.nodes),
                      "failed": len(result.failures), "errors": len(result.errors), "nodes": result.nodes}, sort_keys=True))
    raise SystemExit(not result.wasSuccessful())
