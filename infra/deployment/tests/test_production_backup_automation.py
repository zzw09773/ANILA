from __future__ import annotations

import hashlib
import io
import json
import os
import secrets
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "infra" / "deployment" / "backup"))

import production_backup as backup  # noqa: E402


PROFILE = ROOT / "infra/deployment/backup/production-backup-profile.v1.json"


def _tar_bytes(name: str, content: bytes, *, kind: str = "file") -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        member = tarfile.TarInfo(name)
        if kind == "symlink":
            member.type = tarfile.SYMTYPE
            member.linkname = "target"
            archive.addfile(member)
        else:
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
    return output.getvalue()


def _component(name: str = "db.age") -> dict:
    return {
        "file": name,
        "format": "postgres-custom-v1",
        "surfaces": ["csp-postgresql"],
        "encrypted_sha256": "a" * 64,
        "encrypted_size": 10,
        "logical_sha256": "b" * 64,
        "logical_size": 20,
    }


def _envelope(profile_hash: str) -> dict:
    return {
        "schema_version": backup.ENVELOPE_SCHEMA,
        "backup_id": "backup-20260714T010203Z-deadbeef",
        "created_at": "2026-07-14T01:02:03Z",
        "profile_id": "anila-platform-production",
        "profile_sha256": profile_hash,
        "manifest": {
            "file": "manifest.age",
            "encrypted_sha256": "c" * 64,
            "encrypted_size": 30,
            "logical_sha256": "d" * 64,
            "logical_size": 40,
        },
        "components": [
            {
                "file": "db.age",
                "encrypted_sha256": "a" * 64,
                "encrypted_size": 10,
            }
        ],
    }


def _manifest(profile_hash: str) -> dict:
    return {
        "schema_version": backup.MANIFEST_SCHEMA,
        "backup_id": "backup-20260714T010203Z-deadbeef",
        "created_at": "2026-07-14T01:02:03Z",
        "profile_id": "anila-platform-production",
        "profile_sha256": profile_hash,
        "consistency": {
            "mode": "quiesced-writers-v1",
            "started_at": "2026-07-14T01:00:00Z",
            "finished_at": "2026-07-14T01:02:00Z",
            "quiesced_services": ["csp"],
        },
        "source_release": {"git_commit": "1" * 40, "git_branch": "prod"},
        "compose_images": {"csp-db": "pgvector@sha256:" + "2" * 64},
        "components": [_component()],
        "surfaces": [
            {
                "id": "csp-postgresql",
                "disposition": "required",
                "backup_method": "pg_dump",
                "restore_order": 10,
                "component": "db.age",
            }
        ],
        "external_references": [],
    }


