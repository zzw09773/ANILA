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

# Formal lifecycle pins project/file identity before every Compose subcommand.
# Normalize those global options so the behavior scenarios below can focus on
# the requested subcommand rather than duplicating Docker Compose parsing.
if [[ "${1:-}" == "compose" ]]; then
  shift
  while [[ "${1:-}" == "--project-name" || "${1:-}" == "-p" || "${1:-}" == "-f" ]]; do
    shift 2
  done
  set -- compose "$@"
fi

if [[ "${1:-}" == "compose" && "${2:-}" == "down" ]]; then
  printf '%s\n' 'DOCKER_DOWN_CALLED'
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
if [[ "${1:-}" == "compose" && "${2:-}" == "exec" && "${3:-}" == "-T" && "${4:-}" == "gitlab" ]]; then
  [[ "$*" == *"signup_enabled: false"* ]] || {
    printf '%s\n' 'postconfigure did not request signup=false' >&2
    exit 96
  }
  [[ "$scenario" != "gitlab-postconfigure-fails" ]] || exit 42
  printf '%s\n' 'ANILA_GITLAB_SIGNUP=false'
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
        branch: str = "prod-intranet-card",
        profile: str = "prod-intranet-card",
        pilot_mode: str | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], float]:
        with tempfile.TemporaryDirectory(prefix="anila-docker-stub-") as temp:
            stub_dir = Path(temp)
            test_root = stub_dir / "repo"
            test_script = test_root / "infra/deployment/scripts/deploy-prod.sh"
            test_script.parent.mkdir(parents=True)
            shutil.copy2(DEPLOY_SCRIPT, test_script)
            subprocess.run(
                ["git", "init", "-q", "-b", branch],
                cwd=test_root,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "add", "."], cwd=test_root, check=True, capture_output=True
            )
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=ANILA CI",
                    "-c",
                    "user.email=ci@anila.invalid",
                    "commit",
                    "-qm",
                    "test fixture",
                ],
                cwd=test_root,
                check=True,
                capture_output=True,
            )
            docker = stub_dir / "docker"
            docker.write_text(DOCKER_STUB, encoding="utf-8", newline="\n")
            docker.chmod(0o755)
            # Image-lock behavior has its own executable unit suite.  These
            # tests isolate the downstream wait/tool/postconfigure branches,
            # so acknowledge the already-tested verifier boundary here.
            python3 = stub_dir / "python3"
            python3.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8", newline="\n")
            python3.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = os.pathsep.join((str(stub_dir), env.get("PATH", "")))
            env["DOCKER_STUB_SCENARIO"] = scenario
            env["ANILA_WAIT_TIMEOUT_SECONDS"] = str(wait_timeout)
            env["ANILA_DEPLOYMENT_PROFILE"] = profile
            if pilot_mode is None:
                env.pop("ANILA_PILOT_MODE", None)
            else:
                env["ANILA_PILOT_MODE"] = pilot_mode
            for variable in (
                "COMPOSE_FILE",
                "COMPOSE_PROFILES",
                "COMPOSE_PROJECT_NAME",
                "COMPOSE_ENV_FILES",
                "COMPOSE_DISABLE_ENV_FILE",
                "COMPOSE_PATH_SEPARATOR",
            ):
                env.pop(variable, None)

            started = time.monotonic()
            result = subprocess.run(
                [self.bash, str(test_script), subcommand],
                cwd=test_root,
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

    def test_formal_pilot_mode_rejects_ambiguous_boolean_spelling(self) -> None:
        result, _ = self.run_deploy("all-healthy", "help", pilot_mode="1")
        output = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0, output)
        self.assertIn("ANILA_PILOT_MODE 必須是明確的 true/false", output)

    def test_gate2_pilot_wait_uses_the_posture_aware_compose_wrapper(self) -> None:
        result, _ = self.run_deploy("all-healthy", "wait", pilot_mode="true")
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        self.assertIn("全部 healthy", output)

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
                result, elapsed = self.run_deploy(
                    scenario, "wait", wait_timeout=15
                )
                output = result.stdout + result.stderr
                self.assertNotEqual(result.returncode, 0, output)
                self.assertIn(expected, output)
                self.assertLess(elapsed, 10, output)

    def test_wait_times_out_for_starting_or_missing_services(self) -> None:
        for scenario in ("starting", "missing"):
            with self.subTest(scenario=scenario):
                result, _ = self.run_deploy(scenario, "wait")
                output = result.stdout + result.stderr
                self.assertNotEqual(result.returncode, 0, output)
                self.assertIn("秒內仍有 service 未 ready/healthy", output)
                self.assertIn("csp", output)

    def test_wait_fails_immediately_when_ingestion_exits(self) -> None:
        result, elapsed = self.run_deploy(
            "ingestion-exited", "wait", wait_timeout=15
        )
        output = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0, output)
        self.assertIn("ingestion-worker", output)
        self.assertIn("Exited", output)
        self.assertLess(elapsed, 10, output)

    def test_tool_guard_accepts_a_fresh_install(self) -> None:
        result, _ = self.run_deploy("all-healthy", "tool-preflight")
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        self.assertIn("fresh install", output)

    def test_card_branch_accepts_normal_and_breakglass_profiles(self) -> None:
        for profile in (
            "prod-intranet-card",
            "prod-intranet-card-breakglass",
        ):
            with self.subTest(profile=profile):
                result, _ = self.run_deploy(
                    "all-healthy", "tool-preflight", profile=profile
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_non_card_prod_branch_cannot_claim_card_profile(self) -> None:
        for branch in ("prod-public-passwd", "prod-military-passwd"):
            with self.subTest(branch=branch):
                result, _ = self.run_deploy(
                    "all-healthy",
                    "tool-preflight",
                    branch=branch,
                    profile="prod-intranet-card",
                )
                output = result.stdout + result.stderr
                self.assertNotEqual(result.returncode, 0, output)
                self.assertIn("deployment identity mismatch", output)

    def test_restart_never_stops_stack_before_identity_and_profile_pass(self) -> None:
        scenarios = (
            {"branch": "prod-public-passwd", "profile": "prod-intranet-card"},
            {"branch": "prod-intranet-card", "profile": "development"},
        )
        for values in scenarios:
            with self.subTest(**values):
                result, _ = self.run_deploy(
                    "all-healthy", "restart", **values
                )
                output = result.stdout + result.stderr
                self.assertNotEqual(result.returncode, 0, output)
                self.assertNotIn("DOCKER_DOWN_CALLED", output)

    def test_postconfigure_rejects_wrong_deployment_identity(self) -> None:
        result, _ = self.run_deploy(
            "all-healthy",
            "postconfigure",
            branch="prod-public-passwd",
            profile="prod-intranet-card",
        )
        output = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0, output)
        self.assertIn("deployment identity mismatch", output)
        self.assertNotIn("ANILA_GITLAB_SIGNUP=false", output)

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


    def test_postconfigure_converges_gitlab_signup_posture(self) -> None:
        result, _ = self.run_deploy("all-healthy", "postconfigure")
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        self.assertIn("GitLab self-signup runtime posture 已收斂", output)

    def test_postconfigure_fails_closed_when_gitlab_rejects_update(self) -> None:
        self.assert_guard_failed(
            "gitlab-postconfigure-fails",
            "postconfigure",
            "GitLab self-signup runtime posture 收斂失敗",
        )


if __name__ == "__main__":
    unittest.main()
