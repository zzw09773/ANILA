"""Regression tests for the formal platform air-gap image closure."""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
INTRANET = ROOT / "infra" / "deployment" / "intranet"
INVENTORY = INTRANET / "platform-image-inventory.tsv"
MODEL_INVENTORY = INTRANET / "model-image-inventory.tsv"
CHECKER = INTRANET / "check_airgap_inventory.py"
EXPORTER = INTRANET / "build-and-export-for-intranet.sh"

spec = importlib.util.spec_from_file_location("check_airgap_inventory", CHECKER)
if spec is None or spec.loader is None:  # pragma: no cover - import machinery guard
    raise RuntimeError(f"cannot import {CHECKER}")
checker = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = checker
spec.loader.exec_module(checker)


class AirgapImageInventoryTests(unittest.TestCase):
    def test_flux_agent_is_a_required_built_platform_image(self) -> None:
        entries = {entry.service: entry for entry in checker.load_inventory(INVENTORY)}
        entry = entries["flux2-dev-agent"]
        self.assertEqual(entry.image, "anila-platform-flux2-dev-agent")
        self.assertEqual(entry.bundle, "01-anila-built.tar.gz")
        self.assertEqual(entry.activation, "default")
        self.assertEqual(entry.source, "built")

    def test_codeserver_is_the_only_optional_profile_image(self) -> None:
        entries = checker.load_inventory(INVENTORY)
        optional = [entry for entry in entries if entry.activation != "default"]
        self.assertEqual(len(optional), 1)
        self.assertEqual(optional[0].service, "codeserver")
        self.assertEqual(optional[0].activation, "profile:developer-tools")

    def test_retained_tools_use_supported_security_pins(self) -> None:
        entries = {entry.service: entry for entry in checker.load_inventory(INVENTORY)}
        self.assertEqual(entries["n8n"].image, "n8nio/n8n:2.29.10")
        self.assertEqual(
            entries["gitlab"].image,
            "gitlab/gitlab-ce:19.1.1-ce.0",
        )

    def test_optional_model_inventory_covers_intranet_profile_image(self) -> None:
        entries = checker.load_model_inventory(MODEL_INVENTORY)
        profile_entries = [
            entry for entry in entries if entry.activation == "profile:intranet"
        ]
        self.assertEqual(
            {entry.service for entry in profile_entries},
            {"gemma-4-26b-a4b", "gemma-4-12b", "gpt-oss-120b"},
        )
        self.assertEqual(
            {entry.image for entry in profile_entries},
            {"vllm/vllm-openai:v0.22.1-cu129-ubuntu2404"},
        )

    def test_exporter_consumes_inventory_and_loader_checks_required_images(self) -> None:
        script = EXPORTER.read_text(encoding="utf-8")
        self.assertIn("platform-image-inventory.tsv", script)
        self.assertIn("inventory_images_for_bundle", script)
        self.assertIn('docker compose build "${BUILT_SERVICES[@]}"', script)
        self.assertIn("PLATFORM-IMAGE-INVENTORY.tsv", script)
        self.assertIn("PLATFORM-IMAGE-LOCK.tsv", script)
        self.assertIn("MODEL-IMAGE-INVENTORY.tsv", script)
        self.assertIn("REQUIRED missing/stale", script)
        self.assertIn("required_missing", script)
        self.assertIn("Required bundle file missing/empty", script)
        self.assertIn("expected_id", script)
        self.assertIn("actual_id", script)
        self.assertIn("sha256sum -c CHECKSUMS.sha256", script)
        self.assertNotIn("CHECKSUMS.sha256 不存在", script)
        self.assertNotIn("missing $tar — skipped", script)
        self.assertNotIn(
            "docker compose build csp ingestion-worker router", script
        )

    def test_duplicate_service_is_rejected(self) -> None:
        row = "svc\timage:tag\t01-anila-built.tar.gz\tdefault\tbuilt\n"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "inventory.tsv"
            path.write_text(row + row, encoding="utf-8")
            with self.assertRaises(checker.InventoryError):
                checker.load_inventory(path)

    def test_inventory_matches_resolved_compose_model(self) -> None:
        if shutil.which("docker") is None:
            self.skipTest("docker compose is not installed")
        probe = subprocess.run(
            ["docker", "compose", "version"],
            check=False,
            capture_output=True,
            encoding="utf-8",
        )
        if probe.returncode != 0:
            self.skipTest("docker compose is not usable in this environment")
        required, optional = checker.validate(ROOT, INVENTORY)
        self.assertEqual(required, 13)
        self.assertEqual(optional, 1)
        model_default, model_optional, unique_images = checker.validate_models(
            ROOT, MODEL_INVENTORY
        )
        self.assertEqual(model_default, 6)
        self.assertEqual(model_optional, 3)
        self.assertEqual(unique_images, 7)


if __name__ == "__main__":
    unittest.main()
