#!/usr/bin/env python3
"""Exercise extracted updater copy/restore functions on private synthetic files."""

import os
import sys


def audit_guard(event, args):
    if event == "open" and args and isinstance(args[0], (str, bytes, os.PathLike)):
        parts = os.fsdecode(args[0]).replace("\\", "/").split("/")
        if any(part.startswith(".env") or part in {
            "secrets", "secret", "keys", "credentials", ".aws", ".ssh", "auth",
        } for part in parts) or parts[-1].endswith((".pem", ".key", ".p12", ".pfx")):
            raise PermissionError("protected_path")
    if event in {"socket.connect", "socket.bind", "socket.getaddrinfo", "os.system",
                 "os.exec", "os.posix_spawn", "ctypes.dlopen"}:
        raise PermissionError("external_entry")
    if event == "subprocess.Popen" and args[0] != "/usr/bin/bash":
        raise PermissionError("unexpected_child")


sys.addaudithook(audit_guard)

import argparse
import hashlib
import json
from pathlib import Path
import re
import resource
import subprocess
import tempfile
import unittest


FUNCTION_NAMES = (
    "share_snapshot_names", "_copy_tree", "stage_share_files", "commit_share_files",
    "revert_share_files", "cleanup_share_staging", "restore_share_snapshot",
)
EVENT_NAMES = {
    "hardlink", "fallback", "hardlink_failed", "fallback_failed", "stage_studio",
    "commit_studio", "revert_studio", "cleanup_studio", "unexpected_command",
    "outside_path", "unexpected_argv",
}
SOURCE = None
EXTRACTED = ""
SOURCE_SHA256 = ""
FUNCTION_SHA256 = {}
OBSERVATIONS = []


def extract_functions(path):
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    functions = []
    for name in FUNCTION_NAMES:
        matches = list(re.finditer(r"^" + re.escape(name) + r"\(\) \{\n.*?^\}\n",
                                   text, re.M | re.S))
        if len(matches) != 1:
            raise ValueError("ambiguous_function")
        functions.append(matches[0].group())
    return raw, functions


