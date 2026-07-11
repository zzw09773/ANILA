"""Gate 0 contract for the production CSP container runtime user."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
PRODUCTION_DOCKERFILE = REPO_ROOT / "infra" / "docker" / "csp.Dockerfile"
DEAD_DOCKERFILE = REPO_ROOT / "services" / "csp" / "Dockerfile"
INTRANET_DEPLOY = (
    REPO_ROOT / "infra" / "deployment" / "intranet" / "intranet-deploy.sh"
)
PROD_DEPLOY = REPO_ROOT / "infra" / "deployment" / "scripts" / "deploy-prod.sh"
OPS_SCRIPT = REPO_ROOT / "infra" / "deployment" / "scripts" / "anila-ops.sh"
SAFE_BACKUP_HELPER = (
    REPO_ROOT / "infra" / "deployment" / "scripts" / "safe-runtime-backup.py"
)
JWT_GENERATOR = REPO_ROOT / "services" / "csp" / "scripts" / "generate-jwt-keypair.py"
PLATFORM_COMPOSE = REPO_ROOT / "infra" / "compose" / "platform.yml"
DEV_COMPOSE = REPO_ROOT / "infra" / "compose" / "dev.yml"
PLATFORM_INVENTORY = (
    REPO_ROOT / "infra" / "deployment" / "intranet" / "platform-image-inventory.tsv"
)


class CspNonRootContractTests(unittest.TestCase):
    def test_production_image_has_explicit_non_root_user(self) -> None:
        dockerfile = PRODUCTION_DOCKERFILE.read_text(encoding="utf-8")
        self.assertRegex(dockerfile, r"(?m)^USER\s+csp(?::csp)?\s*$")
        self.assertIn("useradd", dockerfile)
        self.assertIn("--uid 10001", dockerfile)
        self.assertIn("--gid 10001", dockerfile)
        self.assertIn("/app/logs /app/secrets", dockerfile)
        self.assertIn("chown csp:csp /app/logs /app/secrets", dockerfile)
        self.assertIn("/var/anila/attachments", dockerfile)
        self.assertNotRegex(dockerfile, r"(?m)^USER\s+(?:root|0)(?::0)?\s*$")

    def test_dead_csp_dockerfile_is_removed(self) -> None:
        self.assertFalse(
            DEAD_DOCKERFILE.exists(),
            "services/csp/Dockerfile is not used by Compose and must not drift",
        )

    def test_intranet_deploy_prepares_non_root_bind_mounts(self) -> None:
        script = INTRANET_DEPLOY.read_text(encoding="utf-8")
        uid_match = re.search(r"CSP_RUNTIME_UID=(\d+)", script)
        self.assertIsNotNone(uid_match)
        runtime_uid = uid_match.group(1)
        self.assertIn(f"useradd --uid {runtime_uid}", PRODUCTION_DOCKERFILE.read_text(encoding="utf-8"))
        self.assertIn("prepare_csp_runtime_mount", script)
        self.assertIn('prepare_csp_runtime_mount "$SECRETS_DIR" 700', script)
        self.assertIn(
            'prepare_csp_runtime_mount "$PWD/share/uploads/ingestion" 700', script
        )
        self.assertIn("docker run --rm --pull never --user 0:0", script)

        load_index = script.index('bash "$BUNDLE/INTRANET-LOAD.sh"')
        prepare_index = script.index('prepare_csp_runtime_mount "$SECRETS_DIR" 700')
        self.assertLess(load_index, prepare_index, "CSP image must be loaded before helper runs")

    def test_daily_prod_lifecycle_prepares_the_same_mount_contract(self) -> None:
        script = PROD_DEPLOY.read_text(encoding="utf-8")
        self.assertIn("CSP_RUNTIME_UID=10001", script)
        self.assertIn("CSP_RUNTIME_GID=10001", script)
        self.assertIn("CSP_RUNTIME_IMAGE=anila-platform-csp:latest", script)
        self.assertIn(
            'prepare_csp_runtime_mount "$ANILA_SECRETS_DIR" 700', script
        )
        self.assertIn(
            'prepare_csp_runtime_mount "$REPO_ROOT/share/uploads/ingestion" 700',
            script,
        )
        self.assertIn("docker run --rm --pull never", script)
        deploy_body = script[script.index("cmd_deploy() {") : script.index("# ── Subcommand: up")]
        self.assertLess(
            deploy_body.index("docker compose build"),
            deploy_body.index("ensure_jwt_keypair"),
            "local deploy must build the non-root image before using it as chmod helper",
        )
        self.assertIn("docker image inspect \"$CSP_RUNTIME_IMAGE\"", script)

    def test_keypair_ensure_runs_inside_csp_and_host_never_stats_mode_0700(self) -> None:
        prod = PROD_DEPLOY.read_text(encoding="utf-8")
        intranet = INTRANET_DEPLOY.read_text(encoding="utf-8")
        ops = OPS_SCRIPT.read_text(encoding="utf-8")

        self.assertNotIn("[[ ! -f secrets/jwt-private.pem", prod)
        self.assertNotIn("[ -f secrets/jwt-private.pem ]", intranet)
        self.assertNotIn("[ -f secrets/jwt-private.pem ]", ops)
        for script in (prod, intranet):
            self.assertIn("generate-jwt-keypair.py --output-dir /out --ensure", script)
            self.assertIn("--pull never --network none --read-only", script)
        self.assertIn("docker compose exec -T csp python -c", ops)
        self.assertIn("get_private_key,get_public_key", ops)

    def test_keypair_ensure_is_idempotent_and_rejects_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            primary = root / "primary"
            other = root / "other"
            primary.mkdir()
            other.mkdir()

            command = [
                sys.executable,
                str(JWT_GENERATOR),
                "--output-dir",
                str(primary),
                "--ensure",
            ]
            subprocess.run(command, check=True, capture_output=True, text=True)
            private_before = (primary / "jwt-private.pem").read_bytes()
            public_before = (primary / "jwt-public.pem").read_bytes()
            subprocess.run(command, check=True, capture_output=True, text=True)
            self.assertEqual((primary / "jwt-private.pem").read_bytes(), private_before)
            self.assertEqual((primary / "jwt-public.pem").read_bytes(), public_before)

            subprocess.run(
                [
                    sys.executable,
                    str(JWT_GENERATOR),
                    "--output-dir",
                    str(other),
                    "--ensure",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            (primary / "jwt-public.pem").write_bytes(
                (other / "jwt-public.pem").read_bytes()
            )
            refused = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("do not match", refused.stderr)

    def test_keypair_ensure_rejects_weak_rsa_key(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            output = Path(raw)
            subprocess.run(
                [
                    sys.executable,
                    str(JWT_GENERATOR),
                    "--output-dir",
                    str(output),
                    "--key-size",
                    "1024",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            refused = subprocess.run(
                [
                    sys.executable,
                    str(JWT_GENERATOR),
                    "--output-dir",
                    str(output),
                    "--ensure",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("too small", refused.stderr)

    def test_formal_lifecycle_never_requests_registry_pull(self) -> None:
        script = PROD_DEPLOY.read_text(encoding="utf-8")
        command_lines = [
            line.strip()
            for line in script.splitlines()
            if line.strip().startswith("docker compose ")
        ]
        build_lines = [line for line in command_lines if " compose build" in line]
        up_lines = [line for line in command_lines if " compose up" in line]
        self.assertTrue(build_lines)
        self.assertTrue(up_lines)
        for line in build_lines:
            self.assertIn("--pull=false", line, line)
        for line in up_lines:
            self.assertIn("--pull never", line, line)

    def test_every_formal_operator_up_path_refuses_registry_pull(self) -> None:
        for script_path in (PROD_DEPLOY, INTRANET_DEPLOY, OPS_SCRIPT):
            script = script_path.read_text(encoding="utf-8")
            commands = [
                line.strip()
                for line in script.splitlines()
                if "docker compose up " in line and not line.lstrip().startswith("#")
            ]
            self.assertTrue(commands, script_path)
            for command in commands:
                self.assertIn(
                    "--pull never",
                    command,
                    f"{script_path}: {command}",
                )

    def test_compose_mounts_match_the_paths_prepared_for_uid_10001(self) -> None:
        compose = PLATFORM_COMPOSE.read_text(encoding="utf-8")
        self.assertIn(
            "../../share/uploads/ingestion:/var/anila/ingestion-uploads", compose
        )
        self.assertIn("${ANILA_SECRETS_DIR:?", compose)
        self.assertNotIn("../../secrets:/app/secrets:ro", compose)
        self.assertNotIn("/app/logs:", compose)

    def test_attachment_storage_is_non_root_writable_and_durable(self) -> None:
        dockerfile = PRODUCTION_DOCKERFILE.read_text(encoding="utf-8")
        self.assertIn("chown -R csp:csp /var/anila", dockerfile)
        self.assertIn(
            "chmod 700 /app/secrets /var/anila/attachments "
            "/var/anila/ingestion-uploads",
            dockerfile,
        )
        self.assertIn("/var/anila/attachments/.volume-init", dockerfile)

        platform = PLATFORM_COMPOSE.read_text(encoding="utf-8")
        self.assertIn("ATTACHMENT_STORAGE_PATH: /var/anila/attachments", platform)
        self.assertIn("csp-attachments:/var/anila/attachments", platform)
        self.assertRegex(platform, r"(?m)^  csp-attachments:\s*$")

        dev = DEV_COMPOSE.read_text(encoding="utf-8")
        self.assertIn("ATTACHMENT_STORAGE_PATH: /var/anila/attachments", dev)
        self.assertIn("csp-attachments-dev:/var/anila/attachments", dev)
        self.assertRegex(dev, r"(?m)^  csp-attachments-dev:\s*$")
        self.assertEqual(
            dev.count("ingestion-uploads-dev:/var/anila/ingestion-uploads"),
            2,
        )
        self.assertRegex(dev, r"(?m)^  ingestion-uploads-dev:\s*$")
        self.assertNotIn(
            "../../share-dev/uploads/ingestion:/var/anila/ingestion-uploads",
            dev,
        )

    def test_offline_csp_tag_matches_inventory_and_permission_helpers(self) -> None:
        inventory = PLATFORM_INVENTORY.read_text(encoding="utf-8")
        csp_rows = [
            line.split("\t")
            for line in inventory.splitlines()
            if line and not line.startswith("#") and line.split("\t")[0] == "csp"
        ]
        self.assertEqual(len(csp_rows), 1)
        self.assertEqual(csp_rows[0][1], "anila-platform-csp")
        for script_path in (INTRANET_DEPLOY, PROD_DEPLOY):
            script = script_path.read_text(encoding="utf-8")
            self.assertIn("anila-platform-csp:latest", script)

    def test_backup_reads_mode_0700_runtime_data_through_a_bounded_helper(self) -> None:
        script = OPS_SCRIPT.read_text(encoding="utf-8")
        self.assertIn(
            "docker compose ps --status running -q csp-db",
            script,
        )
        self.assertIn('CSP_RUNTIME_IMAGE="anila-platform-csp:latest"', script)
        self.assertIn("safe-runtime-backup.py", script)
        self.assertIn(
            "docker run --rm --pull never --user 0:0 --network none --read-only",
            script,
        )
        self.assertIn("--security-opt no-new-privileges", script)
        self.assertIn(
            'source_mount="type=bind,source=$source,target=/source,readonly"',
            script,
        )
        self.assertIn(
            'source_mount="type=volume,source=$source,target=/source,readonly"',
            script,
        )
        self.assertIn(
            'run_runtime_backup_helper bind "$SECRETS_DIR" secrets "$dest"',
            script,
        )
        self.assertIn(
            'run_runtime_backup_helper bind "$TLS_CERTS_DIR" tls "$dest"',
            script,
        )
        self.assertIn("ANILA_SECRETS_DIR", script)
        self.assertNotIn("cp secrets/*.pem", script)

    def test_full_backup_includes_uploads_and_labeled_attachment_volume(self) -> None:
        script = OPS_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("com.docker.compose.project=anila-platform", script)
        self.assertIn("com.docker.compose.volume=csp-attachments", script)
        self.assertIn(
            'run_runtime_backup_helper bind "$REPO_ROOT/share/uploads" tree '
            '"$dest" uploads',
            script,
        )
        self.assertIn(
            'run_runtime_backup_helper volume "$ATTACHMENT_VOLUME" tree '
            '"$dest" attachments',
            script,
        )
        self.assertIn("uploads.tar.gz + attachments.tar.gz", script)
        self.assertIn("禁止直接 cp/tar", script)

    def test_safe_backup_helper_rejects_secret_links_and_never_follows_tree_links(self) -> None:
        helper = SAFE_BACKUP_HELPER.read_text(encoding="utf-8")
        self.assertIn('JWT_FILES = ("jwt-private.pem", "jwt-public.pem")', helper)
        self.assertIn('TLS_FILES = ("server.key", "server.crt")', helper)
        self.assertIn('"uploads": "uploads.tar.gz"', helper)
        self.assertIn('"attachments": "attachments.tar.gz"', helper)
        self.assertIn('getattr(os, "O_NOFOLLOW", None)', helper)
        self.assertIn("stat.S_ISREG", helper)
        self.assertIn("dereference=False", helper)
        self.assertIn("os.fchown", helper)
        self.assertNotIn("follow_symlinks=True", helper)

    @unittest.skipUnless(os.name == "posix", "runtime helper targets Linux deployment")
    def test_safe_backup_helper_behavior_on_linux(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source"
            destination = root / "destination"
            source.mkdir()
            destination.mkdir()
            (source / "jwt-private.pem").write_text("private", encoding="utf-8")
            (source / "jwt-public.pem").write_text("public", encoding="utf-8")

            subprocess.run(
                [
                    sys.executable,
                    str(SAFE_BACKUP_HELPER),
                    "secrets",
                    "--source",
                    str(source),
                    "--destination",
                    str(destination),
                    "--uid",
                    str(os.getuid()),
                    "--gid",
                    str(os.getgid()),
                ],
                check=True,
            )
            self.assertEqual(
                (destination / "secrets" / "jwt-private.pem").read_text(
                    encoding="utf-8"
                ),
                "private",
            )

            linked_source = root / "linked-source"
            linked_destination = root / "linked-destination"
            linked_source.mkdir()
            linked_destination.mkdir()
            outside_secret = root / "outside-jwt-private.pem"
            outside_secret.write_text("must-not-be-copied", encoding="utf-8")
            (linked_source / "jwt-private.pem").symlink_to(outside_secret)
            (linked_source / "jwt-public.pem").write_text("public", encoding="utf-8")
            refused = subprocess.run(
                [
                    sys.executable,
                    str(SAFE_BACKUP_HELPER),
                    "secrets",
                    "--source",
                    str(linked_source),
                    "--destination",
                    str(linked_destination),
                    "--uid",
                    str(os.getuid()),
                    "--gid",
                    str(os.getgid()),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("regular file", refused.stderr)
            self.assertFalse(
                (linked_destination / "secrets" / "jwt-private.pem").exists()
            )

            tree_source = root / "tree-source"
            tree_destination = root / "tree-destination"
            tree_source.mkdir()
            tree_destination.mkdir()
            outside = root / "outside-secret"
            outside.write_text("must-not-be-read", encoding="utf-8")
            (tree_source / "link").symlink_to(outside)
            subprocess.run(
                [
                    sys.executable,
                    str(SAFE_BACKUP_HELPER),
                    "tree",
                    "--kind",
                    "uploads",
                    "--source",
                    str(tree_source),
                    "--destination",
                    str(tree_destination),
                    "--uid",
                    str(os.getuid()),
                    "--gid",
                    str(os.getgid()),
                ],
                check=True,
            )
            with tarfile.open(tree_destination / "uploads.tar.gz", "r:gz") as archive:
                member = archive.getmember("uploads/link")
                self.assertTrue(member.issym())


if __name__ == "__main__":
    unittest.main()
