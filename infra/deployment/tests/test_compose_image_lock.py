from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[3]
INTRANET = ROOT / "infra/deployment/intranet"
SCRIPT = ROOT / "infra/deployment/scripts/verify-compose-image-lock.py"
SPEC = importlib.util.spec_from_file_location("verify_compose_image_lock", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class ComposeImageLockTests(unittest.TestCase):
    def test_compose_control_guards_are_nounset_safe_and_reject_empty_values(
        self,
    ) -> None:
        guard = r"""
set -euo pipefail
guard() {
  local variable
  for variable in COMPOSE_FILE COMPOSE_PROFILES COMPOSE_PROJECT_NAME \
    COMPOSE_ENV_FILES COMPOSE_DISABLE_ENV_FILE COMPOSE_PATH_SEPARATOR; do
    [[ ! -v "$variable" ]] || return 42
  done
}
unset COMPOSE_FILE COMPOSE_PROFILES COMPOSE_PROJECT_NAME \
  COMPOSE_ENV_FILES COMPOSE_DISABLE_ENV_FILE COMPOSE_PATH_SEPARATOR
guard
COMPOSE_FILE=
set +e
guard
status=$?
set -e
[[ $status -eq 42 ]]
"""
        if os.name == "nt":
            wsl = shutil.which("wsl.exe")
            self.assertIsNotNone(wsl, "WSL is required for the Windows Bash contract")
            command = [str(wsl), "--exec", "bash", "-c", guard]
        else:
            bash = shutil.which("bash")
            self.assertIsNotNone(bash, "bash is required for deployment contracts")
            command = [str(bash), "-c", guard]
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

        exporter = (INTRANET / "build-and-export-for-intranet.sh").read_text(
            encoding="utf-8"
        )
        ops = (ROOT / "infra/deployment/scripts/anila-ops.sh").read_text(
            encoding="utf-8"
        )
        deploy = (ROOT / "infra/deployment/scripts/deploy-prod.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn('[[ -v "$compose_variable" ]]', exporter)
        self.assertIn('[[ ! -v "$compose_variable" ]]', ops)
        self.assertIn('[[ ! -v "$variable" ]]', deploy)

    def setUp(self) -> None:
        self.inventory = MODULE.read_inventory(MODULE.DEFAULT_INVENTORY)

    def _lock_text(self) -> str:
        rows = ["# service\timage\timage_id\tbundle\tactivation"]
        for index, entry in enumerate(self.inventory.values(), 1):
            image_id = "sha256:" + f"{index:064x}"
            rows.append(
                "\t".join(
                    (
                        entry.service,
                        entry.image,
                        image_id,
                        entry.bundle,
                        entry.activation,
                    )
                )
            )
        return "\n".join(rows) + "\n"

    def _env_text(
        self,
        *,
        include_optional: bool = False,
        profile: str = "prod-intranet-card",
    ) -> str:
        rows: list[str] = [f"ANILA_DEPLOYMENT_PROFILE={profile}"]
        for index, entry in enumerate(self.inventory.values(), 1):
            if entry.activation != "default" and not include_optional:
                continue
            variable = MODULE.IMAGE_ENV_BY_SERVICE[entry.service]
            rows.append(f"{variable}=sha256:{index:064x}")
        return "\n".join(rows) + "\n"

    def test_all_named_formal_profiles_are_accepted_and_unknown_is_rejected(self):
        accepted = (
            "prod-intranet-card",
            "prod-intranet-card-breakglass",
            "prod-public-passwd",
            "prod-military-passwd",
            "trial-military",
        )
        for profile in accepted:
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as directory:
                env_path = Path(directory) / ".env"
                env_path.write_text(self._env_text(profile=profile), encoding="utf-8")
                if profile == "prod-intranet-card-breakglass":
                    with env_path.open("a", encoding="utf-8") as stream:
                        stream.write(
                            "ANILA_BREAK_GLASS_OWNER=operator-a\n"
                            "ANILA_BREAK_GLASS_TICKET=INC-100\n"
                            "ANILA_BREAK_GLASS_EXPIRES_AT=2026-07-21T00:00:00Z\n"
                        )
                MODULE.verify_env(
                    env_path,
                    self.inventory,
                    None,
                    include_optional=False,
                    inspect_docker=False,
                )

        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            env_path.write_text(
                self._env_text(profile="unknown-formal-profile"), encoding="utf-8"
            )
            with self.assertRaisesRegex(MODULE.ImageLockError, "unsupported formal"):
                MODULE.verify_env(
                    env_path,
                    self.inventory,
                    None,
                    include_optional=False,
                    inspect_docker=False,
                )

    def _pilot_env_text(self) -> str:
        rows = [
            "ANILA_DEPLOYMENT_PROFILE=prod-intranet-card",
            "ANILA_PILOT_MODE=true",
        ]
        for index, entry in enumerate(self.inventory.values(), 1):
            if entry.service not in MODULE.GATE2_PILOT_ACTIVE_SERVICES:
                continue
            variable = MODULE.IMAGE_ENV_BY_SERVICE[entry.service]
            rows.append(f"{variable}=sha256:{index:064x}")
        return "\n".join(rows) + "\n"

    def test_current_compose_wires_every_inventory_service_to_lock_variable(self):
        MODULE.verify_compose_wiring(MODULE.DEFAULT_COMPOSE, self.inventory)

    def test_literal_tag_mutation_is_rejected(self):
        compose = MODULE.DEFAULT_COMPOSE.read_text(encoding="utf-8").replace(
            "image: ${ANILA_IMAGE_CSP_DB:?ANILA_IMAGE_CSP_DB must be a locked image content ID}",
            "image: pgvector/pgvector:pg16",
            1,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "platform.yml"
            path.write_text(compose, encoding="utf-8")
            with self.assertRaisesRegex(MODULE.ImageLockError, "not variable-pinned"):
                MODULE.verify_compose_wiring(path, self.inventory)

    def test_mutable_fallback_is_rejected(self):
        compose = MODULE.DEFAULT_COMPOSE.read_text(encoding="utf-8").replace(
            "${ANILA_IMAGE_CSP_DB:?ANILA_IMAGE_CSP_DB must be a locked image content ID}",
            "${ANILA_IMAGE_CSP_DB:-pgvector/pgvector:pg16}",
            1,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "platform.yml"
            path.write_text(compose, encoding="utf-8")
            with self.assertRaisesRegex(MODULE.ImageLockError, "not variable-pinned"):
                MODULE.verify_compose_wiring(path, self.inventory)

    def test_bundle_lock_and_default_env_are_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock_path = root / "lock.tsv"
            env_path = root / ".env"
            lock_path.write_text(self._lock_text(), encoding="utf-8")
            env_path.write_text(self._env_text(), encoding="utf-8")
            lock = MODULE.read_lock(lock_path, self.inventory)
            MODULE.verify_env(
                env_path,
                self.inventory,
                lock,
                include_optional=False,
                inspect_docker=False,
            )

    def test_gate2_pilot_requires_only_exact_active_service_pins(self):
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            env_path.write_text(self._pilot_env_text(), encoding="utf-8")
            MODULE.verify_env(
                env_path,
                self.inventory,
                None,
                include_optional=False,
                inspect_docker=False,
                posture="gate2-pilot",
            )

    def test_gate2_pilot_rejects_optional_profile_activation(self):
        with self.assertRaisesRegex(
            MODULE.ImageLockError, "forbids optional Compose profiles"
        ):
            MODULE.active_services(
                self.inventory,
                posture="gate2-pilot",
                include_optional=True,
            )

    def test_mutable_tag_in_formal_env_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            env_path.write_text(
                self._env_text().replace(
                    "ANILA_IMAGE_CSP=sha256:", "ANILA_IMAGE_CSP=anila:"
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(MODULE.ImageLockError, "ANILA_IMAGE_CSP"):
                MODULE.verify_env(
                    env_path,
                    self.inventory,
                    None,
                    include_optional=False,
                    inspect_docker=False,
                )

    def test_optional_profile_requires_its_own_pin_when_enabled(self):
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            env_path.write_text(self._env_text(), encoding="utf-8")
            first_optional = next(
                entry
                for entry in self.inventory.values()
                if entry.activation != "default"
            )
            expected_variable = MODULE.IMAGE_ENV_BY_SERVICE[first_optional.service]
            with self.assertRaisesRegex(MODULE.ImageLockError, expected_variable):
                MODULE.verify_env(
                    env_path,
                    self.inventory,
                    None,
                    include_optional=True,
                    inspect_docker=False,
                )

    def test_resolved_compose_shell_override_is_rejected(self):
        values = {
            line.split("=", 1)[0]: line.split("=", 1)[1]
            for line in self._env_text().splitlines()
        }
        services = {
            entry.service: {"image": values[MODULE.IMAGE_ENV_BY_SERVICE[entry.service]]}
            for entry in self.inventory.values()
            if entry.activation == "default"
        }
        services["csp"]["environment"] = {
            "ANILA_DEPLOYMENT_PROFILE": "prod-intranet-card",
            "ANILA_BREAK_GLASS_OWNER": "",
            "ANILA_BREAK_GLASS_TICKET": "",
            "ANILA_BREAK_GLASS_EXPIRES_AT": "",
        }
        services["csp"]["image"] = "sha256:" + "f" * 64
        with self.assertRaisesRegex(MODULE.ImageLockError, "override for csp"):
            MODULE.verify_resolved_compose_images(
                values,
                self.inventory,
                {"name": "anila-platform", "services": services},
                include_optional=False,
            )

    def test_unexpected_active_optional_profile_is_rejected(self):
        values = {
            line.split("=", 1)[0]: line.split("=", 1)[1]
            for line in self._env_text(include_optional=True).splitlines()
        }
        services = {
            entry.service: {"image": values[MODULE.IMAGE_ENV_BY_SERVICE[entry.service]]}
            for entry in self.inventory.values()
        }
        services["csp"]["environment"] = {
            "ANILA_DEPLOYMENT_PROFILE": "prod-intranet-card",
            "ANILA_BREAK_GLASS_OWNER": "",
            "ANILA_BREAK_GLASS_TICKET": "",
            "ANILA_BREAK_GLASS_EXPIRES_AT": "",
        }
        with self.assertRaisesRegex(MODULE.ImageLockError, "unexpected=.*codeserver"):
            MODULE.verify_resolved_compose_images(
                values,
                self.inventory,
                {"name": "anila-platform", "services": services},
                include_optional=False,
            )

    def test_resolved_project_identity_drift_is_rejected(self):
        values = {
            line.split("=", 1)[0]: line.split("=", 1)[1]
            for line in self._env_text().splitlines()
        }
        services = {
            entry.service: {"image": values[MODULE.IMAGE_ENV_BY_SERVICE[entry.service]]}
            for entry in self.inventory.values()
            if entry.activation == "default"
        }
        services["csp"]["environment"] = {
            "ANILA_DEPLOYMENT_PROFILE": "prod-intranet-card",
            "ANILA_BREAK_GLASS_OWNER": "",
            "ANILA_BREAK_GLASS_TICKET": "",
            "ANILA_BREAK_GLASS_EXPIRES_AT": "",
        }
        with self.assertRaisesRegex(MODULE.ImageLockError, "project identity drift"):
            MODULE.verify_resolved_compose_images(
                values,
                self.inventory,
                {"name": "wrong-project", "services": services},
                include_optional=False,
            )

    def test_gate2_pilot_resolved_service_set_and_images_are_exact(self):
        values = {
            line.split("=", 1)[0]: line.split("=", 1)[1]
            for line in self._pilot_env_text().splitlines()
        }
        services = {
            service: {"image": values[MODULE.IMAGE_ENV_BY_SERVICE[service]]}
            for service in MODULE.GATE2_PILOT_ACTIVE_SERVICES
        }
        services["csp"]["environment"] = {
            "ANILA_DEPLOYMENT_PROFILE": "prod-intranet-card",
            "ANILA_BREAK_GLASS_OWNER": "",
            "ANILA_BREAK_GLASS_TICKET": "",
            "ANILA_BREAK_GLASS_EXPIRES_AT": "",
            "ANILA_PILOT_MODE": "true",
            "GATE2_PILOT_COMPOSE_POSTURE": "gate2-pilot-v1",
        }
        config = {"name": "anila-platform", "services": services}
        MODULE.verify_resolved_compose_images(
            values,
            self.inventory,
            config,
            include_optional=False,
            posture="gate2-pilot",
        )

        for mutation, expected_error in (
            ({**services, "ingestion-worker": {"image": "sha256:" + "f" * 64}}, "unexpected"),
            ({name: value for name, value in services.items() if name != "router"}, "missing"),
        ):
            with (
                self.subTest(expected_error=expected_error),
                self.assertRaisesRegex(MODULE.ImageLockError, expected_error),
            ):
                MODULE.verify_resolved_compose_images(
                    values,
                    self.inventory,
                    {"name": "anila-platform", "services": mutation},
                    include_optional=False,
                    posture="gate2-pilot",
                )

    def test_gate2_pilot_env_and_resolved_marker_must_match_posture(self):
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            env_path.write_text(self._env_text(), encoding="utf-8")
            with self.assertRaisesRegex(MODULE.ImageLockError, "ANILA_PILOT_MODE=true"):
                MODULE.verify_env(
                    env_path,
                    self.inventory,
                    None,
                    include_optional=False,
                    inspect_docker=False,
                    posture="gate2-pilot",
                )

        values = {
            line.split("=", 1)[0]: line.split("=", 1)[1]
            for line in self._pilot_env_text().splitlines()
        }
        services = {
            service: {"image": values[MODULE.IMAGE_ENV_BY_SERVICE[service]]}
            for service in MODULE.GATE2_PILOT_ACTIVE_SERVICES
        }
        services["csp"]["environment"] = {
            "ANILA_DEPLOYMENT_PROFILE": "prod-intranet-card",
            "ANILA_BREAK_GLASS_OWNER": "",
            "ANILA_BREAK_GLASS_TICKET": "",
            "ANILA_BREAK_GLASS_EXPIRES_AT": "",
            "ANILA_PILOT_MODE": "true",
        }
        with self.assertRaisesRegex(MODULE.ImageLockError, "posture marker"):
            MODULE.verify_resolved_compose_images(
                values,
                self.inventory,
                {"name": "anila-platform", "services": services},
                include_optional=False,
                posture="gate2-pilot",
            )

    def test_breakglass_metadata_shell_override_is_rejected(self):
        values = {
            line.split("=", 1)[0]: line.split("=", 1)[1]
            for line in self._env_text().splitlines()
        }
        values.update(
            {
                "ANILA_DEPLOYMENT_PROFILE": "prod-intranet-card-breakglass",
                "ANILA_BREAK_GLASS_OWNER": "operator-a",
                "ANILA_BREAK_GLASS_TICKET": "INC-100",
                "ANILA_BREAK_GLASS_EXPIRES_AT": "2026-07-12T23:00:00Z",
            }
        )
        services = {
            entry.service: {"image": values[MODULE.IMAGE_ENV_BY_SERVICE[entry.service]]}
            for entry in self.inventory.values()
            if entry.activation == "default"
        }
        services["csp"]["environment"] = {
            "ANILA_DEPLOYMENT_PROFILE": "prod-intranet-card-breakglass",
            "ANILA_BREAK_GLASS_OWNER": "operator-a",
            "ANILA_BREAK_GLASS_TICKET": "INC-OTHER",
            "ANILA_BREAK_GLASS_EXPIRES_AT": "2026-07-13T10:00:00Z",
        }
        with self.assertRaisesRegex(MODULE.ImageLockError, "ANILA_BREAK_GLASS_TICKET"):
            MODULE.verify_resolved_compose_images(
                values,
                self.inventory,
                {"name": "anila-platform", "services": services},
                include_optional=False,
            )

    def test_dotenv_compose_control_variables_are_rejected(self):
        for variable in ("COMPOSE_PROJECT_NAME", "COMPOSE_PROFILES"):
            with (
                self.subTest(variable=variable),
                tempfile.TemporaryDirectory() as directory,
            ):
                env_path = Path(directory) / ".env"
                env_path.write_text(
                    self._env_text() + f"{variable}=unexpected\n",
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(MODULE.ImageLockError, variable):
                    MODULE.verify_env(
                        env_path,
                        self.inventory,
                        None,
                        include_optional=False,
                        inspect_docker=False,
                    )

    def test_build_env_uses_inventory_tags_and_canonical_profile(self):
        rows = [
            ("ANILA_DEPLOYMENT_PROFILE", "prod-intranet-card"),
            ("ANILA_HOST", "anila.inventory-check.invalid"),
            *[
                (MODULE.IMAGE_ENV_BY_SERVICE[service], entry.image)
                for service, entry in self.inventory.items()
            ],
        ]
        self.assertEqual(len(rows), len(self.inventory) + 2)
        self.assertEqual(dict(rows)["ANILA_HOST"], "anila.inventory-check.invalid")
        self.assertEqual(dict(rows)["ANILA_IMAGE_CSP_DB"], "pgvector/pgvector:pg16")

    def test_ambient_compose_control_variable_is_rejected_before_render(self):
        with mock.patch.dict(os.environ, {"COMPOSE_PROFILES": "developer-tools"}):
            with self.assertRaisesRegex(MODULE.ImageLockError, "COMPOSE_PROFILES"):
                MODULE.resolved_compose_config(
                    Path("formal.env"), include_optional=False
                )

    def test_empty_ambient_compose_control_variable_is_rejected(self):
        with mock.patch.dict(os.environ, {"COMPOSE_FILE": ""}):
            with self.assertRaisesRegex(MODULE.ImageLockError, "COMPOSE_FILE"):
                MODULE.resolved_compose_config(
                    Path("formal.env"),
                    include_optional=False,
                    posture="gate2-pilot",
                )

    def test_gate2_pilot_render_uses_base_and_overlay_with_canonical_project(self):
        rendered = {"name": "anila-platform", "services": {}}
        completed = subprocess.CompletedProcess(
            ["docker", "compose"], 0, __import__("json").dumps(rendered), ""
        )
        with mock.patch.object(MODULE.subprocess, "run", return_value=completed) as run:
            self.assertEqual(
                MODULE.resolved_compose_config(
                    Path("formal.env"),
                    include_optional=False,
                    posture="gate2-pilot",
                ),
                rendered,
            )
        command = run.call_args.args[0]
        self.assertEqual(command[:4], ["docker", "compose", "--project-name", "anila-platform"])
        self.assertEqual(
            [command[index + 1] for index, value in enumerate(command) if value == "--file"],
            [str(MODULE.DEFAULT_COMPOSE.resolve()), str(MODULE.GATE2_PILOT_COMPOSE.resolve())],
        )

    def test_running_container_immutable_ids_are_read_back(self):
        values = {
            line.split("=", 1)[0]: line.split("=", 1)[1]
            for line in self._env_text().splitlines()
        }
        expected_services = sorted(
            entry.service
            for entry in self.inventory.values()
            if entry.activation == "default"
        )

        def run(command):
            if "--services" in command:
                return subprocess.CompletedProcess(
                    command, 0, "\n".join(expected_services) + "\n", ""
                )
            if command[:3] == ["docker", "container", "inspect"]:
                service = command[-1].removeprefix("cid-")
                return subprocess.CompletedProcess(
                    command,
                    0,
                    values[MODULE.IMAGE_ENV_BY_SERVICE[service]] + "\n",
                    "",
                )
            service = command[-1]
            return subprocess.CompletedProcess(command, 0, f"cid-{service}\n", "")

        with mock.patch.object(MODULE, "_run_utf8", side_effect=run):
            MODULE.verify_running_containers(
                values,
                self.inventory,
                Path("formal.env"),
                include_optional=False,
            )

    def test_gate2_pilot_running_set_and_every_image_id_are_read_back(self):
        values = {
            line.split("=", 1)[0]: line.split("=", 1)[1]
            for line in self._pilot_env_text().splitlines()
        }
        expected_services = sorted(MODULE.GATE2_PILOT_ACTIVE_SERVICES)
        inspected: list[str] = []

        def run(command):
            if "--services" in command:
                return subprocess.CompletedProcess(
                    command, 0, "\n".join(expected_services) + "\n", ""
                )
            if command[:3] == ["docker", "container", "inspect"]:
                service = command[-1].removeprefix("cid-")
                inspected.append(service)
                return subprocess.CompletedProcess(
                    command,
                    0,
                    values[MODULE.IMAGE_ENV_BY_SERVICE[service]] + "\n",
                    "",
                )
            service = command[-1]
            return subprocess.CompletedProcess(command, 0, f"cid-{service}\n", "")

        with mock.patch.object(MODULE, "_run_utf8", side_effect=run):
            MODULE.verify_running_containers(
                values,
                self.inventory,
                Path("formal.env"),
                include_optional=False,
                posture="gate2-pilot",
            )
        self.assertEqual(inspected, expected_services)

    def test_gate2_pilot_running_service_set_rejects_missing_and_unexpected(self):
        values = {
            line.split("=", 1)[0]: line.split("=", 1)[1]
            for line in self._pilot_env_text().splitlines()
        }
        expected = set(MODULE.GATE2_PILOT_ACTIVE_SERVICES)
        mutations = (
            (expected - {"router"}, "missing"),
            (expected | {"ingestion-worker"}, "unexpected"),
        )
        for running, error in mutations:
            completed = subprocess.CompletedProcess(
                ["docker", "compose"], 0, "\n".join(sorted(running)) + "\n", ""
            )
            with (
                self.subTest(error=error),
                mock.patch.object(MODULE, "_run_utf8", return_value=completed),
                self.assertRaisesRegex(MODULE.ImageLockError, error),
            ):
                MODULE.verify_running_containers(
                    values,
                    self.inventory,
                    Path("formal.env"),
                    include_optional=False,
                    posture="gate2-pilot",
                )

    def test_gate2_pilot_running_image_drift_is_rejected(self):
        values = {
            line.split("=", 1)[0]: line.split("=", 1)[1]
            for line in self._pilot_env_text().splitlines()
        }
        expected_services = sorted(MODULE.GATE2_PILOT_ACTIVE_SERVICES)

        def run(command):
            if "--services" in command:
                return subprocess.CompletedProcess(
                    command, 0, "\n".join(expected_services) + "\n", ""
                )
            if command[:3] == ["docker", "container", "inspect"]:
                service = command[-1].removeprefix("cid-")
                image_id = values[MODULE.IMAGE_ENV_BY_SERVICE[service]]
                if service == "router":
                    image_id = "sha256:" + "f" * 64
                return subprocess.CompletedProcess(command, 0, image_id + "\n", "")
            return subprocess.CompletedProcess(command, 0, f"cid-{command[-1]}\n", "")

        with (
            mock.patch.object(MODULE, "_run_utf8", side_effect=run),
            self.assertRaisesRegex(
                MODULE.ImageLockError, "running container image drift for router"
            ),
        ):
            MODULE.verify_running_containers(
                values,
                self.inventory,
                Path("formal.env"),
                include_optional=False,
                posture="gate2-pilot",
            )

    def test_cli_supports_gate2_pilot_posture_and_defaults_to_normal(self):
        parser = MODULE._parser()
        normal = parser.parse_args(["check-wiring"])
        pilot = parser.parse_args(
            ["verify-env", "--posture", "gate2-pilot", "--env-file", ".env"]
        )
        self.assertEqual(normal.posture, "normal")
        self.assertEqual(pilot.posture, "gate2-pilot")

    def test_intranet_bootstrap_installs_and_verifies_bundle_lock(self):
        script = (
            ROOT / "infra/deployment/intranet/intranet-deploy.sh"
        ).read_text(encoding="utf-8")
        self.assertIn("set_env ANILA_DEPLOYMENT_PROFILE    prod-intranet-card", script)
        self.assertIn("emit-env", script)
        self.assertIn('set_env "$image_variable" "$image_id"', script)
        self.assertIn("--lock \"$BUNDLE/PLATFORM-IMAGE-LOCK.tsv\"", script)
        self.assertIn("verify-env --env-file .env", script)
        self.assertIn("--inspect-docker", script)

    def test_prod_lifecycle_rechecks_lock_before_formal_operations(self):
        script = (
            ROOT / "infra/deployment/scripts/deploy-prod.sh"
        ).read_text(encoding="utf-8")
        self.assertIn("check_formal_image_lock()", script)
        identity = script[
            script.index("validate_formal_identity() {") :
            script.index("validate_formal_up() {")
        ]
        formal_up = script[
            script.index("validate_formal_up() {") :
            script.index("# ── Subcommand: deploy")
        ]
        restart = script[
            script.index("cmd_restart() {") :
            script.index("# ── Opt-in developer tool")
        ]
        self.assertIn("check_formal_image_lock", identity)
        self.assertIn("validate_formal_identity", formal_up)
        self.assertIn("check_tool_upgrade_safety", formal_up)
        self.assertLess(restart.index("validate_formal_up"), restart.index("cmd_down"))
        self.assertIn("Use preloaded content-ID-locked images", script)
        self.assertIn("--include-optional --inspect-docker", script)
        self.assertIn("--inspect-containers", script)


if __name__ == "__main__":
    unittest.main()