# PATH is an empty private directory. Only the wrappers below can reach fixed
# filesystem utilities; each path argument must stay inside this case directory.
# Studio hooks are explicit stubs, not simulated volume/Docker acceptance.
BASH_BOUNDARY = r'''
set -euo pipefail
umask 077
event() { printf '%s\n' "$1" >> "$CASE_DIR/events"; }
owned() {
  [[ "$1" == "$CASE_DIR/"* && "$1" != *'/../'* && "$1" != *'/./'* ]] \
    || { event outside_path; return 97; }
}
bad_argv() { event unexpected_argv; return 97; }
command_not_found_handle() { event unexpected_command; return 97; }
install_root() { printf '%s\n' "$CASE_DIR/root"; }
stage_studio_volume() { owned "$1" || return; event stage_studio; }
commit_studio_volume() { owned "$1" || return; event commit_studio; }
revert_studio_volume() { owned "$1" || return; event revert_studio; }
cleanup_studio_volume() { owned "$1" || return; event cleanup_studio; }
dirname() {
  [[ "$#" == 1 || "$#" == 2 && "$1" == -- ]] || { bad_argv; return; }
  owned "${!#}" || return
  /usr/bin/dirname "$@"
}
mkdir() {
  [[ "$#" == 2 && "$1" == -p ]] || { bad_argv; return; }
  owned "$2" || return
  /usr/bin/mkdir "$@"
}
chmod() {
  [[ "$#" == 2 && ( "$1" == 600 || "$1" == 700 ) ]] || { bad_argv; return; }
  owned "$2" || return
  /usr/bin/chmod "$@"
}
mktemp() {
  [[ "$#" == 2 && "$1" == -d && "$2" == "$CASE_DIR/root/state/restore-stage/stage.XXXXXX" ]] \
    || { bad_argv; return; }
  /usr/bin/mktemp "$@"
}
rm() {
  [[ "$#" == 2 || "$#" == 3 && "$2" == -- ]] || { bad_argv; return; }
  [[ "$1" == -f || "$1" == -rf ]] || { bad_argv; return; }
  owned "${!#}" || return
  /usr/bin/rm "$@"
}
mv() {
  [[ "$#" == 2 ]] || { bad_argv; return; }
  owned "$1" && owned "$2" || return
  /usr/bin/mv "$@"
}
grep() {
  [[ "$#" == 3 && "$1" == -q && "$2" == 'Invalid cross-device link' ]] \
    || { bad_argv; return; }
  owned "$3" || return
  /usr/bin/grep "$@"
}
cat() {
  [[ "$#" == 1 ]] || { bad_argv; return; }
  owned "$1" || return
  /usr/bin/cat "$@"
}
tr() {
  [[ "$#" == 2 && "$1" == -d && "$2" == '[:space:]' ]] || { bad_argv; return; }
  /usr/bin/tr "$@"
}
diff() {
  [[ "$#" == 3 && "$1" == -rq ]] || { bad_argv; return; }
  owned "$2" && owned "$3" || return
  /usr/bin/diff "$@"
}
cp() {
  [[ "$#" == 3 && ( "$1" == -al || "$1" == -a ) ]] || { bad_argv; return; }
  owned "$2" && owned "$3" || return
  local inject=0
  if [[ "$MODE" == exdev_success || "$MODE" == exdev_failure ]]; then inject=1; fi
  if [[ "$MODE" == commit_failure && "$3" == "$CASE_DIR/root/state/share/uploads" ]]; then inject=1; fi
  if [[ "$1" == -al ]]; then
    event hardlink
    if [[ "$MODE" == hardlink_failure ]]; then
      event hardlink_failed
      printf 'Permission denied\n' >&2
      return 72
    fi
    if [[ "$inject" == 1 ]]; then
      printf 'Invalid cross-device link\n' >&2
      return 71
    fi
  else
    event fallback
    if [[ "$MODE" == exdev_failure || "$MODE" == commit_failure ]]; then
      /usr/bin/mkdir -p "$3"
      printf 'SYNTHETIC_PARTIAL_COPY\n' > "$3/partial.txt"
      event fallback_failed
      return 73
    fi
  fi
  /usr/bin/cp "$@"
}
'''


def child_limits():
    resource.setrlimit(resource.RLIMIT_CPU, (5, 5))
    resource.setrlimit(resource.RLIMIT_AS, (128 * 1024 * 1024,) * 2)
    resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (1024 * 1024,) * 2)


