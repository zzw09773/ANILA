from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).parents[3]
SECURITY_SRC = ROOT / "packages" / "anila-security" / "src"
if str(SECURITY_SRC) not in sys.path:
    sys.path.insert(0, str(SECURITY_SRC))

from anila_security.model_governance import (  # noqa: E402
    VerifiedModelGovernanceAuthority,
    inventory_content_sha256,
    profile_content_sha256,
)


SCRIPT = ROOT / "infra/deployment/scripts/generate-gate5-test-material.py"
DEFAULT_CALLS = {"r7.csp.memory"}


def _generator():
    spec = importlib.util.spec_from_file_location("gate5_material_generator", SCRIPT)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot import generator: {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_material(paths: dict[str, Path]) -> tuple[dict, dict, dict, dict]:
    return tuple(
        json.loads(paths[name].read_text(encoding="utf-8"))
        for name in ("inventory", "profile", "trust_store", "observed_facts")
    )  # type: ignore[return-value]


class Gate5TestMaterialGeneratorTests(unittest.TestCase):
    def test_default_material_is_memory_only_and_verifies(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = _generator().generate(Path(temp_dir))
            inventory, profile, trust_store, observed = _load_material(paths)

            self.assertEqual(
                profile["profile_id"], "synthetic-gate5-smoke-not-production"
            )
            self.assertEqual(set(profile["enabled_callsites"]), DEFAULT_CALLS)
            self.assertEqual(
                len(profile["disabled_callsites"]), len(inventory["callsites"]) - 1
            )
            self.assertEqual(len(profile["callsite_bindings"]), 1)
            self.assertEqual(
                profile["callsite_bindings"][0]["callsite_id"], "r7.csp.memory"
            )
            self.assertEqual(
                profile["model_artifacts"][0]["artifact_id"],
                "artifact.gate5-synthetic",
            )
            self.assertEqual(
                profile["deployments"][0]["artifact_id"],
                profile["model_artifacts"][0]["artifact_id"],
            )
            self.assertEqual(
                observed["artifacts"][0]["artifact_id"],
                profile["model_artifacts"][0]["artifact_id"],
            )
            self.assertEqual(
                observed["deployments"][0]["deployment_id"],
                profile["deployments"][0]["deployment_id"],
            )
            self.assertTrue(
                all(
                    not call_id.startswith("r7.flux.")
                    for call_id in profile["enabled_callsites"]
                )
            )
            self.assertEqual(
                profile["inventory_sha256"], inventory_content_sha256(inventory)
            )
            self.assertEqual(
                profile["profile_content_sha256"], profile_content_sha256(profile)
            )
            self.assertEqual(
                set(trust_store["trusted_signers"]),
                {"system_owner", "data_owner", "security", "operations"},
            )
            self.assertTrue(
                all(
                    "PRIVATE KEY" not in path.read_text(encoding="utf-8")
                    for path in paths.values()
                )
            )

            now = datetime.fromisoformat(
                profile["deployments"][0]["health_readiness"]["last_check"]
            ) + timedelta(seconds=1)
            authority = VerifiedModelGovernanceAuthority.from_verified_payload(
                inventory=inventory,
                profile=profile,
                trust_store=trust_store,
                now=now,
            )
            self.assertTrue(authority.enabled)
            self.assertEqual(set(authority.bindings), DEFAULT_CALLS)

    def test_generator_supports_explicit_multiple_non_flux_callsites(self) -> None:
        callsite_ids = (
            "r7.csp.proxy",
            "r7.csp.proxy-agent",
            "r7.csp.proxy-service",
            "r7.csp.proxy-service-agent",
            "r7.csp.memory",
        )
        expected_calls = set(callsite_ids)
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = _generator().generate(Path(temp_dir), callsite_ids=callsite_ids)
            inventory, profile, trust_store, _ = _load_material(paths)
            self.assertEqual(set(profile["enabled_callsites"]), expected_calls)
            self.assertEqual(
                len(profile["disabled_callsites"]), len(inventory["callsites"]) - 5
            )
            self.assertEqual(
                {
                    binding["model_artifact_id"]
                    for binding in profile["callsite_bindings"]
                },
                {"artifact.gate5-synthetic"},
            )
            self.assertEqual(
                {binding["deployment_id"] for binding in profile["callsite_bindings"]},
                {"deployment.gate5-synthetic"},
            )

            now = datetime.fromisoformat(
                profile["deployments"][0]["health_readiness"]["last_check"]
            ) + timedelta(seconds=1)
            authority = VerifiedModelGovernanceAuthority.from_verified_payload(
                inventory=inventory,
                profile=profile,
                trust_store=trust_store,
                now=now,
            )
            self.assertEqual(set(authority.bindings), expected_calls)
            self.assertEqual(authority.bindings["r7.csp.proxy"].agent_scope, ())
            self.assertEqual(
                authority.bindings["r7.csp.proxy-agent"].agent_scope,
                ("registered-agent",),
            )
            self.assertEqual(authority.bindings["r7.csp.proxy-service"].agent_scope, ())
            self.assertEqual(
                authority.bindings["r7.csp.proxy-service-agent"].agent_scope,
                ("registered-agent",),
            )
            self.assertEqual(authority.bindings["r7.csp.memory"].agent_scope, ())

    def test_generator_rejects_raw_flux_and_unknown_callsites(self) -> None:
        cases = (
            (("r7.flux.backend",), "non-FLUX"),
            (("r7.csp.prompt-generator",), "non-FLUX"),
            (("r7.not-in-inventory",), "unknown"),
        )
        for callsite_ids, pattern in cases:
            with self.subTest(callsite_ids=callsite_ids, pattern=pattern):
                with tempfile.TemporaryDirectory() as temp_dir:
                    with self.assertRaisesRegex(ValueError, pattern):
                        _generator().generate(Path(temp_dir), callsite_ids=callsite_ids)


if __name__ == "__main__":
    unittest.main()
