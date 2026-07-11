"""Executable regression tests for the production deployment guards.

The deployment script is run through Bash with a fake ``docker`` executable.
This exercises the shell control flow and exit codes without requiring a
Docker daemon or touching the host's real containers and volumes.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
DEPLOY_SCRIPT = ROOT / "infra" / "deployment" / "scripts" / "deploy-prod.sh"


def find_bash() -> str | None:
    """Return a usable Bash binary on Linux/macOS or Git for Windows."""

    # Prefer Git Bash on Windows. ``WindowsApps\bash.exe`` launches WSL and
    # cannot execute a script through a native ``D:\...`` path or see a
    # native temp-directory command stub reliably.
    candidates = (
        [
            r"C:\Program Files\Git\bin\bash.exe",
            r"C:\Program Files\Git\usr\bin\bash.exe",
            shutil.which("bash"),
        ]
        if os.name == "nt"
        else [shutil.which("bash")]
    )
    for candidate in candidates:
        if not candidate or not Path(candidate).is_file():
            continue
        probe = subprocess.run(
            [candidate, "--version"],
            check=False,
            capture_output=True,
        )
        if probe.returncode == 0:
            return candidate
    return None


DOCKER_STUB = r"""#!/usr/bin/env bash
set -euo pipefail

scenario="${DOCKER_STUB_SCENARIO:-all-healthy}"

if [[ "${1:-}" == "info" ]]; then
  exit 0
fi

if [[ "${1:-}" == "compose" && "${2:-}" == "version" ]]; then
  printf '%s\n' 'Docker Compose version v2.test'
  exit 0
fi

if [[ "${1:-}" == "compose" && "${2:-}" == "ps" ]]; then
  if [[ "${3:-}" == "-a" && "${4:-}" == "-q" ]]; then
    service="${5:-}"
    if [[ "$scenario" == "old-n8n-image" && "$service" == "n8n" ]]; then
      printf '%s\n' 'n8n-test-container'
    fi
    exit 0
  fi

  service="${3:-}"
  status='Up 30 seconds (healthy)'
  [[ "$service" == "ingestion-worker" ]] && status='Up 30 seconds'
  case "$scenario:$service" in
    unhealthy:csp) status='Up 30 seconds (unhealthy)' ;;
    restarting:router) status='Restarting (1) 2 seconds ago' ;;
    exited:n8n) status='Exited (1) 2 seconds ago' ;;
    starting:csp) status='Up 2 seconds (health: starting)' ;;
    missing:csp) status='' ;;
    ingestion-exited:ingestion-worker) status='Exited (1) 2 seconds ago' ;;
  esac
  [[ -n "$status" ]] && printf '%s\n' "$status"
  exit 0
fi

if [[ "${1:-}" == "inspect" ]]; then
  if [[ "$scenario" == "old-n8n-image" && "${2:-}" == "n8n-test-container" ]]; then
    printf '%s\n' 'n8nio/n8n:1.82.3'
  fi
  exit 0
fi

if [[ "${1:-}" == "volume" && "${2:-}" == "ls" ]]; then
  filters="$*"
  if [[ "$scenario" == "unmarked-n8n-volume" && "$filters" == *"volume=n8n_data"* ]]; then
    printf '%s\n' 'anila-platform_n8n_data'
  elif [[ "$scenario" == "partial-gitlab-volume" && "$filters" == *"volume=gitlab_config"* ]]; then
    printf '%s\n' 'anila-platform_gitlab_config'
  fi
  exit 0
fi

if [[ "${1:-}" == "run" ]]; then
  # The unmarked-volume scenario deliberately returns no marker content.
  exit 0
fi

printf 'unexpected docker stub invocation: %q ' "$@" >&2
printf '\n' >&2
exit 97
"""


class DeployProdBehaviorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.bash = find_bash()
        if cls.bash is None:
            raise unittest.SkipTest("Bash/Git Bash is not available")

    def run_deploy(
        self,
        scenario: str,
        subcommand: str,
        *,
        wait_timeout: int = 3,
        process_timeout: int = 20,
    ) -> tuple[subprocess.CompletedProcess[str], float]:
        with tempfile.TemporaryDirectory(prefix="anila-docker-stub-") as temp:
            stub_dir = Path(temp)
            docker = stub_dir / "docker"
            docker.write_text(DOCKER_STUB, encoding="utf-8", newline="\n")
            docker.chmod(0o755)

            env = os.environ.copy()
            env["PATH"] = os.pathsep.join((str(stub_dir), env.get("PATH", "")))
            env["DOCKER_STUB_SCENARIO"] = scenario
            env["ANILA_WAIT_TIMEOUT_SECONDS"] = str(wait_timeout)
            env.pop("COMPOSE_PROJECT_NAME", None)

            started = time.monotonic()
            result = subprocess.run(
                [self.bash, str(DEPLOY_SCRIPT), subcommand],
                cwd=ROOT,
                env=env,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=process_timeout,
            )
            return result, time.monotonic() - started

    def assert_guard_failed(
        self, scenario: str, subcommand: str, expected: str
    ) -> subprocess.CompletedProcess[str]:
        result, _ = self.run_deploy(scenario, subcommand)
        output = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0, output)
        self.assertIn(expected, output)
        return result

    def test_wait_succeeds_when_every_service_is_ready(self) -> None:
        result, _ = self.run_deploy("all-healthy", "wait")
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        self.assertIn("全部 healthy", output)

    def test_wait_fails_immediately_for_terminal_health_states(self) -> None:
        for scenario, expected in (
            ("unhealthy", "(unhealthy)"),
            ("restarting", "Restarting"),
            ("exited", "Exited"),
        ):
            with self.subTest(scenario=scenario):
                result, elapsed = self.run_deploy(scenario, "wait")
                output = result.stdout + result.stderr
                self.assertNotEqual(result.returncode, 0, output)
                self.assertIn(expected, output)
                self.assertLess(elapsed, 5, output)

    def test_wait_times_out_for_starting_or_missing_services(self) -> None:
        for scenario in ("starting", "missing"):
            with self.subTest(scenario=scenario):
                result, _ = self.run_deploy(scenario, "wait")
                output = result.stdout + result.stderr
                self.assertNotEqual(result.returncode, 0, output)
                self.assertIn("秒內仍有 service 未 ready/healthy", output)
                self.assertIn("csp", output)

    def test_wait_fails_immediately_when_ingestion_exits(self) -> None:
        result, elapsed = self.run_deploy("ingestion-exited", "wait")
        output = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0, output)
        self.assertIn("ingestion-worker", output)
        self.assertIn("Exited", output)
        self.assertLess(elapsed, 5, output)

    def test_tool_guard_accepts_a_fresh_install(self) -> None:
        result, _ = self.run_deploy("all-healthy", "tool-preflight")
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        self.assertIn("fresh install", output)

    def test_tool_guard_rejects_an_old_n8n_container_image(self) -> None:
        self.assert_guard_failed(
            "old-n8n-image", "tool-preflight", "n8n image 版本護欄拒絕啟動"
        )

    def test_tool_guard_rejects_an_unmarked_existing_volume(self) -> None:
        self.assert_guard_failed(
            "unmarked-n8n-volume",
            "tool-preflight",
            "n8n 有既有資料卷但沒有通過目標版本驗證標記",
        )

    def test_tool_guard_rejects_a_partial_gitlab_restore(self) -> None:
        self.assert_guard_failed(
            "partial-gitlab-volume",
            "tool-preflight",
            "GitLab config/data volume 只剩一側",
        )


if __name__ == "__main__":
    unittest.main()
