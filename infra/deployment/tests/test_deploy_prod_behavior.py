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
MODEL_NETWORK_HELPER = (
    ROOT / "infra" / "deployment" / "scripts" / "ensure-models-network.sh"
)


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
stub_log="${DEPLOY_STUB_LOG:-}"

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
  project=''
  compose_file=''
  while [[ "${1:-}" == "--project-name" || "${1:-}" == "-p" || "${1:-}" == "-f" || "${1:-}" == "--file" ]]; do
    case "$1" in
      --project-name|-p) project="${2:-}" ;;
      -f|--file) compose_file="${2:-}" ;;
    esac
    shift 2
  done
  if [[ -n "$stub_log" ]]; then
    printf 'COMPOSE project=%s file=%s command=%s\n' \
      "$project" "$compose_file" "${1:-}" >>"$stub_log"
  fi
  set -- compose "$@"
fi

if [[ "${1:-}" == "compose" && "${2:-}" == "config" ]]; then
  if [[ "$compose_file" == *"docker-compose.external-embed.yml" ]]; then
    resolved_project='anila-external-embed'
    standalone_marker='external-embed-only-v1'
    [[ "$scenario" != "external-artifact-mismatch" ]] || resolved_project='anila-models'
    [[ "$scenario" != "external-marker-mismatch" ]] || standalone_marker=''
    printf '{"name":"%s","services":{"nv-embed-proxy":{"expose":["8000"],"user":"10001:10001","read_only":true,"tmpfs":["/tmp"],"cap_drop":["ALL"],"security_opt":["no-new-privileges:true"],"environment":{"TRITON_GRPC_URL":"%s"},"labels":{"com.anila.inference-role":"external-shim","com.anila.provider-locality":"internal_shim","com.anila.egress-network":"embedding-egress","com.anila.upstream-locality":"external_governed","com.anila.upstream-transport":"triton-grpc","com.anila.upstream-egress-policy-id":"%s","com.anila.egress-target":"%s","com.anila.standalone-marker":"%s"},"networks":{"models":null,"embedding-egress":null}}},"networks":{"models":{"external":true,"name":"anila-models-net"},"embedding-egress":{"driver":"bridge","internal":false}}}\n' \
      "$resolved_project" "${TRITON_GRPC_URL:-}" "${ANILA_EXTERNAL_EMBED_UPSTREAM_EGRESS_POLICY_ID:-egress.embedding}" "${TRITON_GRPC_URL:-}" "$standalone_marker"
  elif [[ "$compose_file" == *"infra/models/docker-compose.yml" ]]; then
    printf '%s\n' '{"name":"anila-models","services":{"nv-embed-proxy":{},"nv-embed-triton":{}}}'
  else
    printf '%s\n' '{"name":"anila-platform","services":{}}'
  fi
  exit 0
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
        embedding_topology: str | None = None,
        triton_grpc_url: str | None = None,
        egress_only: bool = False,
    ) -> tuple[subprocess.CompletedProcess[str], float]:
        # The managed runner mounts /tmp noexec, so an executable ``docker``
        # stub created there cannot be launched by the Bash fixture.  Keep the
        # whole hermetic fixture under the executable checkout instead.
        with tempfile.TemporaryDirectory(
            prefix=".anila-docker-stub-", dir=ROOT
        ) as temp:
            stub_dir = Path(temp)
            test_root = stub_dir / "repo"
            test_script = test_root / "infra/deployment/scripts/deploy-prod.sh"
            test_script.parent.mkdir(parents=True)
            shutil.copy2(DEPLOY_SCRIPT, test_script)
            if egress_only:
                script = test_script.read_text(encoding="utf-8")
                entrypoint = script.index("# ── Entrypoint")
                test_script.write_text(
                    script[:entrypoint]
                    + "configure_formal_posture\n"
                    + "check_gate5_egress_policy\n",
                    encoding="utf-8",
                    newline="\n",
                )
            shutil.copy2(
                MODEL_NETWORK_HELPER,
                test_script.parent / MODEL_NETWORK_HELPER.name,
            )
            # Keep every Compose/helper path referenced by the deployment
            # script available inside the copied repo.  The docker stub still
            # owns command behavior; these files only prevent an accidental
            # fallback to a host Docker CLI from changing the test contract.
            for relative in (
                "infra/compose/platform.yml",
                "infra/compose/gate2-pilot.yml",
                "infra/models/docker-compose.yml",
                "infra/models/docker-compose.external-embed.yml",
                "infra/models/external-embed-serve.sh",
                "infra/policy/gate5/check_deployment_egress.py",
            ):
                destination = test_root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / relative, destination)
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
            stub_log = stub_dir / "deploy-stub.log"
            python3 = stub_dir / "python3"
            python3.write_text(
                """#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "-" || "${1:-}" == "-c" ]]; then
  exec "$ANILA_TEST_REAL_PYTHON" "$@"
fi
if [[ "${1:-}" == *check_deployment_egress.py ]]; then
  printf 'CHECKER %s\\n' "$*" >>"$DEPLOY_STUB_LOG"
  while (( $# > 0 )); do
    if [[ "$1" == "--compose-json" && $# -ge 2 ]]; then
      printf 'CHECKER_JSON ' >>"$DEPLOY_STUB_LOG"
      tr -d '\\n' <"$2" >>"$DEPLOY_STUB_LOG"
      printf '\\n' >>"$DEPLOY_STUB_LOG"
      shift 2
    else
      shift
    fi
  done
fi
exit 0
""",
                encoding="utf-8",
                newline="\n",
            )
            python3.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = os.pathsep.join((str(stub_dir), env.get("PATH", "")))
            env["DOCKER_STUB_SCENARIO"] = scenario
            env["DEPLOY_STUB_LOG"] = str(stub_log)
            env["ANILA_TEST_REAL_PYTHON"] = shutil.which("python3") or "python3"
            env["ANILA_WAIT_TIMEOUT_SECONDS"] = str(wait_timeout)
            env["ANILA_DEPLOYMENT_PROFILE"] = profile
            if pilot_mode is None:
                env.pop("ANILA_PILOT_MODE", None)
            else:
                env["ANILA_PILOT_MODE"] = pilot_mode
            env.pop("ANILA_EMBEDDING_TOPOLOGY", None)
            env.pop("TRITON_GRPC_URL", None)
            if embedding_topology is not None:
                env["ANILA_EMBEDDING_TOPOLOGY"] = embedding_topology
            if triton_grpc_url is not None:
                env["TRITON_GRPC_URL"] = triton_grpc_url
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
                [self.bash, str(test_script)] + ([] if egress_only else [subcommand]),
                cwd=test_root,
                env=env,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=process_timeout,
            )
            if stub_log.exists():
                result.stdout += "\n" + stub_log.read_text(
                    encoding="utf-8", errors="replace"
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

    def test_gate5_internal_topology_renders_only_the_base_model_artifact(self) -> None:
        result, _ = self.run_deploy(
            "all-healthy",
            "unused",
            embedding_topology="internal",
            egress_only=True,
        )
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        self.assertIn(
            "COMPOSE project=anila-models file=infra/models/docker-compose.yml command=config",
            output,
        )
        self.assertIn('CHECKER_JSON {"name":"anila-models"', output)
        self.assertNotIn("docker-compose.external-embed.yml", output)

    def test_gate5_external_topology_renders_only_the_standalone_artifact(self) -> None:
        for target in ("172.16.120.35:9001", "embed.example.internal:9001"):
            with self.subTest(target=target):
                result, _ = self.run_deploy(
                    "all-healthy",
                    "unused",
                    embedding_topology="external",
                    triton_grpc_url=target,
                    egress_only=True,
                )
                output = result.stdout + result.stderr
                self.assertEqual(result.returncode, 0, output)
                self.assertIn(
                    "COMPOSE project=anila-external-embed file=/",
                    output,
                )
                self.assertIn(
                    "docker-compose.external-embed.yml command=config", output
                )
                self.assertIn(
                    'CHECKER_JSON {"name":"anila-external-embed"', output
                )
                self.assertIn(f'"TRITON_GRPC_URL":"{target}"', output)
                self.assertNotIn(
                    "project=anila-models file=infra/models/docker-compose.yml",
                    output,
                )

    def test_gate5_formal_preflight_requires_a_valid_explicit_topology(self) -> None:
        for topology in (None, "", "remote", "EXTERNAL"):
            with self.subTest(topology=topology):
                result, _ = self.run_deploy(
                    "all-healthy",
                    "unused",
                    embedding_topology=topology,
                    egress_only=True,
                )
                output = result.stdout + result.stderr
                self.assertNotEqual(result.returncode, 0, output)
                self.assertIn("ANILA_EMBEDDING_TOPOLOGY", output)
                self.assertNotIn("docker-compose.yml command=config", output)

    def test_gate5_external_topology_rejects_missing_or_invalid_target(self) -> None:
        for target in (None, "", "https://172.16.120.35:9001", "172.16.120.35"):
            with self.subTest(target=target):
                result, _ = self.run_deploy(
                    "all-healthy",
                    "unused",
                    embedding_topology="external",
                    triton_grpc_url=target,
                    egress_only=True,
                )
                output = result.stdout + result.stderr
                self.assertNotEqual(result.returncode, 0, output)
                self.assertIn("TRITON_GRPC_URL", output)

    def test_gate5_external_topology_rejects_artifact_or_marker_mismatch(self) -> None:
        for scenario in ("external-artifact-mismatch", "external-marker-mismatch"):
            with self.subTest(scenario=scenario):
                result, _ = self.run_deploy(
                    scenario,
                    "unused",
                    embedding_topology="external",
                    triton_grpc_url="172.16.120.35:9001",
                    egress_only=True,
                )
                output = result.stdout + result.stderr
                self.assertNotEqual(result.returncode, 0, output)
                self.assertIn("external embedding standalone resolved Compose", output)

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
