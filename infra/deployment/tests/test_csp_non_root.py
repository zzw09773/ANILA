"""Gate 0 contract for the production CSP container runtime user."""

from __future__ import annotations

import os
import json
import re
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
PRODUCTION_DOCKERFILE = REPO_ROOT / "infra" / "docker" / "csp.Dockerfile"
INGESTION_WORKER_DOCKERFILE = REPO_ROOT / "services" / "ingestion-worker" / "Dockerfile"
PPTX_RENDERER_DOCKERFILE = REPO_ROOT / "services" / "pptx-renderer" / "Dockerfile"
FLUX_AGENT_DOCKERFILE = REPO_ROOT / "services" / "flux2-dev-agent" / "Dockerfile"
CSP_REQUIREMENTS = REPO_ROOT / "services" / "csp" / "requirements.txt"
DEAD_DOCKERFILE = REPO_ROOT / "services" / "csp" / "Dockerfile"
INTRANET_DEPLOY = (
    REPO_ROOT / "infra" / "deployment" / "intranet" / "intranet-deploy.sh"
)
PROD_DEPLOY = REPO_ROOT / "infra" / "deployment" / "scripts" / "deploy-prod.sh"
OPS_SCRIPT = REPO_ROOT / "infra" / "deployment" / "scripts" / "anila-ops.sh"
PRODUCTION_BACKUP = (
    REPO_ROOT / "infra" / "deployment" / "backup" / "production_backup.py"
)
PRODUCTION_BACKUP_PROFILE = (
    REPO_ROOT
    / "infra"
    / "deployment"
    / "backup"
    / "production-backup-profile.v1.json"
)
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
    def test_internal_packages_never_resolve_from_public_index(self) -> None:
        requirements = CSP_REQUIREMENTS.read_text(encoding="utf-8")
        for distribution in ("anila-contracts", "anila-security", "anila-core"):
            self.assertNotRegex(
                requirements,
                rf"(?m)^\s*{re.escape(distribution)}(?:\s|[<>=!~@])",
                f"{distribution} must be installed from a reviewed local path",
            )

        dockerfile = PRODUCTION_DOCKERFILE.read_text(encoding="utf-8")
        requirements_install = dockerfile.index(
            "RUN pip install --no-cache-dir -r requirements.txt"
        )
        for package in ("anila-contracts", "anila-security", "anila-core"):
            copy_index = dockerfile.index(f"COPY packages/{package} /tmp/{package}")
            local_install = dockerfile.index("RUN pip install", copy_index)
            self.assertLess(copy_index, local_install)
            self.assertLess(local_install, requirements_install)
            self.assertIn(f"'file:///tmp/{package}'", dockerfile)
        self.assertIn("direct_url.json", dockerfile)

    def test_production_image_has_explicit_non_root_user(self) -> None:
        dockerfile = PRODUCTION_DOCKERFILE.read_text(encoding="utf-8")
        self.assertRegex(dockerfile, r"(?m)^USER\s+csp(?::csp)?\s*$")
        self.assertIn("useradd", dockerfile)
        self.assertIn("--uid 10001", dockerfile)
        self.assertIn("--gid 10001", dockerfile)
        self.assertIn("/app/logs /app/secrets", dockerfile)
        self.assertIn("chown -R csp:csp /app/logs /app/secrets", dockerfile)
        self.assertIn("/var/anila/attachments", dockerfile)
        self.assertNotRegex(dockerfile, r"(?m)^USER\s+(?:root|0)(?::0)?\s*$")

    def test_ingestion_worker_uses_shared_non_root_runtime_identity(self) -> None:
        dockerfile = INGESTION_WORKER_DOCKERFILE.read_text(encoding="utf-8")
        self.assertRegex(dockerfile, r"(?m)^USER\s+ingestion(?::ingestion)?\s*$")
        self.assertIn("groupadd --gid 10001 ingestion", dockerfile)
        self.assertIn("useradd --create-home --uid 10001 --gid 10001 ingestion", dockerfile)
        self.assertIn("chown -R ingestion:ingestion /var/anila/ingestion-uploads", dockerfile)
        self.assertNotRegex(dockerfile, r"(?m)^USER\s+(?:root|0)(?::0)?\s*$")

    def test_gate3_artifact_services_use_non_root_runtime_identity(self) -> None:
        for dockerfile_path, user, writable_path in (
            (PPTX_RENDERER_DOCKERFILE, "anila", "/var/anila/pptx-out"),
            (FLUX_AGENT_DOCKERFILE, "anila", "/share/flux"),
        ):
            dockerfile = dockerfile_path.read_text(encoding="utf-8")
            self.assertRegex(dockerfile, rf"(?m)^USER\s+{user}(?::{user})?\s*$")
            self.assertIn(f"chown -R {user}:{user}", dockerfile)
            self.assertIn(writable_path, dockerfile)
            self.assertIn("--uid 10001", dockerfile)
            self.assertIn("--gid 10001", dockerfile)
            self.assertNotRegex(
                dockerfile,
                r"(?m)^USER\s+(?:root|0)(?::0)?\s*$",
            )

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
        self.assertIn(
            'CSP_RUNTIME_IMAGE="${ANILA_IMAGE_CSP:-$(env_file_value ANILA_IMAGE_CSP)}"',
            script,
        )
        self.assertIn(
            'prepare_csp_runtime_mount "$ANILA_SECRETS_DIR" 700', script
        )
        self.assertIn(
            'prepare_csp_runtime_mount "$REPO_ROOT/share/uploads/ingestion" 700',
            script,
        )
        self.assertIn("docker run --rm --pull never", script)
        deploy_body = script[script.index("cmd_deploy() {") : script.index("# ── Subcommand: up")]
        self.assertNotIn("docker compose build", deploy_body)
        self.assertIn("preloaded content-ID-locked images", deploy_body)
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
            if line.strip().startswith(("docker compose ", "compose "))
        ]
        build_lines = [line for line in command_lines if "compose build" in line]
        up_lines = [line for line in command_lines if "compose up" in line]
        self.assertFalse(build_lines, "formal host must never build images")
        self.assertTrue(up_lines)
        for line in up_lines:
            self.assertIn("--pull never", line, line)

    def test_every_formal_operator_up_path_refuses_registry_pull(self) -> None:
        for script_path in (PROD_DEPLOY, OPS_SCRIPT):
            script = script_path.read_text(encoding="utf-8")
            commands = [
                line.strip()
                for line in script.splitlines()
                if "docker compose up " in line and not line.lstrip().startswith("#")
            ]
            self.assertTrue(commands, script_path)
            for command in commands:
                self.assertIn(
                    "--no-build",
                    command,
                    f"{script_path}: {command}",
                )
                self.assertIn(
                    "--pull never",
                    command,
                    f"{script_path}: {command}",
                )
        intranet = INTRANET_DEPLOY.read_text(encoding="utf-8")
        self.assertIn("bash infra/deployment/scripts/deploy-prod.sh up", intranet)
        self.assertNotIn("\ndocker compose up ", intranet)

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

    def test_source_snapshot_evidence_uses_external_restricted_state(self) -> None:
        dockerfile = PRODUCTION_DOCKERFILE.read_text(encoding="utf-8")
        self.assertIn("/var/lib/anila/source-snapshots/.volume-init", dockerfile)
        self.assertIn("chown -R csp:csp /var/anila /var/lib/anila", dockerfile)
        self.assertIn("/var/lib/anila/source-snapshots", dockerfile)

        platform = PLATFORM_COMPOSE.read_text(encoding="utf-8")
        self.assertIn(
            "SOURCE_SNAPSHOT_STORAGE_PATH: /var/lib/anila/source-snapshots",
            platform,
        )
        self.assertIn(
            "${ANILA_STATE_DIR:?ANILA_STATE_DIR must be an absolute path "
            "outside the repo}/source-snapshots:/var/lib/anila/source-snapshots",
            platform,
        )
        self.assertNotIn("share/source-snapshots", platform)

        dev = DEV_COMPOSE.read_text(encoding="utf-8")
        self.assertIn(
            "csp-source-snapshots-dev:/var/lib/anila/source-snapshots", dev
        )
        self.assertRegex(dev, r"(?m)^  csp-source-snapshots-dev:\s*$")

        for deploy_script in (PROD_DEPLOY, INTRANET_DEPLOY):
            script = deploy_script.read_text(encoding="utf-8")
            self.assertIn(
                'prepare_csp_runtime_mount "$ANILA_STATE_DIR/source-snapshots" 700',
                script,
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
            self.assertIn("ANILA_IMAGE_CSP", script)

    def test_backup_reads_mode_0700_runtime_data_through_a_bounded_helper(self) -> None:
        tool = PRODUCTION_BACKUP.read_text(encoding="utf-8")
        self.assertIn('"--network",\n            "none"', tool)
        self.assertIn('"--read-only"', tool)
        self.assertIn('"no-new-privileges"', tool)
        self.assertIn('target=/source,readonly', tool)
        self.assertIn('["age", "-R", str(recipients), "-o", str(tmp)]', tool)
        self.assertIn('"pg_dump", "-U", "csp", "-d", "csp", "-Fc"', tool)
        self.assertNotIn("jwt-private.pem", tool)
        self.assertNotIn("server.key", tool)

    def test_full_backup_includes_uploads_and_labeled_attachment_volume(self) -> None:
        profile = json.loads(PRODUCTION_BACKUP_PROFILE.read_text(encoding="utf-8"))
        required = {
            surface["id"]: surface
            for surface in profile["surfaces"]
            if surface["disposition"] == "required"
        }
        for surface_id in (
            "ingestion-uploads",
            "shared-upload-tree",
            "csp-conversation-attachments",
            "sealed-source-snapshots",
            "studio-artifacts",
            "csp-artifact-blobs",
        ):
            self.assertIn(surface_id, required)
        self.assertEqual(
            required["csp-conversation-attachments"]["source"]["name"],
            "csp-attachments",
        )
        self.assertEqual(
            required["csp-conversation-attachments"]["backup_method"],
            "volume_archive",
        )

    def test_safe_backup_helper_rejects_secret_links_and_never_follows_tree_links(self) -> None:
        helper = SAFE_BACKUP_HELPER.read_text(encoding="utf-8")
        self.assertIn('JWT_FILES = ("jwt-private.pem", "jwt-public.pem")', helper)
        self.assertIn('TLS_FILES = ("server.key", "server.crt")', helper)
        self.assertIn('"uploads": "uploads.tar.gz"', helper)
        self.assertIn('"attachments": "attachments.tar.gz"', helper)
        self.assertIn('"source-snapshots": "source-snapshots.tar.gz"', helper)
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
