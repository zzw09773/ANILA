from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Callable

import yaml


ROOT = Path(__file__).parents[3]
PROFILE_PATH = (
    ROOT / "infra/deployment/backup/production-backup-profile.v1.json"
)
COMPOSE_PATH = ROOT / "infra/compose/platform.yml"
VERIFIER_PATH = (
    ROOT / "infra/deployment/scripts/verify-production-backup-profile.py"
)

_SPEC = importlib.util.spec_from_file_location(
    "verify_production_backup_profile", VERIFIER_PATH
)
assert _SPEC is not None and _SPEC.loader is not None
_VERIFIER = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_VERIFIER)

BackupProfileError = _VERIFIER.BackupProfileError
verify_profile = _VERIFIER.verify_profile


def _profile() -> dict[str, Any]:
    return json.loads(PROFILE_PATH.read_text(encoding="utf-8"))


def _surface(profile: dict[str, Any], name: str) -> dict[str, Any]:
    return next(
        surface
        for surface in profile["surfaces"]
        if surface["source"]["name"] == name
    )


def _write_profile(directory: Path, value: dict[str, Any]) -> Path:
    path = directory / "profile.json"
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


def _write_compose(directory: Path, value: dict[str, Any]) -> Path:
    path = directory / "platform.yml"
    path.write_text(
        yaml.safe_dump(value, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return path


class ProductionBackupProfileTests(unittest.TestCase):
    def test_repository_profile_exactly_covers_production_compose(self) -> None:
        self.assertEqual(
            verify_profile(profile_path=PROFILE_PATH, compose_path=COMPOSE_PATH),
            {
                "derivable": 4,
                "excluded": 2,
                "required": 17,
                "total": 23,
            },
        )

    def test_mandatory_application_and_tool_state_is_required(self) -> None:
        profile = _profile()
        required = {
            surface["source"]["name"]
            for surface in profile["surfaces"]
            if surface["disposition"] == "required"
        }
        self.assertLessEqual(
            {
                "csp-pgdata",
                "../../share/uploads/ingestion",
                "csp-attachments",
                (
                    "${ANILA_STATE_DIR:?ANILA_STATE_DIR must be an absolute "
                    "path outside the repo}/source-snapshots"
                ),
                (
                    "${ANILA_STATE_DIR:?ANILA_STATE_DIR must be an absolute "
                    "path outside the repo}/artifact-blobs"
                ),
                "anila-studio-artifacts",
                "redis-data",
                "n8n_data",
                "gitlab_config",
                "gitlab_logs",
                "gitlab_data",
            },
            required,
        )

    def test_cli_reports_machine_readable_coverage_counts(self) -> None:
        result = subprocess.run(
            [sys.executable, str(VERIFIER_PATH)],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "total=23, required=17, derivable=4, excluded=2", result.stdout
        )

    def _assert_profile_rejected(
        self,
        mutate: Callable[[dict[str, Any]], None],
        match: str,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            profile = _profile()
            mutate(profile)
            with self.assertRaisesRegex(BackupProfileError, match):
                verify_profile(
                    profile_path=_write_profile(directory, profile),
                    compose_path=COMPOSE_PATH,
                )

    def test_invalid_or_fail_open_profile_values_are_rejected(self) -> None:
        cases: list[tuple[str, Callable[[dict[str, Any]], None], str]] = [
            (
                "schema",
                lambda profile: profile.update(schema_version="anila.backup.v0"),
                "unsupported.*schema",
            ),
            (
                "fail-open policy",
                lambda profile: profile["policy"].update(
                    default_disposition="allow"
                ),
                "fail-closed",
            ),
            (
                "unknown top-level key",
                lambda profile: profile.update(unreviewed_default="allow"),
                "profile keys invalid",
            ),
            (
                "invalid disposition",
                lambda profile: _surface(profile, "codeserver_config").update(
                    disposition="optional"
                ),
                "invalid disposition",
            ),
            (
                "excluded owner missing",
                lambda profile: _surface(profile, "codeserver_config").update(
                    owner=""
                ),
                "owner.*meaningful",
            ),
            (
                "excluded reason placeholder",
                lambda profile: _surface(profile, "codeserver_config").update(
                    reason="none"
                ),
                "reason.*meaningful|placeholder",
            ),
            (
                "excluded acceptance gate placeholder",
                lambda profile: _surface(profile, "codeserver_config").update(
                    acceptance_gate="TBD"
                ),
                "acceptance_gate.*placeholder|acceptance gate is invalid",
            ),
            (
                "mandatory surface excluded",
                lambda profile: _surface(profile, "csp-pgdata").update(
                    disposition="excluded", backup_method="none"
                ),
                "mandatory.*must be required",
            ),
            (
                "required surface has no backup",
                lambda profile: _surface(profile, "redis-data").update(
                    backup_method="none"
                ),
                "required surface has non-backup method",
            ),
            (
                "boolean restore order",
                lambda profile: _surface(profile, "codeserver_config").update(
                    restore_order=True
                ),
                "restore_order must be an integer",
            ),
            (
                "unauthenticated encryption",
                lambda profile: profile["automation"]["encryption"].update(
                    tool="tar"
                ),
                "automation.encryption.tool must be age",
            ),
            (
                "invalid retention",
                lambda profile: profile["automation"]["retention"].update(
                    default_keep=0
                ),
                "retention default_keep must be 1..3650",
            ),
            (
                "missing key reference",
                lambda profile: profile["automation"][
                    "external_references"
                ].pop("ingress-tls-key-reference"),
                "external reference inventory drifted",
            ),
        ]
        for label, mutate, match in cases:
            with self.subTest(label=label):
                self._assert_profile_rejected(mutate, match)

    def test_missing_surface_is_rejected(self) -> None:
        self._assert_profile_rejected(
            lambda profile: profile.update(surfaces=profile["surfaces"][1:]),
            "coverage mismatch.*missing",
        )

    def test_extra_surface_is_rejected(self) -> None:
        def mutate(profile: dict[str, Any]) -> None:
            extra = copy.deepcopy(profile["surfaces"][-1])
            extra["id"] = "unreviewed-extra-state"
            extra["source"] = {
                "kind": "bind",
                "name": "/unreviewed/state",
                "mounts": [
                    {
                        "service": "csp",
                        "target": "/unreviewed/state",
                        "read_only": False,
                    }
                ],
            }
            extra["restore_order"] = 999
            profile["surfaces"].append(extra)

        self._assert_profile_rejected(mutate, "coverage mismatch.*extra")

    def test_duplicate_surface_id_and_source_are_rejected(self) -> None:
        def mutate(profile: dict[str, Any]) -> None:
            duplicate = copy.deepcopy(profile["surfaces"][0])
            duplicate["restore_order"] = 998
            profile["surfaces"].append(duplicate)

        self._assert_profile_rejected(mutate, "duplicate surface id")

    def test_duplicate_mount_inside_surface_is_rejected(self) -> None:
        def mutate(profile: dict[str, Any]) -> None:
            surface = _surface(profile, "../../share/uploads/ingestion")
            surface["source"]["mounts"].append(
                copy.deepcopy(surface["source"]["mounts"][0])
            )

        self._assert_profile_rejected(mutate, "duplicate mount")

    def test_profile_mount_drift_is_rejected(self) -> None:
        def mutate(profile: dict[str, Any]) -> None:
            _surface(profile, "n8n_data")["source"]["mounts"][0][
                "target"
            ] = "/wrong"

        self._assert_profile_rejected(mutate, "mount inventory drift")

    def _assert_compose_rejected(
        self,
        mutate: Callable[[dict[str, Any]], None],
        match: str,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            compose = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
            mutate(compose)
            with self.assertRaisesRegex(BackupProfileError, match):
                verify_profile(
                    profile_path=PROFILE_PATH,
                    compose_path=_write_compose(directory, compose),
                )

    def test_new_named_volume_in_compose_is_rejected_until_profiled(self) -> None:
        def mutate(compose: dict[str, Any]) -> None:
            compose["volumes"]["new-production-state"] = None
            compose["services"]["csp"]["volumes"].append(
                "new-production-state:/var/lib/anila/new-state"
            )

        self._assert_compose_rejected(mutate, "coverage mismatch.*missing")

    def test_new_writable_bind_in_compose_is_rejected_until_profiled(self) -> None:
        self._assert_compose_rejected(
            lambda compose: compose["services"]["csp"]["volumes"].append(
                "../../share/new-state:/var/lib/anila/new-state"
            ),
            "coverage mismatch.*missing",
        )

    def test_new_read_only_bind_is_rejected_until_profiled(self) -> None:
        self._assert_compose_rejected(
            lambda compose: compose["services"]["csp"]["volumes"].append(
                "../../config/static.json:/etc/anila/static.json:ro"
            ),
            "coverage mismatch.*missing",
        )

    def test_unmounted_declared_named_volume_is_rejected(self) -> None:
        self._assert_compose_rejected(
            lambda compose: compose["volumes"].update({"orphan-state": None}),
            "not mounted",
        )

    def test_duplicate_compose_yaml_key_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            compose_path = Path(raw) / "duplicate.yml"
            compose_path.write_text(
                COMPOSE_PATH.read_text(encoding="utf-8") + "\nvolumes: {}\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(BackupProfileError, "duplicate YAML key"):
                verify_profile(profile_path=PROFILE_PATH, compose_path=compose_path)

    def test_malformed_compose_and_duplicate_json_keys_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            malformed = directory / "malformed.yml"
            malformed.write_text("services: [unterminated", encoding="utf-8")
            with self.assertRaisesRegex(BackupProfileError, "cannot parse"):
                verify_profile(profile_path=PROFILE_PATH, compose_path=malformed)

            duplicate_json = directory / "duplicate.json"
            duplicate_json.write_text(
                '{"schema_version":"a","schema_version":"b"}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(BackupProfileError, "duplicate JSON key"):
                verify_profile(
                    profile_path=duplicate_json,
                    compose_path=COMPOSE_PATH,
                )


if __name__ == "__main__":
    unittest.main()
