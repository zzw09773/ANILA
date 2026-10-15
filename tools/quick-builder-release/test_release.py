#!/usr/bin/env python3
# ruff: noqa: E402 -- The tool installs the audit guard before test imports.
import os
import sys

# Load this one explicit sibling file; its first imports are only os/sys and
# the protected-file/network/command deny hook. No application import or .pth.
sys.dont_write_bytecode = True
_tool_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "release.py")
_scope = {"__name__": "release_test_module", "__file__": _tool_path}
with open(_tool_path, "rb") as _source:
    exec(compile(_source.read(), _tool_path, "exec"), _scope)

import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest import mock


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="quick-release-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.assets = self.base / "assets"
        self.assets.mkdir()
        for path in _scope["SOURCE_FILES"]:
            target = self.repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"synthetic source\n")
        (self.repo / "scripts/release/images.tsv").write_text("csp anila-csp:latest csp yes\n")

    def output(self, name="output"):
        path = self.base / name
        path.mkdir()
        return str(path)

    def build(self, output=None, **kwargs):
        output = output or self.output()
        result = _scope["build"](str(self.repo), output, **kwargs)
        return Path(output), result

    def replace_manifest(self, path, manifest):
        raw = _scope["canonical"](manifest)
        (path / "manifest.json").write_bytes(raw)
        sums = _scope["sha"](raw) + "  manifest.json\n"
        if manifest["archive_included"]:
            sums += _scope["sha"]((path / "source.tar").read_bytes()) + "  source.tar\n"
        (path / "SHA256SUMS").write_text(sums)

    def test_repeated_builds_are_bit_identical(self):
        first, manifest = self.build(archive=True)
        second, _ = self.build(self.output("repeat"), archive=True)
        for name in ("manifest.json", "SHA256SUMS", "source.tar"):
            self.assertEqual((first / name).read_bytes(), (second / name).read_bytes())
        self.assertTrue(manifest["selection_complete"])
        self.assertFalse(manifest["deployment_ready"])
        self.assertEqual(_scope["verify"](str(first)), manifest)

    def test_absent_assets_are_missing_not_fabricated_images(self):
        path, manifest = self.build(archive=True)
        self.assertTrue(all(item["status"] == "missing" for item in manifest["assets"]))
        self.assertFalse(manifest["asset_files_complete"])
        self.assertIsNone(manifest["declared_images"][0]["observed_image_id"])
        self.assertFalse(manifest["declared_images"][0]["identity_verified"])
        self.assertEqual(_scope["verify"](str(path)), manifest)

    def test_reordered_tar_is_rejected_even_with_consistent_outer_checksums(self):
        path, _ = self.build(archive=True)
        with tarfile.open(path / "source.tar", mode="r:") as archive:
            entries = [(member, archive.extractfile(member).read()) for member in archive]
        with tarfile.open(path / "source.tar", mode="w", format=tarfile.USTAR_FORMAT) as archive:
            for member, content in reversed(entries):
                archive.addfile(member, io.BytesIO(content))
        sums = _scope["sha"]((path / "manifest.json").read_bytes()) + "  manifest.json\n"
        sums += _scope["sha"]((path / "source.tar").read_bytes()) + "  source.tar\n"
        (path / "SHA256SUMS").write_text(sums)
        with self.assertRaisesRegex(_scope["UnsafeInput"], "ordering"):
            _scope["verify"](str(path))

    def test_present_asset_checksum_and_tamper(self):
        target = self.assets / "ocr/chi_tra.traineddata"
        target.parent.mkdir()
        target.write_bytes(b"synthetic OCR bytes, not a validated model")
        path, manifest = self.build(archive=True, assets_path=str(self.assets))
        self.assertEqual(next(row for row in manifest["assets"] if row["id"] == "ocr:chi_tra")["status"], "present")
        _scope["verify"](str(path), assets_path=str(self.assets))
        target.write_bytes(b"tampered")
        with self.assertRaises(_scope["UnsafeInput"]):
            _scope["verify"](str(path), assets_path=str(self.assets))

    def test_missing_source_does_not_claim_complete(self):
        (self.repo / _scope["SOURCE_FILES"][0]).unlink()
        path, manifest = self.build(archive=True)
        self.assertFalse(manifest["selection_complete"])
        _scope["verify"](str(path))

    def test_manifest_only_verification_checks_actual_source(self):
        path, _ = self.build()
        _scope["verify"](str(path), repo_path=str(self.repo))
        with self.assertRaises(_scope["UnsafeInput"]):
            _scope["verify"](str(path))
        (self.repo / _scope["SOURCE_FILES"][0]).write_bytes(b"changed")
        with self.assertRaises(_scope["UnsafeInput"]):
            _scope["verify"](str(path), repo_path=str(self.repo))

    def test_forbidden_paths_fail_before_open_even_if_canary_exists(self):
        reader = _scope["Root"](str(self.repo))
        self.addCleanup(reader.close)
        for path in (".env", "nested/.env.production", "secrets/value.txt", "keys/value.txt", "x.pem", "x.key", "logs/event.txt"):
            with self.subTest(path=path), mock.patch.object(os, "open", side_effect=AssertionError("canary was opened")):
                with self.assertRaises(_scope["UnsafeInput"]):
                    reader.read(path)

    def test_path_traversal_and_absolute_paths(self):
        reader = _scope["Root"](str(self.repo))
        self.addCleanup(reader.close)
        for path in ("../outside", "/etc/passwd", "x/../../outside", "x//y", "./x", "x\\y", "x/./y", "x\x00y"):
            with self.subTest(path=path), self.assertRaises(_scope["UnsafeInput"]):
                reader.read(path)

    def test_source_symlink_canary_never_read(self):
        canary = self.base / "canary.bin"
        canary.write_bytes(b"SYNTHETIC_CANARY_DO_NOT_READ")
        path = self.repo / _scope["SOURCE_FILES"][0]
        path.unlink()
        path.symlink_to(canary)
        with self.assertRaises(OSError):
            self.build(archive=True)

    def test_ancestor_symlink_is_rejected(self):
        real = self.base / "real"
        real.mkdir()
        linked = self.base / "linked"
        linked.symlink_to(real, target_is_directory=True)
        with self.assertRaises(OSError):
            _scope["Root"](str(linked))

    def test_asset_directory_symlink_to_secrets_canary_rejected(self):
        canary = self.base / "canary-directory"
        canary.mkdir()
        (canary / "chi_tra.traineddata").write_bytes(b"SYNTHETIC_CANARY_DO_NOT_READ")
        (self.assets / "ocr").symlink_to(canary, target_is_directory=True)
        with self.assertRaises(OSError):
            self.build(assets_path=str(self.assets))

    def test_hardlink_and_fifo_rejected(self):
        original = self.repo / _scope["SOURCE_FILES"][0]
        os.link(original, self.base / "other-link")
        with self.assertRaises(_scope["UnsafeInput"]):
            self.build()
        original.unlink()
        os.mkfifo(original)
        with self.assertRaises(_scope["UnsafeInput"]):
            self.build(self.output("fifo-output"))

    def test_source_size_limit(self):
        reader = _scope["Root"](str(self.repo))
        self.addCleanup(reader.close)
        with self.assertRaises(_scope["UnsafeInput"]):
            reader.read(_scope["SOURCE_FILES"][0], maximum=1)

    def test_manifest_checksum_tamper(self):
        path, _ = self.build(archive=True)
        with (path / "manifest.json").open("ab") as stream:
            stream.write(b" ")
        with self.assertRaises(_scope["UnsafeInput"]):
            _scope["verify"](str(path))

    def test_tar_checksum_tamper(self):
        path, _ = self.build(archive=True)
        with (path / "source.tar").open("r+b") as stream:
            stream.seek(1024)
            stream.write(b"tampered")
        with self.assertRaises(_scope["UnsafeInput"]):
            _scope["verify"](str(path))

    def test_self_consistent_unapproved_manifest_path_is_rejected(self):
        path, manifest = self.build(archive=True)
        manifest["files"][0]["path"] = "../.env"
        self.replace_manifest(path, manifest)
        with self.assertRaises(_scope["UnsafeInput"]):
            _scope["verify"](str(path))

    def test_false_deployment_or_image_identity_claim_rejected(self):
        for field in ("deployment_ready", "image"):
            path, manifest = self.build(self.output(field), archive=True)
            if field == "image":
                manifest["declared_images"][0]["observed_image_id"] = "sha256:invented"
            else:
                manifest[field] = True
            self.replace_manifest(path, manifest)
            with self.assertRaises(_scope["UnsafeInput"]):
                _scope["verify"](str(path))

    def test_tar_traversal_and_symlink_rejected_before_any_extraction(self):
        for kind in ("traversal", "symlink"):
            path, manifest = self.build(self.output(kind), archive=True)
            output = io.BytesIO()
            with tarfile.open(fileobj=output, mode="w", format=tarfile.USTAR_FORMAT) as archive:
                member = tarfile.TarInfo("../canary" if kind == "traversal" else "manifest.json")
                member.uid = member.gid = member.mtime = 0
                member.mode = 0o644
                if kind == "symlink":
                    member.type, member.linkname = tarfile.SYMTYPE, "../canary"
                archive.addfile(member)
            (path / "source.tar").write_bytes(output.getvalue())
            self.replace_manifest(path, manifest)
            with self.assertRaises(_scope["UnsafeInput"]):
                _scope["verify"](str(path))
            self.assertFalse((self.base / "canary").exists())

    def test_explicit_sbom_is_bounded_and_only_observed(self):
        sbom = self.base / "synthetic.cdx.json"
        sbom.write_text(json.dumps({"bomFormat": "CycloneDX", "specVersion": "1.6"}))
        path, manifest = self.build(archive=True, repo_sbom=str(sbom), revision="a" * 40)
        self.assertEqual(manifest["revision_claim"]["value"], "a" * 40)
        self.assertFalse(manifest["source_revision_verified"])
        _scope["verify"](str(path), repo_sbom=str(sbom))
        sbom.write_text("tampered")
        with self.assertRaises(_scope["UnsafeInput"]):
            _scope["verify"](str(path), repo_sbom=str(sbom))

    def test_self_consistent_tar_with_trailing_data_is_rejected(self):
        path, manifest = self.build(archive=True)
        with (path / "source.tar").open("ab") as stream:
            stream.write(b"SYNTHETIC_UNAPPROVED_TRAILING_CANARY")
        self.replace_manifest(path, manifest)
        with self.assertRaises(_scope["UnsafeInput"]):
            _scope["verify"](str(path))

    def test_aggregate_source_budget_is_enforced_before_read(self):
        with mock.patch.dict(_scope, MAX_SOURCE_TOTAL=1):
            with self.assertRaises(_scope["UnsafeInput"]):
                self.build()

    def test_unknown_image_archive_name_is_never_followed(self):
        (self.repo / "scripts/release/images.tsv").write_text("csp anila-csp:latest ../../secrets yes\n")
        with self.assertRaises(_scope["UnsafeInput"]):
            self.build()

    def test_no_overwrite_and_output_not_inside_source(self):
        path, _ = self.build(archive=True)
        with self.assertRaises(FileExistsError):
            _scope["build"](str(self.repo), str(path), archive=True)
        inner = self.repo / "output"
        inner.mkdir()
        with self.assertRaises(_scope["UnsafeInput"]):
            _scope["build"](str(self.repo), str(inner))

    def test_audit_hook_blocks_protected_reads_and_commands(self):
        for event, args in (("open", ("/tmp/keys/private.txt", "r", 0)),
                            ("open", ("/tmp/secrets/canary", "r", 0)),
                            ("subprocess.Popen", ("docker", [], None, None)),
                            ("socket.connect", (None, ("127.0.0.1", 2375)))):
            with self.subTest(event=event), self.assertRaises(PermissionError):
                sys.audit(event, *args)


if __name__ == "__main__":
    unittest.main(verbosity=2)