class ProductionBackupAutomationTests(unittest.TestCase):
    @mock.patch.object(
        backup.subprocess,
        "run",
        side_effect=FileNotFoundError("missing backup dependency"),
    )
    def test_runner_wraps_command_start_oserror(self, run: mock.Mock) -> None:
        with self.assertRaises(backup.BackupAutomationError) as caught:
            backup.Runner().run(["missing-tool", "--version"], cwd=ROOT)

        self.assertIn("command failed to start (missing-tool)", str(caught.exception))
        self.assertIsInstance(caught.exception.__cause__, FileNotFoundError)
        run.assert_called_once()

    def _run_minimal_backup(
        self,
        root: Path,
        *,
        restart_error: Exception | None = None,
    ) -> tuple[Path, list[str], BaseException | None]:
        backup_root = root / "local"
        off_host = root / "offhost"
        off_host.mkdir()
        signing_key = root / "signing.pem"
        signing_key.write_text("test-only", encoding="utf-8")
        events: list[str] = []

        class OrchestrationRunner(backup.Runner):
            def run(self, argv, *, cwd, input_bytes=None, check=True):  # type: ignore[override]
                if argv[0] == "git" and "HEAD" in argv:
                    stdout = b"1" * 40 + b"\n"
                elif argv[0] == "git":
                    stdout = b"prod-intranet-card\n"
                elif argv[0] == "openssl":
                    Path(argv[argv.index("-out") + 1]).write_bytes(b"signature")
                    stdout = b""
                else:
                    raise AssertionError(f"unexpected command: {argv}")
                return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr=b"")

        automation = backup.ProductionBackup(ROOT, PROFILE, runner=OrchestrationRunner())

        def fake_stream(**kwargs):
            destination = kwargs["destination"]
            destination.write_bytes(b"encrypted")
            return backup.Component(
                file=destination.name,
                format=kwargs["logical_format"],
                surfaces=tuple(sorted(kwargs["surfaces"])),
                encrypted_sha256=hashlib.sha256(b"encrypted").hexdigest(),
                encrypted_size=len(b"encrypted"),
                logical_sha256=hashlib.sha256(b"logical").hexdigest(),
                logical_size=len(b"logical"),
            )

        def restart(_services):
            events.append("restart")
            if restart_error is not None:
                raise restart_error

        def alert(status, _detail, _backup_id):
            events.append(f"alert:{status}")

        compose = {
            "services": {
                "csp": {"image": "csp@sha256:" + "a" * 64},
                "csp-db": {"image": "pgvector@sha256:" + "b" * 64},
            }
        }
        caught: BaseException | None = None
        with ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ, {
                "ANILA_BACKUP_OFFHOST_DIR": str(off_host),
                "ANILA_BACKUP_SIGNING_KEY_FILE": str(signing_key),
            }, clear=False))
            stack.enter_context(mock.patch.object(automation, "verify_profile"))
            stack.enter_context(mock.patch.object(backup.shutil, "which", return_value="tool"))
            stack.enter_context(mock.patch.object(backup, "_assert_off_host_mount"))
            stack.enter_context(mock.patch.object(automation, "compose_config", return_value=compose))
            stack.enter_context(mock.patch.object(automation, "_external_references", return_value=[]))
            stack.enter_context(mock.patch.object(automation, "_running_services", return_value={"csp"}))
            stack.enter_context(mock.patch.object(automation, "_compose"))
            stack.enter_context(mock.patch.object(automation, "_resolved_source", return_value=("bind", str(root))))
            stack.enter_context(mock.patch.object(automation, "_archive_command", return_value=["unused"]))
            stack.enter_context(mock.patch.object(automation, "_stream_to_age", side_effect=fake_stream))
            stack.enter_context(mock.patch.object(automation, "_restart_and_verify_writers", side_effect=restart))
            stack.enter_context(mock.patch.object(automation, "_emit_alert", side_effect=alert))
            try:
                result = automation.backup(backup_root)
            except BaseException as exc:
                caught = exc
                result = backup_root
        return result, events, caught

    def test_safe_extract_accepts_regular_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp)
            raw = _tar_bytes("nested/blob.bin", b"classified")
            digest, size = backup._safe_extract_stream(io.BytesIO(raw), target)
            self.assertEqual(
                (target / "nested/blob.bin").read_bytes(), b"classified"
            )
            self.assertEqual(digest, hashlib.sha256(raw).hexdigest())
            self.assertEqual(size, len(raw))

    def test_safe_extract_rejects_escape_and_links(self) -> None:
        for name, kind in (
            ("../escape", "file"),
            ("/absolute", "file"),
            ("link", "symlink"),
        ):
            with self.subTest(name=name, kind=kind), tempfile.TemporaryDirectory() as temp:
                with self.assertRaisesRegex(
                    backup.BackupAutomationError, "unsafe archive"
                ):
                    backup._safe_extract_stream(
                        io.BytesIO(_tar_bytes(name, b"x", kind=kind)), Path(temp)
                    )

    def test_manifest_validation_accepts_exact_signed_inventory(self) -> None:
        profile_hash = "f" * 64
        backup.validate_manifest(
            _manifest(profile_hash), _envelope(profile_hash), profile_hash
        )

    def test_manifest_validation_fails_closed_on_mutation(self) -> None:
        for mutation in ("checksum", "extra", "missing"):
            with self.subTest(mutation=mutation):
                profile_hash = "f" * 64
                manifest = _manifest(profile_hash)
                if mutation == "checksum":
                    manifest["components"][0]["logical_sha256"] = "0" * 64
                    manifest["components"][0]["encrypted_sha256"] = "0" * 64
                elif mutation == "extra":
                    manifest["unexpected"] = True
                else:
                    manifest["components"] = []
                with self.assertRaises(backup.BackupAutomationError):
                    backup.validate_manifest(
                        manifest, _envelope(profile_hash), profile_hash
                    )

    def test_disposable_restore_target_rejects_existing_and_repo_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            existing = Path(temp) / "existing"
            existing.mkdir()
            with self.assertRaisesRegex(
                backup.BackupAutomationError, "must not already exist"
            ):
                backup._assert_new_disposable_target(existing, ROOT)
        with self.assertRaisesRegex(backup.BackupAutomationError, "outside repo"):
            backup._assert_new_disposable_target(ROOT / "restore", ROOT)

    def test_disposable_restore_target_rejects_backup_bundle_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            bundle = root / "backup-20260714T000000Z-deadbeef"
            bundle.mkdir()
            with self.assertRaisesRegex(
                backup.BackupAutomationError, "overlaps backup bundle"
            ):
                backup._assert_new_disposable_target(
                    bundle / "plaintext",
                    repo,
                    forbidden_roots=(bundle,),
                )

    def test_single_writer_lock_rejects_second_writer(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            lock_path = Path(temp) / ".lock"
            with backup.SingleWriterLock(lock_path):
                with self.assertRaisesRegex(
                    backup.BackupAutomationError, "already exists"
                ):
                    with backup.SingleWriterLock(lock_path):
                        pass
            self.assertFalse(lock_path.exists())

    def test_happy_path_recovers_writers_before_success_alert_and_publish(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            result, events, caught = self._run_minimal_backup(Path(temp))
            self.assertIsNone(caught)
            self.assertTrue(result.is_dir())
            self.assertEqual(events, ["restart", "alert:success"])

    def test_restart_failure_fails_job_and_reports_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            result, events, caught = self._run_minimal_backup(
                Path(temp), restart_error=backup.BackupAutomationError("restart refused")
            )
            self.assertIsInstance(caught, backup.BackupAutomationError)
            self.assertFalse(
                any(path.name.startswith("backup-") for path in result.iterdir())
            )
            self.assertEqual(events, ["restart", "restart", "alert:failure"])
            self.assertTrue(
                any("writer recovery also failed" in note for note in caught.__notes__)
            )

    def test_preflight_failure_uses_failure_alert_path(self) -> None:
        automation = backup.ProductionBackup(ROOT, PROFILE)
        with mock.patch.object(
            automation, "_backup_impl", side_effect=backup.BackupAutomationError("profile drift")
        ), mock.patch.object(automation, "_emit_alert") as emit:
            with self.assertRaisesRegex(backup.BackupAutomationError, "profile drift"):
                automation.backup(Path("unused"))
        emit.assert_called_once()
        self.assertEqual(emit.call_args.args[0], "failure")

    def test_offhost_same_filesystem_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            local = root / "local"
            offhost = root / "offhost"
            local.mkdir()
            offhost.mkdir()
            with self.assertRaisesRegex(
                backup.BackupAutomationError, "distinct device|remote filesystem"
            ):
                backup._assert_off_host_mount(
                    offhost,
                    local,
                    platform_name="posix",
                    is_mount=lambda _path: True,
                    filesystem_type=lambda _path: "ext4",
                    device_id=lambda _path: 7,
                )

    def test_active_generation_restore_metrics_fail_closed(self) -> None:
        self.assertEqual(
            backup._validate_restore_db_metrics(["0", "0", "4", "0", "0"])[
                "indexed_documents"
            ],
            4,
        )
        for values in (
            ["0", "1", "4", "0", "0"],
            ["0", "0", "4", "1", "0"],
            ["0", "0", "4", "0", "1"],
        ):
            with self.subTest(values=values), self.assertRaisesRegex(
                backup.BackupAutomationError, "active vector generation"
            ):
                backup._validate_restore_db_metrics(values)

    def test_profile_external_references_never_read_private_key_paths(self) -> None:
        profile = json.loads(PROFILE.read_text(encoding="utf-8"))
        environment: dict[str, str] = {}
        for name, contract in profile["automation"]["external_references"].items():
            environment[contract["reference_env"]] = f"kms://anila/{name}"
            environment[contract["fingerprint_env"]] = (
                "sha256:" + hashlib.sha256(name.encode()).hexdigest()
            )
        with mock.patch.dict(os.environ, environment, clear=False):
            automation = backup.ProductionBackup(ROOT, PROFILE)
            references = automation._external_references()
        self.assertEqual(
            {row["id"] for row in references},
            set(profile["automation"]["external_references"]),
        )
        self.assertTrue(
            all(
                set(row) == {"id", "reference", "fingerprint"}
                for row in references
            )
        )

    def test_duplicate_json_keys_are_rejected(self) -> None:
        with self.assertRaisesRegex(
            backup.BackupAutomationError, "duplicate JSON key"
        ):
            backup.strict_json_bytes(
                b'{"schema":"one","schema":"two"}', label="test"
            )

    def test_anila_ops_late_wrapper_disables_plaintext_and_in_place_paths(self) -> None:
        script = (ROOT / "infra/deployment/scripts/anila-ops.sh").read_text(
            encoding="utf-8"
        )
        late = script.index("PRODUCTION_BACKUP_TOOL=")
        dispatch = script.index('SUBCMD="${1:-help}"')
        self.assertLess(late, dispatch)
        self.assertEqual(script.count("cmd_backup() {"), 1)
        self.assertEqual(script.count("cmd_restore() {"), 1)
        wrapper = script[late:dispatch]
        self.assertIn("production-backup.py", wrapper)
        self.assertIn("restore-prepare", wrapper)
        self.assertIn("restore-smoke", wrapper)
        self.assertNotIn("pg_dump", wrapper)
        self.assertNotIn("ALTER DATABASE", wrapper)
        for legacy_signature in (
            "env.bak",
            "db-csp.sql.gz",
            "cp .env",
            "ALTER DATABASE csp RENAME",
            "gunzip -c",
            "RESTORE 確認",
        ):
            with self.subTest(legacy_signature=legacy_signature):
                self.assertNotIn(legacy_signature, script)

    @unittest.skipUnless(
        os.environ.get("ANILA_RUN_DOCKER_RESTORE_SMOKE") == "1",
        "set ANILA_RUN_DOCKER_RESTORE_SMOKE=1 for disposable Docker smoke",
    )
    def test_disposable_docker_restore_smoke(self) -> None:
        image = os.environ.get(
            "ANILA_TEST_PGVECTOR_IMAGE", "pgvector/pgvector:pg16"
        )
        source = "anila-backup-test-source-" + secrets.token_hex(4)

        def run(argv: list[str], *, input_bytes: bytes | None = None) -> bytes:
            result = subprocess.run(
                argv,
                input=input_bytes,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            if result.returncode != 0:
                self.fail(result.stderr.decode("utf-8", errors="replace"))
            return result.stdout

        try:
            run([
                "docker", "run", "-d", "--rm", "--name", source,
                "--network", "none", "-e", "POSTGRES_PASSWORD=test-only", image,
            ])
            for _ in range(60):
                ready = subprocess.run(
                    ["docker", "exec", source, "pg_isready", "-U", "postgres"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
                if ready.returncode == 0:
                    break
                time.sleep(1)
            else:
                self.fail("source PostgreSQL did not become ready")

            schema = b"""
CREATE EXTENSION vector;
CREATE TABLE ingestion_collections (
  id integer PRIMARY KEY, embedding_model text NOT NULL,
  embedding_dim integer NOT NULL, embedding_fingerprint text NOT NULL
);
CREATE TABLE ingestion_documents (
  id integer PRIMARY KEY, collection_id integer NOT NULL,
  active_generation_id integer, storage_path text, status text NOT NULL
);
CREATE TABLE ingestion_document_generations (
  id integer PRIMARY KEY, document_id integer NOT NULL,
  collection_id integer NOT NULL, status text NOT NULL,
  embedding_model text NOT NULL,
  embedding_dim integer NOT NULL, embedding_fingerprint text NOT NULL,
  chunk_count integer NOT NULL
);
CREATE TABLE document_chunks (
  id integer PRIMARY KEY, collection_id integer NOT NULL,
  document_id integer NOT NULL,
  generation_id integer NOT NULL, is_active_generation boolean NOT NULL,
  chunk_type text NOT NULL, embedding vector(3)
);
CREATE TABLE attachments (storage_path text NOT NULL);
CREATE TABLE artifact_versions (blob_key text);
INSERT INTO ingestion_collections VALUES
  (20, 'test-model', 3, 'sha256:' || repeat('a', 64));
INSERT INTO ingestion_document_generations VALUES
  (10, 1, 20, 'active', 'test-model', 3, 'sha256:' || repeat('a', 64), 1);
INSERT INTO ingestion_documents VALUES
  (1, 20, 10, '/var/anila/ingestion-uploads/doc.bin', 'indexed');
INSERT INTO document_chunks VALUES (1, 20, 1, 10, true, 'leaf', '[1,2,3]');
INSERT INTO attachments VALUES ('conversation/a.bin');
INSERT INTO artifact_versions VALUES ('ab/artifact.pdf');
"""
            run(
                ["docker", "exec", "-i", source, "psql", "-U", "postgres"],
                input_bytes=schema,
            )
            with tempfile.TemporaryDirectory() as temp:
                prepared = Path(temp) / "prepared"
                prepared.mkdir()
                dump = prepared / "db-csp.dump"
                dump.write_bytes(
                    run([
                        "docker", "exec", source, "pg_dump", "-U", "postgres",
                        "-d", "postgres", "-Fc",
                    ])
                )
                for surface_id, relative in (
                    ("ingestion-uploads", "doc.bin"),
                    ("csp-conversation-attachments", "conversation/a.bin"),
                    ("csp-artifact-blobs", "ab/artifact.pdf"),
                ):
                    output = prepared / "surfaces" / surface_id / relative
                    output.parent.mkdir(parents=True, exist_ok=True)
                    output.write_bytes(b"test")
                (prepared / "manifest.json").write_bytes(
                    backup._canonical_json({"compose_images": {"csp-db": image}})
                )
                (prepared / "RESTORE_PREPARED.json").write_bytes(
                    backup._canonical_json({"schema_version": backup.PREPARED_SCHEMA})
                )
                profile = json.loads(PROFILE.read_text(encoding="utf-8"))
                report = backup.restore_smoke(prepared, profile)
                self.assertEqual(report["status"], "pass")
                self.assertEqual(report["checks"]["indexed_documents"], 1)
                self.assertEqual(
                    report["checks"]["active_generation_contract_violations"], 0
                )
                self.assertEqual(report["checks"]["artifact_blob_references"], 1)

                # Mutation sensitivity: a generation/collection contract
                # mismatch must make the disposable restore smoke fail.
                run([
                    "docker", "exec", source, "psql", "-U", "postgres",
                    "-c", "UPDATE ingestion_document_generations SET "
                    "embedding_fingerprint='sha256:' || repeat('b',64)",
                ])
                dump.write_bytes(
                    run([
                        "docker", "exec", source, "pg_dump", "-U", "postgres",
                        "-d", "postgres", "-Fc",
                    ])
                )
                with self.assertRaisesRegex(
                    backup.BackupAutomationError, "active vector generation"
                ):
                    backup.restore_smoke(prepared, profile)

                # Even when generation and collection metadata agree, the
                # physical leaf vector dimension must agree too.
                run([
                    "docker", "exec", source, "psql", "-U", "postgres",
                    "-c", "UPDATE ingestion_document_generations SET "
                    "embedding_fingerprint='sha256:' || repeat('a',64), embedding_dim=2; "
                    "UPDATE ingestion_collections SET embedding_dim=2",
                ])
                dump.write_bytes(
                    run([
                        "docker", "exec", source, "pg_dump", "-U", "postgres",
                        "-d", "postgres", "-Fc",
                    ])
                )
                with self.assertRaisesRegex(
                    backup.BackupAutomationError, "active vector generation"
                ):
                    backup.restore_smoke(prepared, profile)
        finally:
            subprocess.run(
                ["docker", "rm", "-f", source],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )


if __name__ == "__main__":
    unittest.main()
