"""Focused tests for the shared external model-network lifecycle helper."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
HELPER = ROOT / "infra/deployment/scripts/ensure-models-network.sh"


DOCKER_STUB = r"""#!/usr/bin/env bash
set -euo pipefail

state_file="${MODEL_NETWORK_STATE:?}"
log_file="${MODEL_NETWORK_LOG:?}"
command_name="${1:-}"
if [[ "$command_name" != network ]]; then
  echo "unexpected docker command: $*" >&2
  exit 97
fi
subcommand="${2:-}"
network="${3:-}"
case "$subcommand" in
  ls)
    if [[ -n "${MODEL_NETWORK_LIST_ERROR:-}" ]]; then
      echo "$MODEL_NETWORK_LIST_ERROR" >&2
      exit 1
    fi
    if [[ -f "$state_file" ]]; then
      printf '%s\n' "${MODEL_NETWORK_NAME:?}"
    fi
    ;;
  inspect)
    if [[ -n "${MODEL_NETWORK_INSPECT_ERROR:-}" ]]; then
      echo "$MODEL_NETWORK_INSPECT_ERROR" >&2
      exit 1
    fi
    [[ -f "$state_file" ]] || exit 1
    if [[ " $* " == *" --format "* ]]; then
      format="${5:-}"
      IFS='|' read -r internal driver < "$state_file"
      case "$format" in
        "{{.Internal}}") printf '%s\n' "$internal" ;;
        "{{.Driver}}") printf '%s\n' "$driver" ;;
        *) echo "unexpected inspect format: $format" >&2; exit 98 ;;
      esac
    fi
    ;;
  create)
    [[ "${3:-}" == "--driver" && "${4:-}" == "bridge" && "${5:-}" == "--internal" ]] || {
      echo "create missing --internal: $*" >&2
      exit 99
    }
    network="${6:-}"
    printf 'create %s\n' "$network" >> "$log_file"
    if [[ -n "${MODEL_NETWORK_CREATE_RACE:-}" ]]; then
      # Simulate another caller winning immediately after our successful list.
      printf 'true|bridge\n' > "$state_file"
      echo "network already exists" >&2
      exit 1
    fi
    printf 'true|bridge\n' > "$state_file"
    ;;
  *)
    echo "unexpected network subcommand: $*" >&2
    exit 97
    ;;
esac
"""


class ModelsNetworkHelperTests(unittest.TestCase):
    def run_helper(
        self,
        state: str | None,
        *,
        list_error: str | None = None,
        inspect_error: str | None = None,
        create_race: bool = False,
    ) -> tuple[subprocess.CompletedProcess[str], str]:
        # /tmp is mounted noexec in the managed runner; keep the executable
        # docker stub under the writable checkout instead.
        with tempfile.TemporaryDirectory(
            prefix=".anila-network-helper-", dir=ROOT
        ) as temp:
            directory = Path(temp)
            docker = directory / "docker"
            docker.write_text(DOCKER_STUB, encoding="utf-8", newline="\n")
            docker.chmod(0o755)
            state_file = directory / "state"
            log_file = directory / "calls"
            if state is not None:
                state_file.write_text(state, encoding="utf-8")
            env = os.environ.copy()
            env.update(
                {
                    "PATH": f"{directory}{os.pathsep}{env.get('PATH', '')}",
                    "MODEL_NETWORK_STATE": str(state_file),
                    "MODEL_NETWORK_LOG": str(log_file),
                    "MODEL_NETWORK_NAME": "anila-test-models",
                }
            )
            if list_error is not None:
                env["MODEL_NETWORK_LIST_ERROR"] = list_error
            if inspect_error is not None:
                env["MODEL_NETWORK_INSPECT_ERROR"] = inspect_error
            if create_race:
                env["MODEL_NETWORK_CREATE_RACE"] = "1"
            result = subprocess.run(
                ["bash", str(HELPER), "ensure", "anila-test-models"],
                cwd=ROOT,
                env=env,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            calls = log_file.read_text(encoding="utf-8") if log_file.exists() else ""
            return result, calls

    def test_missing_network_is_created_with_internal_bridge(self) -> None:
        result, calls = self.run_helper(None)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, "create anila-test-models\n")

    def test_valid_existing_network_is_idempotent(self) -> None:
        result, calls = self.run_helper("true|bridge\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, "")

    def test_create_race_verifies_the_winning_network(self) -> None:
        result, calls = self.run_helper(None, create_race=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, "create anila-test-models\n")

    def test_malformed_existing_network_fails_without_mutation(self) -> None:
        result, calls = self.run_helper("false|bridge\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Internal=true", result.stderr)
        self.assertIn("refusing automatic removal", result.stderr.lower())
        self.assertEqual(calls, "")

    def test_network_discovery_errors_are_never_treated_as_missing(self) -> None:
        for error in (
            "Cannot connect to the Docker daemon at unix:///var/run/docker.sock",
            "permission denied while trying to connect to the Docker daemon socket",
            "context deadline exceeded",
        ):
            with self.subTest(error=error):
                result, calls = self.run_helper(None, list_error=error)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("network discovery failed", result.stderr.lower())
                self.assertEqual(calls, "")

    def test_existing_network_readback_error_fails_without_mutation(self) -> None:
        result, calls = self.run_helper(
            "true|bridge\n", inspect_error="error during connect: connection reset"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("topology read-back failed", result.stderr.lower())
        self.assertEqual(calls, "")

    def test_compose_consumers_are_external_and_unowned(self) -> None:
        for relative in (
            "infra/compose/platform.yml",
            "infra/compose/dev.yml",
            "infra/models/docker-compose.yml",
        ):
            document = (ROOT / relative).read_text(encoding="utf-8")
            marker = "  models:" if relative.endswith("docker-compose.yml") else "  anila-models-net:"
            self.assertIn(marker, document, relative)
            lines = document.splitlines()
            marker_index = lines.index(marker)
            network_lines = []
            for line in lines[marker_index + 1 :]:
                if line.startswith("  ") and not line.startswith("    "):
                    break
                network_lines.append(line)
            network_block = "\n".join(network_lines)
            self.assertIn("external: true", network_block, relative)
            self.assertNotIn("driver:", network_block, relative)
            self.assertNotIn("internal:", network_block, relative)

    def test_deployment_entries_source_the_single_helper(self) -> None:
        for relative in (
            "infra/deployment/scripts/deploy-prod.sh",
            "infra/deployment/intranet/model-serve.sh",
            "infra/deployment/intranet/intranet-deploy.sh",
        ):
            document = (ROOT / relative).read_text(encoding="utf-8")
            self.assertIn("ensure-models-network.sh", document, relative)
            self.assertIn("ensure_models_network", document, relative)

    def test_silver_e2e_uses_helper_and_never_removes_canonical_network(self) -> None:
        document = (
            ROOT / "infra/deployment/scripts/gate5-silver-e2e.sh"
        ).read_text(encoding="utf-8")
        self.assertIn("ensure-models-network.sh", document)
        self.assertIn("ensure_models_network", document)
        self.assertNotIn("docker network rm anila-models-net", document)


if __name__ == "__main__":
    unittest.main()
