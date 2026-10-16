#!/usr/bin/env python3
"""Run restore script copies against builtin-only command stubs, never Docker."""
import os
import sys

sys.dont_write_bytecode = True
_active_child = None
_active_bin = None


def _guard(event, args):
    if event.startswith(("socket.", "ctypes.dlopen", "os.system", "os.exec", "os.spawn")):
        raise PermissionError("network and native execution disabled")
    if event == "subprocess.Popen":
        executable, argv, cwd, env = args
        if (executable != "/bin/bash" or len(argv) != 2 or argv[1] != _active_child
                or cwd != "/tmp" or env.get("PATH") != _active_bin
                or "HOME" in env or "BASH_ENV" in env or "ENV" in env):
            raise PermissionError("only the controlled script copy may run")
    if event == "open" and isinstance(args[0], (str, bytes)):
        value = os.fsdecode(args[0]).replace("\\", "/")
        parts = value.casefold().split("/")
        if any(p.startswith(".env") or p in {"secrets", "keys", "auth", "credentials", ".git", ".ssh", ".aws", ".npmrc"}
               or p.endswith((".pem", ".key", ".log")) for p in parts):
            raise PermissionError("protected path")


sys.addaudithook(_guard)

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[4]
SOURCE_ROOT = REPO
ID = "sha256:" + "a" * 64
OTHER_ID = "sha256:" + "b" * 64
DEFAULT_IMAGE = "anila-pgvector:local"
SCRIPTS = Path("infra/deployment/scripts")
SAFE_TRACES = []

STUB = r'''record() {
printf '%s' "$1" >> "$STUB_EVENTS"
shift
local arg
for arg in "$@"; do arg=${arg//$'\n'/\\n}; arg=${arg//$'\t'/\\t}; printf '\t%s' "$arg" >> "$STUB_EVENTS"; done
printf '\n' >> "$STUB_EVENTS"
}
_stub() {
local name=$1
shift
case "$name" in
  docker)
    if [[ "${1:-}" == image && "${2:-}" == inspect ]]; then
      record image-inspect "$@"
      [[ "$STUB_MODE" != missing ]] || return 1
      case "$STUB_MODE" in
        malformed) printf 'not-an-image-id\n';;
        empty) printf '\n';;
        multiline) printf '%s\n%s\n' "$STUB_ID" "$STUB_ID";;
        *) printf '%s\n' "$STUB_ID";;
      esac
    elif [[ "${1:-}" == inspect ]]; then
      record container-inspect "$@"
    elif [[ "${1:-}" == exec ]]; then
      record write-db "$@"
    elif [[ "${1:-}" == run ]]; then
      record helper-run "$@"
      if [[ "$STUB_MODE" == retag ]]; then
        for arg in "$@"; do
          if [[ "$arg" == anila-pgvector:local ]]; then
            record retargeted-to "$STUB_OTHER_ID"
          fi
        done
      fi
    else
      record UNEXPECTED docker "$@"; return 97
    fi
    ;;
  date) printf 'synthetic-time\n';;
  dirname) value=${1%/*}; printf '%s\n' "${value:-.}";;
  basename) printf '%s\n' "${1##*/}";;
  dd) printf PGDMP;;
  mktemp)
    record write-tmp "$@"
    case "$*" in
      *.out) printf '%s\n' "$STUB_ROOT/restore.out";;
      *.err) printf '%s\n' "$STUB_ROOT/restore.err";;
      *) printf '%s\n' "$STUB_ROOT/work";;
    esac
    ;;
  cp|mkdir|rm) record "write-$name" "$@";;
  tar)
    if [[ "${1:-}" == -tf ]]; then record tar-list "$@";
    elif [[ "${1:-}" == -xzf ]]; then record write-files "$@";
    else record UNEXPECTED tar "$@"; return 97; fi
    ;;
  pg_restore) record host-list "$@";;
  grep) return 1;;
  bash)
    [[ "$#" == 1 && "$1" == "$STUB_CHILD" ]] || { record UNEXPECTED bash "$@"; return 97; }
    record child-script "$@"
    /bin/bash "$1"
    ;;
  *) record UNEXPECTED "$name" "$@"; return 97;;
esac
}
'''


