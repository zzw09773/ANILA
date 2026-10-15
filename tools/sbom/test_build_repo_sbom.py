# ruff: noqa: E402 -- Install the protected-file audit hook before other imports.
"""Pure-stdlib SBOM regression tests; run from /tmp with python3 -I -S."""
import os
import sys


def _guard(event, args):
    if event == "open" and args and isinstance(args[0], (str, bytes, os.PathLike)):
        parts = os.fsdecode(args[0]).replace("\\", "/").split("/")
        if ("secrets" in parts or parts[-1].startswith(".env")
                or parts[-1] == ".npmrc" or parts[-1].endswith((".pem", ".key"))):
            raise PermissionError("Protected file access denied")


sys.addaudithook(_guard)

import importlib.util
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("sbom", Path(__file__).with_name("build_repo_sbom.py"))
sbom = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sbom)


def item(inventory, name, *, observation=None, locator=None):
    return next(r for r in inventory.records.values() if r["name"] == name
                and (observation is None or r["observation"] == observation)
                and (locator is None or r["locator"] == locator))


class InventoryTests(unittest.TestCase):
    def test_npm_nested_resolution_uses_nearest_recorded_package(self):
        inventory = sbom.Inventory()
        inventory.npm_lock("app/package-lock.json", {"name": "app", "packages": {
            "": {"dependencies": {"a": "^1"}},
            "node_modules/a": {"version": "1.0.0", "dependencies": {"b": "^2"}},
            "node_modules/b": {"version": "1.0.0"},
            "node_modules/a/node_modules/b": {"version": "2.0.0"},
        }})
        a = item(inventory, "a")
        nested = item(inventory, "b", locator="node_modules/a/node_modules/b")
        self.assertEqual(inventory.edges[a["ref"]], {nested["ref"]})
        self.assertEqual(a["relationship"], "direct")
        self.assertEqual(nested["relationship"], "transitive")

    def test_range_declaration_never_becomes_exact_or_locked(self):
        inventory = sbom.Inventory()
        inventory.npm_manifest("app/package.json", {"name": "app", "dependencies": {"foo": ">=1 <3"}})
        foo = item(inventory, "foo")
        component = sbom.component(foo, None)
        self.assertIsNone(foo["version"])
        self.assertNotIn("version", component)
        self.assertEqual(foo["observation"], "declared")
        self.assertEqual(foo["constraint"], ">=1 <3")

    def test_requirement_pin_and_hash_stay_declared_unless_lock_file(self):
        inventory = sbom.Inventory()
        raw = "foo==1.2.3 " + "\\\n" + "  --hash=sha256:" + "a" * 64 + "\n"
        inventory.requirements("requirements.txt", raw)
        inventory.requirements("requirements.lock", raw)
        self.assertEqual(item(inventory, "foo", observation="declared")["version"], "1.2.3")
        self.assertEqual(item(inventory, "foo", observation="locked")["hashes"], [{"alg": "SHA-256", "content": "a" * 64}])

    def test_requirements_include_is_not_followed(self):
        inventory = sbom.Inventory()
        inventory.requirements("requirements.txt", "-r .env\n-c ../secrets/unsafe.txt\nfoo>=1\n")
        self.assertEqual(len([n for n in inventory.notices if n["type"] == "directive_not_followed"]), 2)
        self.assertEqual(item(inventory, "foo")["observation"], "declared")

    def test_lock_directness_uses_explicit_input_only(self):
        inventory = sbom.Inventory()
        inventory.requirements("x/requirements.in", "foo\n")
        inventory.requirements("x/requirements.lock", "foo==1\nbar==2\n")
        self.assertEqual(item(inventory, "foo", observation="locked")["relationship"], "direct")
        self.assertEqual(item(inventory, "bar")["relationship"], "transitive")

    def test_inserted_declarations_keep_stable_baseline_identity(self):
        before, after = sbom.Inventory(), sbom.Inventory()
        before.python_manifest("pyproject.toml", {"project": {"name": "app", "dependencies": ["b>=1", "c==2"]}})
        after.python_manifest("pyproject.toml", {"project": {"name": "app", "dependencies": ["a>=1", "b>=1", "c==3"]}})
        b = item(after, "b")
        c = item(after, "c")
        self.assertEqual(sbom.change(b, before.records[b["key"]]), "baseline-existing")
        self.assertEqual(sbom.change(c, before.records[c["key"]]), "version-or-constraint-changed")
        self.assertEqual(sbom.change(item(after, "a"), None), "added")

    def test_uv_new_package_does_not_renumber_existing_versions(self):
        before, after = sbom.Inventory(), sbom.Inventory()
        before.uv_lock("uv.lock", {"package": [{"name": "z", "version": "1"}]})
        after.uv_lock("uv.lock", {"package": [{"name": "a", "version": "1"}, {"name": "z", "version": "2"}]})
        z = item(after, "z")
        self.assertEqual(sbom.change(z, before.records[z["key"]]), "version-or-constraint-changed")

    def test_uv_ambiguous_versions_are_not_silently_resolved(self):
        inventory = sbom.Inventory()
        inventory.uv_lock("uv.lock", {"package": [
            {"name": "app", "version": "1", "source": {"editable": "."}, "dependencies": [{"name": "a"}]},
            {"name": "a", "version": "1"}, {"name": "a", "version": "2"},
        ]})
        self.assertTrue(any(n["type"] == "ambiguous_uv_edge" for n in inventory.notices))
        self.assertEqual(len([r for r in inventory.records.values() if r["name"] == "a"]), 2)

    def test_urls_are_clean_in_nested_evidence_and_constraints(self):
        record = sbom.Inventory().add(
            "pypi", "requirements.txt", "foo", "foo", "declared",
            constraint="@ https://user:password@repo.example/a.whl?token=private#fragment",
            urls=["https://user:password@repo.example/a.whl?token=private#fragment"],
            extra={"artifacts": [{"url": "https://user:password@repo.example/a.whl?token=private#fragment"}]},
        )
        rendered = json.dumps(sbom.component(record, None))
        for secret in ("password", "user:", "token=", "private", "#fragment"):
            self.assertNotIn(secret, rendered)
        self.assertIn("https://repo.example/a.whl", rendered)

    def test_installed_metadata_is_not_imported_or_treated_as_lock(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "site-packages"
            metadata = root / "foo-9.dist-info/METADATA"
            metadata.parent.mkdir(parents=True)
            metadata.write_text("Name: Foo\nVersion: 9\nLicense: MIT\nAuthor: Example\n\n")
            (root / "foo.py").write_text("raise AssertionError('must not import installed code')\n")
            inventory = sbom.Inventory()
            inventory.requirements("requirements.lock", "foo==1\n")
            inventory.installed_metadata([str(root)])
            self.assertEqual(item(inventory, "foo", observation="locked")["version"], "1")
            installed = item(inventory, "foo", observation="installed")
            self.assertEqual(installed["version"], "9")
            self.assertEqual(sbom.change(installed, None), "not-compared-installed-observation")

    def test_license_file_is_evidence_path_not_license_text(self):
        inventory = sbom.Inventory()
        inventory.python_manifest("pyproject.toml", {"project": {"name": "app", "license": {"file": "LICENSE.txt"}}})
        rendered = sbom.component(item(inventory, "app"), None)
        self.assertNotIn("licenses", rendered)
        properties = {p["name"]: p["value"] for p in rendered["properties"]}
        self.assertEqual(properties["anila:license-status"], "unknown")
        self.assertEqual(properties["anila:license-file-not-read"], "LICENSE.txt")

    def test_npm_local_link_has_recorded_target_without_following_files(self):
        inventory = sbom.Inventory()
        inventory.npm_lock("app/package-lock.json", {"name": "app", "packages": {
            "": {"dependencies": {"foo": "file:vendor/foo"}},
            "node_modules/foo": {"resolved": "vendor/foo", "link": True},
            "vendor/foo": {"name": "foo", "version": "1"},
        }})
        link = item(inventory, "foo", locator="node_modules/foo")
        target = item(inventory, "foo", locator="vendor/foo")
        self.assertEqual(inventory.edges[link["ref"]], {target["ref"]})

    def test_complete_exports_remove_url_userinfo_query_fragment_canaries(self):
        url = "https://CANARY_USER:CANARY_PASSWORD@repo.example/pkg?CANARY_QUERY=token#CANARY_FRAGMENT"
        inventory = sbom.Inventory()
        inventory.npm_lock("app/package-lock.json", {"name": "app", "packages": {
            "": {"dependencies": {"foo": url}},
            "node_modules/foo": {"version": "1", "resolved": url,
                                 "dependencies": {url: "1"}, "license": url},
        }})
        inventory.add("pypi", "requirements.txt", "foo", "foo", "declared",
                      version=url, constraint="@ " + url, author=url,
                      extra={"nested": {"url": url}})
        inventory.notices.append({"path": url, "type": "fixture", "dependency": url})
        with patch.object(sbom, "load_repository", return_value=inventory):
            bom, summary = sbom.build(Path("/tmp"))
        rendered = json.dumps([bom, summary])
        for canary in ("CANARY_USER", "CANARY_PASSWORD", "CANARY_QUERY", "CANARY_FRAGMENT"):
            self.assertNotIn(canary, rendered)
        sbom.validate_structure(bom)

    def test_invalid_baseline_fails_instead_of_reporting_everything_added(self):
        with TemporaryDirectory() as directory, self.assertRaises(ValueError):
            sbom.load_repository(Path(directory), "not-a-commit")

    def test_deterministic_bom_and_reference_invariants(self):
        inventory = sbom.Inventory()
        inventory.requirements("requirements.txt", "foo>=1\n")
        with patch.object(sbom, "load_repository", return_value=inventory):
            first, _ = sbom.build(Path("/tmp"))
            second, _ = sbom.build(Path("/tmp"))
        self.assertEqual(first, second)
        sbom.validate_structure(first)
        first["dependencies"][0]["dependsOn"].append("unknown-ref")
        with self.assertRaises(AssertionError):
            sbom.validate_structure(first)

    def test_protected_files_are_denied_before_open(self):
        for path in ("/tmp/.env", "/tmp/.npmrc", "/tmp/secrets/fixture", "/tmp/fixture.pem", "/tmp/fixture.key"):
            with self.subTest(path=path), self.assertRaises(PermissionError):
                open(path)


if __name__ == "__main__":
    if os.getcwd() != "/tmp":
        raise SystemExit("Run stdlib SBOM tests from /tmp")
    unittest.main()
