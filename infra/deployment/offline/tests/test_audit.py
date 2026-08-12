from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


TESTS_ROOT = Path(__file__).resolve().parent
AUDIT_TOOL = TESTS_ROOT.parent / "audit-wheelhouse.py"
FIXTURES = TESTS_ROOT / "fixtures"


def run_audit(house: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(AUDIT_TOOL), str(house), *extra],
        capture_output=True,
        text=True,
        check=False,
    )


class AuditWheelhouseTests(unittest.TestCase):
    def test_clean_house_with_covering_manifest_passes(self) -> None:
        result = run_audit(FIXTURES / "clean")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("2 wheels", result.stdout)

    def test_write_manifest_uses_collected_wheels(self) -> None:
        with TemporaryDirectory() as temporary:
            manifest = Path(temporary) / "csp.freeze.txt"
            result = run_audit(FIXTURES / "clean", "--write-manifest", str(manifest))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(manifest.read_text(encoding="utf-8"), "alpha==1.0\nbravo==2.0\n")

    def test_two_versions_with_covering_manifests_pass(self) -> None:
        result = run_audit(FIXTURES / "duplicate-version")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("2 wheels", result.stdout)

    def test_tarball_fails(self) -> None:
        result = run_audit(FIXTURES / "tarball")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("*.tar.gz", result.stderr)

    def test_empty_house_fails(self) -> None:
        with TemporaryDirectory() as temporary:
            result = run_audit(Path(temporary))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no wheels", result.stderr)

    def test_manifest_entry_without_wheel_fails(self) -> None:
        result = run_audit(FIXTURES / "manifest-missing")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no matching wheel", result.stderr)

    def test_orphan_wheel_fails(self) -> None:
        result = run_audit(FIXTURES / "orphan-wheel")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not referenced", result.stderr)

    def test_duplicate_distribution_in_one_manifest_fails(self) -> None:
        result = run_audit(FIXTURES / "duplicate-manifest")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("more than once", result.stderr)


if __name__ == "__main__":
    unittest.main()