class RestoreOfflineTests(unittest.TestCase):
    def run_script(self, script, *, mode="valid", host_list=True, skip_db=False,
                   skip_volumes=False, image=None):
        global _active_child, _active_bin
        with tempfile.TemporaryDirectory(prefix="restore-offline-test-", dir="/tmp") as name:
            base = Path(name)
            target = base / "repo" / SCRIPTS
            target.mkdir(parents=True)
            (target / "assert-db-ownership.sql").write_text("-- synthetic placeholder; never executed by a database\n")
            snapshot = base / "snapshot"
            snapshot.mkdir()
            (snapshot / "db.dump").write_bytes(b"PGDMP synthetic, never parsed by Postgres")
            for item in ("uploads", "attachments", "static", "pki", "quickstart", "studio-artifacts", "router-sessions", "n8n"):
                (snapshot / ("files-" + item + ".tar")).write_bytes(b"synthetic archive, command stub only")
            (base / "work").mkdir()
            (base / "restore.out").touch()
            (base / "restore.err").touch()
            bindir = base / "bin"
            bindir.mkdir()
            commands = ["docker", "date", "dirname", "basename", "dd", "mktemp", "cp", "mkdir", "rm", "tar", "grep", "bash"]
            if host_list:
                commands.append("pg_restore")
            prefix = STUB + "\n".join(command + '() { _stub ' + command + ' "$@"; }' for command in commands) + "\n"
            sources = []
            for filename in ("restore-csp-db.sh", "restore-all.sh"):
                data = (SOURCE_ROOT / SCRIPTS / filename).read_bytes()
                original = data.decode()
                (target / filename).write_text(prefix + original)
                sources.append({"path": str(SCRIPTS / filename), "sha256": hashlib.sha256(data).hexdigest()})
            events = base / "events.safe.txt"
            events.touch()
            env = {"PATH": str(bindir), "LANG": "C", "STUB_EVENTS": str(events),
                   "STUB_ROOT": str(base), "STUB_CHILD": str(target / "restore-csp-db.sh"),
                   "STUB_MODE": mode, "STUB_ID": ID, "STUB_OTHER_ID": OTHER_ID,
                   "ANILA_RESTORE_CONTAINER": "synthetic-pg", "ANILA_RESTORE_DUMP": str(snapshot / "db.dump"),
                   "ANILA_RESTORE_SNAPSHOT": str(snapshot), "ANILA_RESTORE_CONFIRM": "yes",
                   "ANILA_SHARE_ROOT": str(base / "share")}
            if skip_db:
                env["ANILA_RESTORE_SKIP_DB"] = "1"
            if skip_volumes:
                env["ANILA_RESTORE_SKIP_VOLUMES"] = "1"
            if image is not None:
                env["ANILA_BACKUP_IMAGE"] = image
            _active_child, _active_bin = str(target / script), str(bindir)
            try:
                result = subprocess.run(["/bin/bash", _active_child], cwd="/tmp", env=env,
                                        capture_output=True, text=True, timeout=8)
            finally:
                _active_child = _active_bin = None
            rows = [line.split("\t") for line in events.read_text().splitlines()]
            SAFE_TRACES.append({"case": self.id(), "script": script, "mode": mode,
                                "host_pg_restore": host_list, "skip_db": skip_db, "skip_volumes": skip_volumes,
                                "exit_code": result.returncode, "sources": sources,
                                "mutation_commands": [r for r in rows if r[0].startswith("write-")],
                                "pg_restore_exec_commands": [r for r in rows if r[0] == "helper-run" and "--list" not in r and "pg_restore" in r],
                                "helper_commands": [r for r in rows if r[0] == "helper-run"],
                                "image_inspect_commands": [r for r in rows if r[0] == "image-inspect"],
                                "retargeted_commands": [r for r in rows if r[0] == "retargeted-to"],
                                "unexpected_commands": [r for r in rows if r[0] == "UNEXPECTED"],
                                "tool_execution": "Bash functions only; PATH is an empty temporary directory; only exact child script may use /bin/bash."})
            self.assertFalse(any(row[0] == "UNEXPECTED" for row in rows), rows)
            return result.returncode, rows, result.stdout + result.stderr

    def assert_no_writes(self, rows):
        self.assertFalse([row for row in rows if row[0].startswith("write-") or row[0] in {"helper-run", "child-script"}], rows)

    def assert_helpers(self, rows):
        runs = [row[1:] for row in rows if row[0] == "helper-run"]
        self.assertTrue(runs, rows)
        for argv in runs:
            self.assertIn(ID, argv, argv)
            self.assertNotIn(DEFAULT_IMAGE, argv, argv)
            self.assertNotIn("pgvector/pgvector:pg16", argv, argv)
            self.assertEqual(argv[argv.index("--pull") + 1], "never", argv)
            network = argv[argv.index("--network") + 1]
            if "--entrypoint" in argv and argv[argv.index("--entrypoint") + 1] == "tar":
                self.assertEqual(network, "none")
            elif "--list" in argv:
                self.assertEqual(network, "none")
            else:
                self.assertEqual(network, "container:synthetic-pg")
        return runs

    def test_db_missing_image_precedes_all_writes(self):
        rc, rows, _ = self.run_script("restore-csp-db.sh", mode="missing")
        self.assertNotEqual(rc, 0)
        self.assert_no_writes(rows)

    def test_db_rejects_malformed_image_id(self):
        for mode in ("malformed", "empty", "multiline"):
            with self.subTest(mode=mode):
                rc, rows, _ = self.run_script("restore-csp-db.sh", mode=mode)
                self.assertNotEqual(rc, 0)
                self.assert_no_writes(rows)

    def test_db_uses_local_immutable_id_and_never_pull(self):
        rc, rows, _ = self.run_script("restore-csp-db.sh")
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.assert_helpers(rows)), 1)
        inspected = next(row for row in rows if row[0] == "image-inspect")
        self.assertEqual(inspected[-1], DEFAULT_IMAGE)

    def test_db_accepts_explicit_existing_image_id(self):
        rc, rows, _ = self.run_script("restore-csp-db.sh", image=ID)
        self.assertEqual(rc, 0)
        self.assert_helpers(rows)
        self.assertEqual(next(row for row in rows if row[0] == "image-inspect")[-1], ID)

    def test_db_tag_change_cannot_retarget_helper(self):
        rc, rows, _ = self.run_script("restore-csp-db.sh", mode="retag")
        self.assertEqual(rc, 0)
        self.assert_helpers(rows)
        self.assertFalse([r for r in rows if r[0] == "retargeted-to"])

    def test_all_missing_volume_helper_precedes_db_and_file_writes(self):
        rc, rows, _ = self.run_script("restore-all.sh", mode="missing")
        self.assertNotEqual(rc, 0)
        self.assert_no_writes(rows)

    def test_all_missing_list_helper_precedes_writes_even_when_restores_skipped(self):
        rc, rows, _ = self.run_script("restore-all.sh", mode="missing", host_list=False,
                                      skip_db=True, skip_volumes=True)
        self.assertNotEqual(rc, 0)
        self.assert_no_writes(rows)

    def test_all_rejects_malformed_image_id_before_writes(self):
        rc, rows, _ = self.run_script("restore-all.sh", mode="malformed")
        self.assertNotEqual(rc, 0)
        self.assert_no_writes(rows)

    def test_all_helpers_are_pinned_pull_never_with_required_network(self):
        rc, rows, _ = self.run_script("restore-all.sh", host_list=False)
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.assert_helpers(rows)), 5)

    def test_all_host_list_does_not_add_container_list(self):
        rc, rows, _ = self.run_script("restore-all.sh", host_list=True)
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.assert_helpers(rows)), 4)
        self.assertEqual(len([r for r in rows if r[0] == "host-list"]), 1)

    def test_all_tag_change_preserves_same_id_in_child_and_volume_helpers(self):
        rc, rows, _ = self.run_script("restore-all.sh", mode="retag", host_list=False)
        self.assertEqual(rc, 0)
        self.assert_helpers(rows)
        inspected = [row[-1] for row in rows if row[0] == "image-inspect"]
        self.assertEqual(inspected, [DEFAULT_IMAGE, ID])
        self.assertFalse([r for r in rows if r[0] == "retargeted-to"])

    def test_all_skip_db_keeps_file_helpers_offline(self):
        rc, rows, _ = self.run_script("restore-all.sh", skip_db=True)
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.assert_helpers(rows)), 3)
        self.assertFalse([row for row in rows if row[0] == "write-db"])

    def test_all_skip_volumes_keeps_db_helper_pinned(self):
        rc, rows, _ = self.run_script("restore-all.sh", skip_volumes=True)
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.assert_helpers(rows)), 1)

    def test_all_does_not_require_unused_image(self):
        rc, rows, _ = self.run_script("restore-all.sh", mode="missing", skip_db=True, skip_volumes=True)
        self.assertEqual(rc, 0)
        self.assertFalse([r for r in rows if r[0] in {"image-inspect", "helper-run", "write-db"}])

    def test_release_version_docs_match_tool_format_version(self):
        tool = (SOURCE_ROOT / "tools/quick-builder-release/release.py").read_text()
        version = re.search(r'^TOOL_VERSION = "([^"]+)"', tool, re.M).group(1)
        readme = (SOURCE_ROOT / "tools/quick-builder-release/README.md").read_text()
        guide = (SOURCE_ROOT / "docs/guides/quick-builder-offline-delivery.md").read_text()
        self.assertIn("`release.py` " + version, readme)
        self.assertIn("工具版 " + version + "、schema v1", guide)


class SafeResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.safe_nodes = []

    def addSuccess(self, test):
        super().addSuccess(test)
        self.safe_nodes.append({"node": test.id(), "outcome": "passed"})

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.safe_nodes.append({"node": test.id(), "outcome": "failed", "error_type": err[0].__name__})

    def addError(self, test, err):
        super().addError(test, err)
        self.safe_nodes.append({"node": test.id(), "outcome": "error", "error_type": err[0].__name__})

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        self.safe_nodes.append({"node": subtest.id(), "outcome": "passed" if err is None else "failed",
                                "kind": "subtest"})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=REPO)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    SOURCE_ROOT = args.source_root.resolve()
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(RestoreOfflineTests)
    result = unittest.TextTestRunner(verbosity=2, resultclass=SafeResult).run(suite)
    if args.report:
        failed_cases = {row["node"].split(" (", 1)[0] for row in result.safe_nodes if row["outcome"] != "passed"}
        args.report.write_text(json.dumps({"scope": "14 controlled full-script-copy Bash dynamic cases and one static version-doc case. No main script sourced, real Docker, DB, network or services.",
                                          "source_root": str(SOURCE_ROOT), "test_cases_run": result.testsRun,
                                          "test_cases_passed": result.testsRun - len(failed_cases), "test_cases_failed": len(failed_cases),
                                          "failure_records": len(result.failures), "errors": len(result.errors),
                                          "subtests_separate": True, "nodes": result.safe_nodes,
                                          "dynamic_invocations": SAFE_TRACES}, ensure_ascii=False, indent=2) + "\n")
    raise SystemExit(0 if result.wasSuccessful() else 1)
