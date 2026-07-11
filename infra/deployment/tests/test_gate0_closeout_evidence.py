"""Behavioral contracts for the Gate 0 closeout evidence collector."""
from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "infra/deployment/scripts/gate0-closeout-evidence.py"
SPEC = importlib.util.spec_from_file_location("gate0_closeout_evidence", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class Gate0CloseoutEvidenceTests(unittest.TestCase):
    def test_profile_artifact_omits_expanded_secrets(self) -> None:
        config = {
            "name": "anila-platform",
            "services": {
                "csp": {
                    "environment": {
                        "ANILA_ENV": "production",
                        "ENABLE_MEMORY": "false",
                        "ENABLE_PUBLIC_SHARE": "false",
                        "REQUIRE_CARD_LOGIN_ONLY": "true",
                        "ANILA_TRACE_ENDPOINT": "http://csp:8000",
                        "SITE_URL": "https://anila.ai.ncsist.org.tw",
                        "CSP_SECRET_KEY": "must-never-appear",
                        "CSP_SERVICE_TOKEN": "must-never-appear",
                        "DATABASE_URL": "postgresql://user:must-never-appear@db/app",
                        "AUTO_REGISTER_LINKS": json.dumps([
                            {"name": "n8n 工作流程", "url": "https://n8n.ai.ncsist.org.tw/"},
                            {"name": "GitLab", "url": "https://gitlab.ai.ncsist.org.tw/"},
                            {"name": "Code Server (按需)", "url": "https://code.ai.ncsist.org.tw/"},
                        ]),
                    },
                    "networks": {"default": None},
                    "volumes": [],
                },
                "router": {"environment": {"ANILA_TRACE_ENDPOINT": "http://csp:8000"}},
                "n8n": {"networks": {"n8n-tools": None}, "volumes": []},
                "gitlab": {
                    "networks": {"gitlab-tools": None},
                    "volumes": [],
                    "ports": [{"target": 22, "host_ip": "10.53.100.15"}],
                },
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            bundle = MODULE.EvidenceBundle(Path(directory) / "evidence")
            with patch.object(MODULE, "compose_config", return_value=config), patch.object(
                MODULE, "git_commit", return_value="1aedf155"
            ):
                payload = MODULE.collect_profile(bundle)
            self.assertTrue(payload["passed"])
            serialized = (bundle.directory / "formal-profile.json").read_text(
                encoding="utf-8"
            )
            self.assertNotIn("must-never-appear", serialized)
            self.assertNotIn("DATABASE_URL", serialized)
            self.assertNotIn("CSP_SERVICE_TOKEN", serialized)
            manifest = json.loads(
                (bundle.directory / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertFalse(manifest["git_worktree_clean"])

    def test_fresh_host_uses_air_gap_no_build_contract(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn(
            '"docker", "compose", "up", "-d", "--no-build", "--pull", "never"',
            source,
        )
        self.assertIn("formal_invocation", source)
        self.assertIn("COMPOSE_PROJECT_NAME", source)
        self.assertIn("COMPOSE_FILE", source)
        self.assertIn("existing_project_containers", source)
        self.assertIn("existing_project_volumes", source)
        self.assertIn("missing_default_images", source)
        self.assertIn("git_worktree_clean", source)

    def test_trace_and_tls_artifacts_do_not_serialize_credentials(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("The bearer token is never written to evidence", source)
        self.assertNotIn('"token": token', source)
        self.assertNotIn('"private_key_bytes"', source)
        self.assertIn("current_key_differs_from_all_historical_keys", source)
        self.assertIn("collect_historical_tls_before", source)
        self.assertIn("historical_pairs", source)
        self.assertIn("host_private_material_acl_restricted", source)
        self.assertIn("broad_principals_present", source)
        self.assertIn("tls-rotation-comparison.json", source)

    def test_collector_requires_external_evidence_directory(self) -> None:
        with self.assertRaises(MODULE.CloseoutError):
            MODULE.EvidenceBundle(ROOT / "evidence-must-not-be-created")


if __name__ == "__main__":
    unittest.main()