class CopyTreeTests(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory(prefix="qb-copy-tree-", dir="/tmp")
        self.addCleanup(self.workspace.cleanup)
        self.root = Path(self.workspace.name)
        (self.root / "empty-path").mkdir()
        (self.root / "events").write_bytes(b"")

    def run_function(self, mode, entry):
        env = {"PATH": str(self.root / "empty-path"), "LANG": "C", "LC_ALL": "C",
               "CASE_DIR": str(self.root), "MODE": mode, "TMPDIR": str(self.root)}
        if entry == "direct":
            (self.root / "root/state").mkdir(parents=True)
            (self.root / "source").mkdir()
            (self.root / "source/item.txt").write_bytes(b"SYNTHETIC_SOURCE\n")
            command = '_copy_tree "$CASE_DIR/source" "$CASE_DIR/destination"'
        else:
            live = self.root / "root/state/share/uploads"
            live.mkdir(parents=True)
            (live / "live.txt").write_bytes(b"SYNTHETIC_LIVE_BEFORE\n")
            snapshot = self.root / "backup/files/uploads"
            snapshot.mkdir(parents=True)
            (snapshot / "snapshot.txt").write_bytes(b"SYNTHETIC_BACKUP\n")
            if entry == "commit":
                stage = self.root / "stage/uploads"
                stage.mkdir(parents=True)
                (stage / "snapshot.txt").write_bytes(b"SYNTHETIC_BACKUP\n")
                (self.root / "backup/db.dump.share-stage").write_text(str(stage.parent) + "\n")
                command = 'commit_share_files "$CASE_DIR/backup/db.dump"'
            else:
                command = 'restore_share_snapshot "$CASE_DIR/backup/db.dump"'
        program = BASH_BOUNDARY + "\n" + EXTRACTED + "\n" + (
            'function_rc=0\nif ' + command
            + '; then function_rc=0; else function_rc=$?; fi\n'
            + 'printf "FUNCTION_RC=%s\\n" "$function_rc"\n'
        )
        completed = subprocess.run(
            ["/usr/bin/bash", "--noprofile", "--norc", "-c", program],
            env=env, cwd=self.root, stdin=subprocess.DEVNULL, capture_output=True,
            timeout=10, preexec_fn=child_limits, check=False,
        )
        self.assertEqual(completed.returncode, 0, "shell_boundary_failed")
        lines = completed.stdout.decode("ascii").splitlines()
        result_lines = [line for line in lines if re.fullmatch(r"FUNCTION_RC=[0-9]+", line)]
        self.assertEqual(len(result_lines), 1, "missing_function_status")
        events = (self.root / "events").read_text().splitlines()
        self.assertTrue(all(event in EVENT_NAMES for event in events), "unknown_event")
        self.assertFalse(set(events) & {"unexpected_command", "outside_path", "unexpected_argv"},
                         "command_boundary_violation")
        rc = int(result_lines[0].split("=")[1])
        OBSERVATIONS.append({"node": self.id().split(".")[-1], "entry": entry,
                             "mode": mode, "function_exit": rc, "events": events,
                             "stderr_bytes": len(completed.stderr)})
        return rc, events

    def test_hardlink_copy_success(self):
        rc, events = self.run_function("hardlink_success", "direct")
        self.assertEqual(rc, 0, "hardlink_success_rejected")
        self.assertEqual((self.root / "destination/item.txt").read_bytes(), b"SYNTHETIC_SOURCE\n")
        self.assertNotIn("fallback", events)

    def test_exdev_fallback_success(self):
        rc, events = self.run_function("exdev_success", "direct")
        self.assertEqual(rc, 0, "fallback_success_rejected")
        self.assertEqual((self.root / "destination/item.txt").read_bytes(), b"SYNTHETIC_SOURCE\n")
        self.assertEqual(events, ["hardlink", "fallback"])

    def test_non_exdev_failure_does_not_fallback(self):
        rc, events = self.run_function("hardlink_failure", "direct")
        self.assertNotEqual(rc, 0, "hardlink_failure_hidden")
        self.assertNotIn("fallback", events)
        self.assertFalse((self.root / "destination").exists())

    def test_exdev_fallback_failure_propagates(self):
        rc, events = self.run_function("exdev_failure", "direct")
        self.assertIn("fallback_failed", events)
        self.assertNotEqual(rc, 0, "fallback_failure_hidden")

    def test_commit_failure_restores_live_and_keeps_snapshot(self):
        rc, events = self.run_function("commit_failure", "commit")
        self.assertIn("fallback_failed", events)
        self.assertNotEqual(rc, 0, "commit_failure_hidden")
        self.assertEqual((self.root / "root/state/share/uploads/live.txt").read_bytes(),
                         b"SYNTHETIC_LIVE_BEFORE\n")
        self.assertFalse((self.root / "root/state/share/uploads/partial.txt").exists())
        self.assertEqual((self.root / "backup/files/uploads/snapshot.txt").read_bytes(),
                         b"SYNTHETIC_BACKUP\n")
        self.assertTrue((self.root / "stage/uploads/snapshot.txt").is_file())
        self.assertNotIn("commit_studio", events)
        self.assertNotIn("cleanup_studio", events)

    def test_restore_commit_failure_has_no_success_cleanup(self):
        rc, events = self.run_function("commit_failure", "restore")
        self.assertIn("fallback_failed", events)
        self.assertNotEqual(rc, 0, "restore_failure_hidden")
        self.assertEqual((self.root / "root/state/share/uploads/live.txt").read_bytes(),
                         b"SYNTHETIC_LIVE_BEFORE\n")
        self.assertEqual((self.root / "backup/files/uploads/snapshot.txt").read_bytes(),
                         b"SYNTHETIC_BACKUP\n")
        self.assertIn("revert_studio", events)
        self.assertNotIn("cleanup_studio", events)
        self.assertNotIn("commit_studio", events)

    def test_restore_success_cleans_staging_after_copy(self):
        rc, events = self.run_function("hardlink_success", "restore")
        self.assertEqual(rc, 0, "restore_success_rejected")
        self.assertEqual((self.root / "root/state/share/uploads/snapshot.txt").read_bytes(),
                         b"SYNTHETIC_BACKUP\n")
        self.assertFalse((self.root / "root/state/share/uploads/live.txt").exists())
        self.assertFalse((self.root / "backup/db.dump.share-stage").exists())
        self.assertIn("commit_studio", events)
        self.assertIn("cleanup_studio", events)


class SafeResult(unittest.TestResult):
    def __init__(self):
        super().__init__()
        self.nodes = []

    def addSuccess(self, test):
        super().addSuccess(test)
        self.nodes.append({"node": test.id(), "outcome": "passed"})

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.nodes.append({"node": test.id(), "outcome": "failed", "type": err[0].__name__})

    def addError(self, test, err):
        super().addError(test, err)
        self.nodes.append({"node": test.id(), "outcome": "error", "type": err[0].__name__})


def main():
    global SOURCE, EXTRACTED, SOURCE_SHA256, FUNCTION_SHA256
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--script", type=Path,
                        default=Path(__file__).resolve().parents[1] / "anila-update.sh")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    suite_path = Path(__file__)
    suite_sha256 = hashlib.sha256(suite_path.read_bytes()).hexdigest()
    SOURCE = args.script
    raw, functions = extract_functions(SOURCE)
    SOURCE_SHA256 = hashlib.sha256(raw).hexdigest()
    FUNCTION_SHA256 = {name: hashlib.sha256(body.encode()).hexdigest()
                       for name, body in zip(FUNCTION_NAMES, functions)}
    EXTRACTED = "\n".join(functions)
    result = SafeResult()
    unittest.defaultTestLoader.loadTestsFromTestCase(CopyTreeTests).run(result)
    report = {"schema": "anila.updater-copy-tree-tests/v1", "source_sha256": SOURCE_SHA256,
              "suite_sha256": suite_sha256,
              "function_sha256": FUNCTION_SHA256, "tests_run": result.testsRun,
              "passed": sum(row["outcome"] == "passed" for row in result.nodes),
              "failed": len(result.failures), "errors": len(result.errors),
              "nodes": result.nodes, "observations": OBSERVATIONS,
              "full_updater_sourced": False, "DB_Docker_network_used": False,
              "scope": "Real extracted file functions; cp failure and Studio hooks are stubs; no actual EXDEV mount or volume/DB restore.",
              "source_unchanged": hashlib.sha256(SOURCE.read_bytes()).hexdigest() == SOURCE_SHA256,
              "suite_unchanged": hashlib.sha256(suite_path.read_bytes()).hexdigest() == suite_sha256}
    if args.report:
        with args.report.open("x", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    print(json.dumps({name: report[name] for name in ("tests_run", "passed", "failed", "errors", "source_unchanged")}))
    return 0 if result.wasSuccessful() and report["source_unchanged"] and report["suite_unchanged"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
