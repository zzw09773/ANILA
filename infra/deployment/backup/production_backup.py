#!/usr/bin/env python3
"""Fail-closed production backup and disposable restore preparation.

Every captured component is streamed directly into ``age``.  Plaintext
database dumps, service volumes, and filesystem archives never become files
in the publish staging directory.  The signed envelope binds encrypted bytes;
the encrypted manifest additionally binds each component's logical plaintext
hash and its profile surface mapping.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Iterable


ENVELOPE_SCHEMA = "anila.production-backup-envelope.v1"
MANIFEST_SCHEMA = "anila.production-backup-manifest.v1"
PREPARED_SCHEMA = "anila.production-restore-prepared.v1"
BACKUP_NAME_RE = re.compile(r"^backup-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")
FINGERPRINT_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
COMPONENT_RE = re.compile(r"^[a-z0-9][a-z0-9-]*\.age$")
_APPROVED_REMOTE_FILESYSTEMS = {
    "ceph",
    "cifs",
    "fuse.sshfs",
    "glusterfs",
    "nfs",
    "nfs4",
    "sshfs",
}

ENVELOPE_KEYS = {
    "schema_version",
    "backup_id",
    "created_at",
    "profile_id",
    "profile_sha256",
    "manifest",
    "components",
}
MANIFEST_KEYS = {
    "schema_version",
    "backup_id",
    "created_at",
    "profile_id",
    "profile_sha256",
    "consistency",
    "source_release",
    "compose_images",
    "components",
    "surfaces",
    "external_references",
}
COMPONENT_KEYS = {
    "file",
    "format",
    "surfaces",
    "encrypted_sha256",
    "encrypted_size",
    "logical_sha256",
    "logical_size",
}
MANIFEST_REF_KEYS = {
    "file",
    "encrypted_sha256",
    "encrypted_size",
    "logical_sha256",
    "logical_size",
}


class BackupAutomationError(RuntimeError):
    """A backup invariant failed; callers must exit non-zero."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _canonical_json(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BackupAutomationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def strict_json_bytes(raw: bytes, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupAutomationError(f"invalid {label} JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise BackupAutomationError(f"{label} root must be an object")
    return value


def _exact_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise BackupAutomationError(
            f"{label} keys invalid; missing={sorted(expected - set(value))}, "
            f"extra={sorted(set(value) - expected)}"
        )


def sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise BackupAutomationError(f"required environment variable is missing: {name}")
    return value


def _required_file_env(name: str) -> Path:
    raw_path = Path(_required_env(name)).expanduser()
    if raw_path.is_symlink():
        raise BackupAutomationError(f"{name} must name a regular non-symlink file")
    path = raw_path.resolve()
    if not path.is_file():
        raise BackupAutomationError(f"{name} must name a regular non-symlink file")
    return path


def _safe_directory(path: Path, *, label: str, create: bool = False) -> Path:
    path = path.expanduser()
    if path.is_symlink():
        raise BackupAutomationError(f"{label} must be a real directory: {path}")
    if create:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = path.resolve()
    if not path.is_dir():
        raise BackupAutomationError(f"{label} must be a real directory: {path}")
    return path


def _reject_ambient_compose_environment() -> None:
    names = sorted(name for name in os.environ if name.startswith("COMPOSE_"))
    if names:
        raise BackupAutomationError(
            "ambient Docker Compose variables are forbidden: " + ", ".join(names)
        )


def _assert_outside_repo(path: Path, repo_root: Path, *, label: str) -> None:
    path = path.resolve()
    repo_root = repo_root.resolve()
    if path == repo_root or repo_root in path.parents:
        raise BackupAutomationError(f"{label} must be outside the repository")


def _linux_mount_fstype(path: Path) -> str:
    """Return the filesystem type for an exact Linux mountpoint."""

    try:
        rows = Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise BackupAutomationError("cannot read Linux mount evidence") from exc
    expected = str(path.resolve())
    for row in rows:
        fields = row.split()
        try:
            separator = fields.index("-")
        except ValueError:
            continue
        if len(fields) <= 4 or len(fields) <= separator + 1:
            continue
        mountpoint = (
            fields[4]
            .replace("\\040", " ")
            .replace("\\011", "\t")
            .replace("\\012", "\n")
            .replace("\\134", "\\")
        )
        if str(Path(mountpoint).resolve()) == expected:
            return fields[separator + 1].lower()
    raise BackupAutomationError("off-host directory lacks Linux mount evidence")


def _assert_off_host_mount(
    off_host: Path,
    backup_root: Path,
    *,
    platform_name: str | None = None,
    is_mount: Any | None = None,
    filesystem_type: Any | None = None,
    device_id: Any | None = None,
) -> None:
    """Require evidence that the second copy is on independent storage.

    The Windows escape hatch exists only for local tests.  Formal deployment
    profiles can never use it.
    """

    platform_name = os.name if platform_name is None else platform_name
    if platform_name == "nt":
        profile = os.environ.get("ANILA_DEPLOYMENT_PROFILE", "").strip().lower()
        override = os.environ.get("ANILA_BACKUP_TEST_ALLOW_WINDOWS_OFFHOST", "")
        if profile.startswith("prod-") or override != "1":
            raise BackupAutomationError(
                "Windows off-host validation is test-only and forbidden for formal profiles"
            )
        print(
            "production backup TEST-ONLY: Windows off-host mount evidence bypassed",
            file=sys.stderr,
        )
        return

    if platform_name != "posix":
        raise BackupAutomationError("unsupported platform for off-host mount validation")
    mount_check = os.path.ismount if is_mount is None else is_mount
    if not mount_check(off_host):
        raise BackupAutomationError("off-host directory must be an exact mountpoint")
    fstype_reader = _linux_mount_fstype if filesystem_type is None else filesystem_type
    fstype = str(fstype_reader(off_host)).lower()
    id_reader = (lambda value: value.stat().st_dev) if device_id is None else device_id
    if (
        id_reader(off_host) == id_reader(backup_root)
        and fstype not in _APPROVED_REMOTE_FILESYSTEMS
    ):
        raise BackupAutomationError(
            "off-host mount must use a distinct device or approved remote filesystem"
        )


class Runner:
    def run(
        self,
        argv: list[str],
        *,
        cwd: Path,
        input_bytes: bytes | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[bytes]:
        try:
            result = subprocess.run(
                argv,
                cwd=cwd,
                input=input_bytes,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        except OSError as exc:
            raise BackupAutomationError(
                f"command failed to start ({argv[0]}): {exc}"
            ) from exc
        if check and result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="replace")[-2000:]
            raise BackupAutomationError(f"command failed ({argv[0]}): {detail}")
        return result


@dataclass(frozen=True)
class Component:
    file: str
    format: str
    surfaces: tuple[str, ...]
    encrypted_sha256: str
    encrypted_size: int
    logical_sha256: str
    logical_size: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "format": self.format,
            "surfaces": list(self.surfaces),
            "encrypted_sha256": self.encrypted_sha256,
            "encrypted_size": self.encrypted_size,
            "logical_sha256": self.logical_sha256,
            "logical_size": self.logical_size,
        }


class SingleWriterLock:
    def __init__(self, path: Path):
        self.path = path
        self.fd: int | None = None

    def __enter__(self) -> "SingleWriterLock":
        try:
            self.fd = os.open(
                self.path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except FileExistsError as exc:
            raise BackupAutomationError(
                f"backup writer lock already exists: {self.path}"
            ) from exc
        os.write(self.fd, f"pid={os.getpid()} started={_utc_now()}\n".encode())
        os.fsync(self.fd)
        return self

    def __exit__(self, *_exc: Any) -> None:
        if self.fd is not None:
            os.close(self.fd)
        self.path.unlink(missing_ok=True)


class _HashingReader(io.RawIOBase):
    def __init__(self, raw: BinaryIO):
        self.raw = raw
        self.digest = hashlib.sha256()
        self.size = 0

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        chunk = self.raw.read(size)
        if chunk:
            self.digest.update(chunk)
            self.size += len(chunk)
        return chunk


_TAR_STREAM_SCRIPT = r"""
import os, stat, sys, tarfile
root = '/source'
with tarfile.open(fileobj=sys.stdout.buffer, mode='w|gz', format=tarfile.PAX_FORMAT) as out:
    for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
        safe_dirs = []
        for name in sorted(dirs):
            path = os.path.join(current, name)
            mode = os.lstat(path).st_mode
            if stat.S_ISLNK(mode):
                raise SystemExit('symlink prohibited: ' + path)
            if not stat.S_ISDIR(mode):
                raise SystemExit('non-directory tree entry: ' + path)
            safe_dirs.append(name)
            out.add(path, arcname=os.path.relpath(path, root), recursive=False)
        dirs[:] = safe_dirs
        for name in sorted(files):
            path = os.path.join(current, name)
            mode = os.lstat(path).st_mode
            if not stat.S_ISREG(mode):
                raise SystemExit('non-regular file prohibited: ' + path)
            out.add(path, arcname=os.path.relpath(path, root), recursive=False)
"""


class ProductionBackup:
    def __init__(
        self,
        repo_root: Path,
        profile_path: Path,
        *,
        runner: Runner | None = None,
    ):
        self.repo_root = repo_root.resolve()
        self.profile_path = profile_path.resolve()
        self.runner = runner or Runner()
        self.profile = strict_json_bytes(
            self.profile_path.read_bytes(), label="backup profile"
        )
        self.automation = self.profile.get("automation")
        if not isinstance(self.automation, dict):
            raise BackupAutomationError("profile lacks executable automation policy")
        self.compose_path = self.repo_root / self.profile["scope"]["compose_file"]

    def verify_profile(self) -> None:
        verifier = self.repo_root / "infra/deployment/scripts/verify-production-backup-profile.py"
        result = self.runner.run(
            [
                sys.executable,
                str(verifier),
                "--profile",
                str(self.profile_path),
                "--compose",
                str(self.compose_path),
            ],
            cwd=self.repo_root,
        )
        if b"PASS" not in result.stdout:
            raise BackupAutomationError("profile verifier did not emit PASS")

    def compose_config(self) -> dict[str, Any]:
        result = self.runner.run(
            [
                "docker",
                "compose",
                "-f",
                str(self.compose_path),
                "config",
                "--format",
                "json",
            ],
            cwd=self.repo_root,
        )
        return strict_json_bytes(result.stdout, label="resolved Compose")

    def _resolved_source(
        self, surface: dict[str, Any], compose: dict[str, Any]
    ) -> tuple[str, str]:
        found: set[tuple[str, str]] = set()
        for expected in surface["source"]["mounts"]:
            service = compose.get("services", {}).get(expected["service"])
            if not isinstance(service, dict):
                raise BackupAutomationError(
                    f"resolved service missing for {surface['id']}: {expected['service']}"
                )
            for mount in service.get("volumes", []):
                if not isinstance(mount, dict) or mount.get("target") != expected["target"]:
                    continue
                kind = "named_volume" if mount.get("type") == "volume" else "bind"
                source = mount.get("source")
                if not isinstance(source, str) or not source:
                    raise BackupAutomationError(f"resolved source missing: {surface['id']}")
                found.add((kind, source))
        if len(found) != 1:
            raise BackupAutomationError(
                f"surface does not resolve to exactly one source: {surface['id']} -> {found}"
            )
        kind, source = next(iter(found))
        if kind == "named_volume":
            volume_config = compose.get("volumes", {}).get(source, {})
            explicit_name = (
                volume_config.get("name")
                if isinstance(volume_config, dict)
                else None
            )
            source = (
                explicit_name
                if isinstance(explicit_name, str) and explicit_name
                else f"{self.profile['scope']['compose_project']}_{source}"
            )
        return kind, source

    def _stream_to_age(
        self,
        *,
        destination: Path,
        surfaces: Iterable[str],
        logical_format: str,
        source_argv: list[str] | None = None,
        source_bytes: bytes | None = None,
    ) -> Component:
        if (source_argv is None) == (source_bytes is None):
            raise BackupAutomationError("component must have exactly one plaintext source")
        recipients_env = self.automation["encryption"]["recipients_file_env"]
        recipients = _required_file_env(recipients_env)
        tmp = destination.with_suffix(destination.suffix + ".tmp")
        tmp.unlink(missing_ok=True)
        logical_hash = hashlib.sha256()
        logical_size = 0
        with tempfile.TemporaryFile() as source_err, tempfile.TemporaryFile() as age_err:
            age = subprocess.Popen(
                ["age", "-R", str(recipients), "-o", str(tmp)],
                cwd=self.repo_root,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=age_err,
            )
            assert age.stdin is not None
            source: subprocess.Popen[bytes] | None = None
            try:
                if source_argv is not None:
                    source = subprocess.Popen(
                        source_argv,
                        cwd=self.repo_root,
                        stdout=subprocess.PIPE,
                        stderr=source_err,
                    )
                    assert source.stdout is not None
                    chunks = iter(lambda: source.stdout.read(1024 * 1024), b"")
                else:
                    assert source_bytes is not None
                    chunks = (source_bytes[index : index + 1024 * 1024]
                              for index in range(0, len(source_bytes), 1024 * 1024))
                for chunk in chunks:
                    logical_hash.update(chunk)
                    logical_size += len(chunk)
                    age.stdin.write(chunk)
                age.stdin.close()
                if source is not None and source.wait() != 0:
                    source_err.seek(0)
                    detail = source_err.read().decode("utf-8", errors="replace")[-2000:]
                    raise BackupAutomationError(f"capture command failed: {detail}")
                if age.wait() != 0:
                    age_err.seek(0)
                    detail = age_err.read().decode("utf-8", errors="replace")[-2000:]
                    raise BackupAutomationError(f"age encryption failed: {detail}")
            except BaseException:
                if source is not None and source.poll() is None:
                    source.kill()
                    source.wait()
                if age.poll() is None:
                    age.kill()
                    age.wait()
                tmp.unlink(missing_ok=True)
                raise
        if logical_size <= 0 or not tmp.is_file():
            tmp.unlink(missing_ok=True)
            raise BackupAutomationError("encrypted component is empty")
        os.replace(tmp, destination)
        encrypted_hash, encrypted_size = sha256_file(destination)
        return Component(
            file=destination.name,
            format=logical_format,
            surfaces=tuple(sorted(surfaces)),
            encrypted_sha256=encrypted_hash,
            encrypted_size=encrypted_size,
            logical_sha256=logical_hash.hexdigest(),
            logical_size=logical_size,
        )

    def _archive_command(
        self, *, kind: str, source: str, runtime_image: str
    ) -> list[str]:
        if kind == "bind":
            path = Path(source).resolve()
            if path.is_symlink() or not path.is_dir():
                raise BackupAutomationError(f"bind backup source is unsafe: {path}")
            mount = f"type=bind,source={path},target=/source,readonly"
        elif kind == "named_volume":
            mount = f"type=volume,source={source},target=/source,readonly"
        else:
            raise BackupAutomationError(f"unsupported archive source kind: {kind}")
        return [
            "docker",
            "run",
            "--rm",
            "--pull",
            "never",
            "--user",
            "0:0",
            "--network",
            "none",
            "--read-only",
            "--security-opt",
            "no-new-privileges",
            "--mount",
            mount,
            runtime_image,
            "python",
            "-c",
            _TAR_STREAM_SCRIPT,
        ]

    def _external_references(self) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        for name, contract in sorted(
            self.automation["external_references"].items()
        ):
            reference = _required_env(contract["reference_env"])
            fingerprint = _required_env(contract["fingerprint_env"]).lower()
            if len(reference) < 8 or any(char.isspace() for char in reference):
                raise BackupAutomationError(f"external reference is invalid: {name}")
            if not FINGERPRINT_RE.fullmatch(fingerprint):
                raise BackupAutomationError(
                    f"external fingerprint must be sha256:<64 lowercase hex>: {name}"
                )
            result.append(
                {"id": name, "reference": reference, "fingerprint": fingerprint}
            )
        return result

    def _running_services(self) -> set[str]:
        result = self.runner.run(
            [
                "docker", "compose", "-f", str(self.compose_path),
                "ps", "--status", "running", "--services",
            ],
            cwd=self.repo_root,
        )
        return {
            line.strip()
            for line in result.stdout.decode().splitlines()
            if line.strip()
        }

    def _compose(self, *args: str) -> None:
        self.runner.run(
            ["docker", "compose", "-f", str(self.compose_path), *args],
            cwd=self.repo_root,
        )

    def _service_states(self, services: list[str]) -> dict[str, tuple[str, str]]:
        result = self.runner.run(
            [
                "docker", "compose", "-f", str(self.compose_path),
                "ps", "--format", "json", *services,
            ],
            cwd=self.repo_root,
        )
        raw = result.stdout.decode("utf-8", errors="strict").strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
            rows = parsed if isinstance(parsed, list) else [parsed]
        except json.JSONDecodeError:
            try:
                rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
            except json.JSONDecodeError as exc:
                raise BackupAutomationError("cannot parse writer recovery state") from exc
        states: dict[str, tuple[str, str]] = {}
        for row in rows:
            if not isinstance(row, dict):
                raise BackupAutomationError("writer recovery state must be JSON objects")
            service = row.get("Service") or row.get("service")
            state = row.get("State") or row.get("state")
            health = row.get("Health") or row.get("health") or ""
            if not all(isinstance(value, str) for value in (service, state, health)):
                raise BackupAutomationError("writer recovery state fields are invalid")
            if service in states:
                raise BackupAutomationError(f"duplicate writer recovery state: {service}")
            states[service] = (state.lower(), health.lower())
        return states

    def _restart_and_verify_writers(self, services: list[str]) -> None:
        if not services:
            return
        self._compose("up", "-d", "--no-build", "--pull", "never", *services)
        raw_timeout = os.environ.get("ANILA_BACKUP_RESTART_TIMEOUT_SECONDS", "300")
        try:
            timeout = int(raw_timeout)
        except ValueError as exc:
            raise BackupAutomationError(
                "ANILA_BACKUP_RESTART_TIMEOUT_SECONDS must be an integer"
            ) from exc
        if not 1 <= timeout <= 1800:
            raise BackupAutomationError(
                "ANILA_BACKUP_RESTART_TIMEOUT_SECONDS must be 1..1800"
            )
        deadline = time.monotonic() + timeout
        last_detail = "no state returned"
        expected = set(services)
        while True:
            states = self._service_states(services)
            missing = sorted(expected - set(states))
            bad = {
                service: states[service]
                for service in sorted(expected & set(states))
                if states[service][0] != "running"
                or states[service][1] not in {"", "healthy"}
            }
            if not missing and not bad:
                return
            last_detail = f"missing={missing}, unhealthy={bad}"
            if time.monotonic() >= deadline:
                raise BackupAutomationError(
                    "writer services did not recover before timeout: " + last_detail
                )
            time.sleep(min(2.0, max(0.0, deadline - time.monotonic())))

    def _emit_failure_alert(self, detail: str, backup_id: str) -> None:
        try:
            self._emit_alert("failure", detail, backup_id)
        except BaseException as alert_exc:
            print(
                "production backup failure alert ERROR: "
                f"backup_id={backup_id} alert={alert_exc}; original={detail[:1000]}",
                file=sys.stderr,
            )

    def _emit_alert(self, status_value: str, detail: str, backup_id: str) -> None:
        hook_env = self.automation["alert"]["hook_env"]
        hook = Path(_required_env(hook_env)).expanduser().resolve()
        if hook.is_symlink() or not hook.is_file() or not os.access(hook, os.X_OK):
            raise BackupAutomationError(f"alert hook is not executable: {hook}")
        payload = _canonical_json(
            {
                "schema_version": "anila.production-backup-alert.v1",
                "status": status_value,
                "backup_id": backup_id,
                "detail": detail[:1000],
                "timestamp": _utc_now(),
            }
        )
        self.runner.run([str(hook)], cwd=self.repo_root, input_bytes=payload)

    def backup(self, backup_root: Path) -> Path:
        backup_id = (
            datetime.now(timezone.utc).strftime("backup-%Y%m%dT%H%M%SZ-")
            + secrets.token_hex(4)
        )
        try:
            return self._backup_impl(backup_root, backup_id)
        except BaseException as exc:
            notes = getattr(exc, "__notes__", [])
            detail = str(exc)
            if notes:
                detail += "; " + "; ".join(str(note) for note in notes)
            self._emit_failure_alert(detail, backup_id)
            raise

    def _backup_impl(self, backup_root: Path, backup_id: str) -> Path:
        _reject_ambient_compose_environment()
        self.verify_profile()
        for tool in ("docker", "age", "openssl"):
            if shutil.which(tool) is None:
                raise BackupAutomationError(f"required executable not found: {tool}")
        backup_root = _safe_directory(backup_root, label="backup root", create=True)
        _assert_outside_repo(backup_root, self.repo_root, label="backup root")
        off_host = _safe_directory(
            Path(_required_env(self.automation["off_host"]["directory_env"])),
            label="off-host backup directory",
        )
        _assert_outside_repo(off_host, self.repo_root, label="off-host directory")
        if off_host == backup_root or off_host in backup_root.parents or backup_root in off_host.parents:
            raise BackupAutomationError("off-host directory must be independent of backup root")
        _assert_off_host_mount(off_host, backup_root)
        signing_key = _required_file_env(
            self.automation["signature"]["signing_key_env"]
        )
        stage = backup_root / f".{backup_id}.staging"
        final = backup_root / backup_id
        compose: dict[str, Any] = {}
        running: set[str] = set()
        stopped: list[str] = []
        consistency_started = ""
        try:
            with SingleWriterLock(backup_root / ".production-backup.lock"):
                stage.mkdir(mode=0o700)
                compose = self.compose_config()
                services = compose.get("services")
                if not isinstance(services, dict):
                    raise BackupAutomationError("resolved Compose lacks services")
                runtime_image = services.get("csp", {}).get("image")
                db_image = services.get("csp-db", {}).get("image")
                if not isinstance(runtime_image, str) or not runtime_image:
                    raise BackupAutomationError("resolved CSP runtime image is missing")
                if not isinstance(db_image, str) or not db_image:
                    raise BackupAutomationError("resolved PostgreSQL image is missing")

                references = self._external_references()
                running = self._running_services()
                writers = self.automation["consistency"]["writer_services"]
                consistency_started = _utc_now()
                stopped = [service for service in writers if service in running]
                if stopped:
                    self._compose("stop", *stopped)
                redis_service = self.automation["consistency"]["redis_service"]
                if redis_service in running:
                    self._compose("exec", "-T", redis_service, "redis-cli", "SAVE")
                    self._compose("stop", redis_service)
                    stopped.append(redis_service)
                components: list[Component] = []
                references_bytes = _canonical_json(
                    {
                        "schema_version": "anila.external-key-references.v1",
                        "references": references,
                    }
                )
                reference_surfaces = tuple(
                    surface["id"]
                    for surface in self.profile["surfaces"]
                    if surface["backup_method"] == "key_management_reference"
                )
                components.append(
                    self._stream_to_age(
                        destination=stage / "external-references.age",
                        surfaces=reference_surfaces,
                        logical_format="external-references-json-v1",
                        source_bytes=references_bytes,
                    )
                )

                surface_rows: list[dict[str, Any]] = []
                for surface in sorted(
                    self.profile["surfaces"], key=lambda row: row["restore_order"]
                ):
                    disposition = surface["disposition"]
                    method = surface["backup_method"]
                    row = {
                        "id": surface["id"],
                        "disposition": disposition,
                        "backup_method": method,
                        "restore_order": surface["restore_order"],
                        "component": None,
                    }
                    if disposition != "required":
                        surface_rows.append(row)
                        continue
                    if method == "key_management_reference":
                        row["component"] = "external-references.age"
                        surface_rows.append(row)
                        continue
                    filename = f"{surface['id']}.age"
                    if method == "pg_dump":
                        db_service = self.automation["consistency"]["database_service"]
                        command = [
                            "docker", "compose", "-f", str(self.compose_path),
                            "exec", "-T", db_service,
                            "pg_dump", "-U", "csp", "-d", "csp", "-Fc",
                        ]
                        logical_format = "postgres-custom-v1"
                    else:
                        kind, source = self._resolved_source(surface, compose)
                        command = self._archive_command(
                            kind=kind, source=source, runtime_image=runtime_image
                        )
                        logical_format = "tar-gzip-tree-v1"
                    component = self._stream_to_age(
                        destination=stage / filename,
                        surfaces=(surface["id"],),
                        logical_format=logical_format,
                        source_argv=command,
                    )
                    components.append(component)
                    row["component"] = filename
                    surface_rows.append(row)

                consistency_finished = _utc_now()
                git_commit = self.runner.run(
                    ["git", "rev-parse", "HEAD"], cwd=self.repo_root
                ).stdout.decode().strip()
                git_branch = self.runner.run(
                    ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=self.repo_root
                ).stdout.decode().strip()
                profile_hash, _ = sha256_file(self.profile_path)
                compose_images = {
                    name: service["image"]
                    for name, service in sorted(services.items())
                    if isinstance(service, dict) and isinstance(service.get("image"), str)
                }
                manifest = {
                    "schema_version": MANIFEST_SCHEMA,
                    "backup_id": backup_id,
                    "created_at": _utc_now(),
                    "profile_id": self.profile["profile_id"],
                    "profile_sha256": profile_hash,
                    "consistency": {
                        "mode": self.automation["consistency"]["mode"],
                        "started_at": consistency_started,
                        "finished_at": consistency_finished,
                        "quiesced_services": sorted(stopped),
                    },
                    "source_release": {"git_commit": git_commit, "git_branch": git_branch},
                    "compose_images": compose_images,
                    "components": [component.as_dict() for component in components],
                    "surfaces": surface_rows,
                    "external_references": references,
                }
                manifest_component = self._stream_to_age(
                    destination=stage / "manifest.age",
                    surfaces=(),
                    logical_format="manifest-json-v1",
                    source_bytes=_canonical_json(manifest),
                )
                envelope = {
                    "schema_version": ENVELOPE_SCHEMA,
                    "backup_id": backup_id,
                    "created_at": _utc_now(),
                    "profile_id": self.profile["profile_id"],
                    "profile_sha256": profile_hash,
                    "manifest": {
                        key: value
                        for key, value in manifest_component.as_dict().items()
                        if key not in {"format", "surfaces"}
                    },
                    "components": [
                        {
                            "file": component.file,
                            "encrypted_sha256": component.encrypted_sha256,
                            "encrypted_size": component.encrypted_size,
                        }
                        for component in components
                    ],
                }
                envelope_path = stage / "envelope.json"
                envelope_path.write_bytes(_canonical_json(envelope))
                os.chmod(envelope_path, 0o600)
                signature_path = stage / "envelope.sig"
                self.runner.run(
                    [
                        "openssl", "dgst", "-sha256", "-sign", str(signing_key),
                        "-out", str(signature_path), str(envelope_path),
                    ],
                    cwd=self.repo_root,
                )
                if not signature_path.is_file() or signature_path.stat().st_size == 0:
                    raise BackupAutomationError("backup envelope signature is empty")

                # A backup is not successful while the quiesced application is
                # still down.  Recover and read back running/health state before
                # publishing either copy or emitting the success event.
                self._restart_and_verify_writers(stopped)
                stopped = []

                off_tmp = off_host / f".{backup_id}.copying"
                off_final = off_host / backup_id
                if off_tmp.exists() or off_final.exists() or final.exists():
                    raise BackupAutomationError("backup destination collision")
                shutil.copytree(stage, off_tmp)
                self._verify_copy_readback(stage, off_tmp)
                os.replace(off_tmp, off_final)
                os.replace(stage, final)
                self._apply_retention(backup_root)
                self._apply_retention(off_host)
                self._emit_alert("success", "backup published and off-host readback passed", backup_id)
                return final
        except BaseException as exc:
            shutil.rmtree(stage, ignore_errors=True)
            if stopped:
                try:
                    self._restart_and_verify_writers(stopped)
                except BaseException as restart_exc:
                    exc.add_note(f"writer recovery also failed: {restart_exc}")
            raise

    def _verify_copy_readback(self, source: Path, copied: Path) -> None:
        source_files = sorted(path.relative_to(source) for path in source.rglob("*") if path.is_file())
        copied_files = sorted(path.relative_to(copied) for path in copied.rglob("*") if path.is_file())
        if source_files != copied_files:
            raise BackupAutomationError("off-host copy file inventory mismatch")
        for relative in source_files:
            if sha256_file(source / relative) != sha256_file(copied / relative):
                raise BackupAutomationError(f"off-host copy readback mismatch: {relative}")

    def _apply_retention(self, backup_root: Path) -> None:
        raw_keep = os.environ.get(
            self.automation["retention"]["keep_env"],
            str(self.automation["retention"]["default_keep"]),
        )
        try:
            keep = int(raw_keep)
        except ValueError as exc:
            raise BackupAutomationError("backup retention must be an integer") from exc
        if not 1 <= keep <= 3650:
            raise BackupAutomationError("backup retention must be 1..3650")
        candidates: list[Path] = []
        for path in backup_root.iterdir():
            if not path.is_dir() or not BACKUP_NAME_RE.fullmatch(path.name):
                continue
            envelope = path / "envelope.json"
            signature = path / "envelope.sig"
            if not envelope.is_file() or not signature.is_file():
                continue
            parsed = strict_json_bytes(envelope.read_bytes(), label="retention envelope")
            if parsed.get("schema_version") != ENVELOPE_SCHEMA:
                continue
            candidates.append(path)
        for old in sorted(candidates, key=lambda item: item.name)[:-keep]:
            shutil.rmtree(old)


def validate_envelope(bundle: Path, public_key: Path) -> dict[str, Any]:
    if bundle.is_symlink() or not bundle.is_dir():
        raise BackupAutomationError("backup bundle must be a real directory")
    envelope_path = bundle / "envelope.json"
    signature_path = bundle / "envelope.sig"
    if (
        envelope_path.is_symlink()
        or signature_path.is_symlink()
        or not envelope_path.is_file()
        or not signature_path.is_file()
    ):
        raise BackupAutomationError("bundle lacks envelope or signature")
    try:
        verified = subprocess.run(
            [
                "openssl", "dgst", "-sha256", "-verify", str(public_key),
                "-signature", str(signature_path), str(envelope_path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as exc:
        raise BackupAutomationError(
            "backup envelope signature verification could not start"
        ) from exc
    if verified.returncode != 0:
        raise BackupAutomationError("backup envelope signature verification failed")
    envelope = strict_json_bytes(envelope_path.read_bytes(), label="envelope")
    _exact_keys(envelope, ENVELOPE_KEYS, "envelope")
    if envelope["schema_version"] != ENVELOPE_SCHEMA:
        raise BackupAutomationError("unsupported envelope schema")
    if not BACKUP_NAME_RE.fullmatch(envelope["backup_id"]):
        raise BackupAutomationError("invalid backup id")
    manifest = envelope["manifest"]
    if not isinstance(manifest, dict):
        raise BackupAutomationError("envelope manifest reference must be object")
    _exact_keys(manifest, MANIFEST_REF_KEYS, "envelope manifest")
    envelope_components = envelope["components"]
    if not isinstance(envelope_components, list) or not envelope_components:
        raise BackupAutomationError("envelope components must be non-empty")
    rows = [manifest, *envelope_components]
    names: set[str] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise BackupAutomationError("envelope component must be object")
        expected = MANIFEST_REF_KEYS if index == 0 else {
            "file", "encrypted_sha256", "encrypted_size"
        }
        _exact_keys(row, expected, "envelope component")
        name = row["file"]
        if not isinstance(name, str) or not COMPONENT_RE.fullmatch(name) or name in names:
            raise BackupAutomationError("invalid or duplicate envelope component name")
        names.add(name)
        path = bundle / name
        if path.is_symlink() or not path.is_file():
            raise BackupAutomationError(f"encrypted component missing: {name}")
        digest, size = sha256_file(path)
        if digest != row["encrypted_sha256"] or size != row["encrypted_size"]:
            raise BackupAutomationError(f"encrypted component checksum mismatch: {name}")
    return envelope


def _decrypt_bytes(path: Path, identity: Path) -> bytes:
    try:
        result = subprocess.run(
            ["age", "-d", "-i", str(identity), str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as exc:
        raise BackupAutomationError("age decryption could not start") from exc
    if result.returncode != 0:
        raise BackupAutomationError(
            "age authentication/decryption failed: "
            + result.stderr.decode("utf-8", errors="replace")[-1000:]
        )
    return result.stdout


def validate_manifest(
    manifest: dict[str, Any], envelope: dict[str, Any], profile_sha256: str
) -> None:
    _exact_keys(manifest, MANIFEST_KEYS, "manifest")
    if manifest["schema_version"] != MANIFEST_SCHEMA:
        raise BackupAutomationError("unsupported manifest schema")
    for key in ("backup_id", "profile_id", "profile_sha256"):
        if manifest[key] != envelope[key]:
            raise BackupAutomationError(f"manifest/envelope mismatch: {key}")
    if manifest["profile_sha256"] != profile_sha256:
        raise BackupAutomationError("backup profile checksum does not match restore profile")
    components = manifest["components"]
    if not isinstance(components, list) or not components:
        raise BackupAutomationError("manifest components must be non-empty")
    expected = {
        row["file"]: (row["encrypted_sha256"], row["encrypted_size"])
        for row in envelope["components"]
    }
    actual: set[str] = set()
    logical_surfaces: dict[str, set[str]] = {}
    for component in components:
        if not isinstance(component, dict):
            raise BackupAutomationError("manifest component must be object")
        _exact_keys(component, COMPONENT_KEYS, "manifest component")
        name = component["file"]
        if name in actual or name not in expected:
            raise BackupAutomationError("manifest component inventory mismatch")
        if (
            not isinstance(component["format"], str)
            or not isinstance(component["surfaces"], list)
            or not all(isinstance(item, str) and item for item in component["surfaces"])
            or len(set(component["surfaces"])) != len(component["surfaces"])
            or not FINGERPRINT_RE.fullmatch(
                "sha256:" + str(component["logical_sha256"])
            )
            or not isinstance(component["logical_size"], int)
            or component["logical_size"] < 0
        ):
            raise BackupAutomationError(f"manifest logical metadata invalid: {name}")
        actual.add(name)
        logical_surfaces[name] = set(component["surfaces"])
        if expected[name] != (
            component["encrypted_sha256"], component["encrypted_size"]
        ):
            raise BackupAutomationError(f"manifest encrypted metadata mismatch: {name}")
    if actual != set(expected):
        raise BackupAutomationError("manifest omits encrypted components")

    consistency = manifest["consistency"]
    if not isinstance(consistency, dict):
        raise BackupAutomationError("manifest consistency must be object")
    _exact_keys(
        consistency,
        {"mode", "started_at", "finished_at", "quiesced_services"},
        "manifest consistency",
    )
    if (
        consistency["mode"] != "quiesced-writers-v1"
        or not isinstance(consistency["quiesced_services"], list)
        or not all(isinstance(item, str) and item for item in consistency["quiesced_services"])
    ):
        raise BackupAutomationError("manifest consistency metadata invalid")

    release = manifest["source_release"]
    if not isinstance(release, dict):
        raise BackupAutomationError("manifest source release must be object")
    _exact_keys(release, {"git_commit", "git_branch"}, "manifest source release")
    if not re.fullmatch(r"[0-9a-f]{40}", str(release["git_commit"])):
        raise BackupAutomationError("manifest git commit invalid")
    images = manifest["compose_images"]
    if not isinstance(images, dict) or not images or not all(
        isinstance(key, str) and key and isinstance(value, str) and value
        for key, value in images.items()
    ):
        raise BackupAutomationError("manifest Compose image inventory invalid")

    surface_ids: set[str] = set()
    mapped_components: set[str] = set()
    for surface in manifest["surfaces"]:
        if not isinstance(surface, dict):
            raise BackupAutomationError("manifest surface must be object")
        _exact_keys(
            surface,
            {"id", "disposition", "backup_method", "restore_order", "component"},
            "manifest surface",
        )
        surface_id = surface["id"]
        component_name = surface["component"]
        if not isinstance(surface_id, str) or not surface_id or surface_id in surface_ids:
            raise BackupAutomationError("manifest surface id invalid or duplicate")
        surface_ids.add(surface_id)
        if surface["disposition"] == "required":
            if component_name not in actual:
                raise BackupAutomationError("required surface lacks component")
            if surface_id not in logical_surfaces[component_name]:
                raise BackupAutomationError("surface/component mapping mismatch")
            mapped_components.add(component_name)
        elif component_name is not None:
            raise BackupAutomationError("non-required surface must not map component")
    if mapped_components != actual:
        raise BackupAutomationError("manifest contains an unmapped encrypted component")

    reference_ids: set[str] = set()
    for reference in manifest["external_references"]:
        if not isinstance(reference, dict):
            raise BackupAutomationError("external reference must be object")
        _exact_keys(reference, {"id", "reference", "fingerprint"}, "external reference")
        reference_id = reference["id"]
        if (
            not isinstance(reference_id, str)
            or not reference_id
            or reference_id in reference_ids
            or not isinstance(reference["reference"], str)
            or len(reference["reference"]) < 8
            or not FINGERPRINT_RE.fullmatch(str(reference["fingerprint"]))
        ):
            raise BackupAutomationError("external reference metadata invalid")
        reference_ids.add(reference_id)


def _assert_new_disposable_target(
    target: Path,
    repo_root: Path,
    *,
    forbidden_roots: Iterable[Path] = (),
) -> Path:
    target = target.expanduser().absolute()
    if target.exists():
        raise BackupAutomationError("restore target must not already exist")
    resolved_parent = target.parent.resolve()
    target = resolved_parent / target.name
    repo_root = repo_root.resolve()
    if target == repo_root or repo_root in target.parents or target == Path(target.anchor):
        raise BackupAutomationError("restore target must be disposable and outside repo")
    state_dir = os.environ.get("ANILA_STATE_DIR")
    if state_dir:
        live = Path(state_dir).expanduser().resolve()
        if target == live or live in target.parents or target in live.parents:
            raise BackupAutomationError("restore target overlaps live ANILA_STATE_DIR")
    for raw_root in forbidden_roots:
        root = raw_root.expanduser().resolve()
        if target == root or root in target.parents or target in root.parents:
            raise BackupAutomationError("restore target overlaps backup bundle")
    return target


def _safe_extract_stream(stream: BinaryIO, destination: Path) -> tuple[str, int]:
    hashing = _HashingReader(stream)
    with tarfile.open(fileobj=hashing, mode="r|gz") as archive:
        for member in archive:
            pure = PurePosixPath(member.name)
            if (
                pure.is_absolute()
                or ".." in pure.parts
                or member.issym()
                or member.islnk()
                or member.isdev()
            ):
                raise BackupAutomationError(f"unsafe archive member: {member.name}")
            output = destination.joinpath(*pure.parts)
            if member.isdir():
                output.mkdir(parents=True, exist_ok=True, mode=0o700)
                continue
            if not member.isfile():
                raise BackupAutomationError(f"unsupported archive member: {member.name}")
            output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            source = archive.extractfile(member)
            if source is None:
                raise BackupAutomationError(f"archive file unreadable: {member.name}")
            with output.open("xb") as handle:
                shutil.copyfileobj(source, handle, 1024 * 1024)
            os.chmod(output, stat.S_IMODE(member.mode) & 0o700 or 0o600)
    return hashing.digest.hexdigest(), hashing.size


def prepare_restore(
    *, bundle: Path, target: Path, repo_root: Path, profile_path: Path
) -> Path:
    bundle = bundle.expanduser()
    if bundle.is_symlink() or not bundle.is_dir():
        raise BackupAutomationError("backup bundle must be a real directory")
    bundle = bundle.resolve()
    public_key = _required_file_env("ANILA_BACKUP_SIGNING_PUBLIC_KEY_FILE")
    identity = _required_file_env("ANILA_BACKUP_AGE_IDENTITY_FILE")
    envelope = validate_envelope(bundle, public_key)
    manifest_raw = _decrypt_bytes(bundle / envelope["manifest"]["file"], identity)
    manifest_ref = envelope["manifest"]
    if (
        hashlib.sha256(manifest_raw).hexdigest() != manifest_ref["logical_sha256"]
        or len(manifest_raw) != manifest_ref["logical_size"]
    ):
        raise BackupAutomationError("decrypted manifest checksum mismatch")
    manifest = strict_json_bytes(manifest_raw, label="manifest")
    profile_hash, _ = sha256_file(profile_path)
    validate_manifest(manifest, envelope, profile_hash)
    target = _assert_new_disposable_target(
        target, repo_root, forbidden_roots=(bundle,)
    )
    target.mkdir(mode=0o700)
    try:
        for component in manifest["components"]:
            encrypted = bundle / component["file"]
            try:
                process = subprocess.Popen(
                    ["age", "-d", "-i", str(identity), str(encrypted)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            except OSError as exc:
                raise BackupAutomationError(
                    "age component decryption could not start"
                ) from exc
            assert process.stdout is not None
            try:
                if component["format"] == "tar-gzip-tree-v1":
                    surfaces = component["surfaces"]
                    if len(surfaces) != 1:
                        raise BackupAutomationError("tree component must map one surface")
                    destination = target / "surfaces" / surfaces[0]
                    destination.mkdir(parents=True, mode=0o700)
                    digest, size = _safe_extract_stream(process.stdout, destination)
                else:
                    output_name = (
                        "db-csp.dump"
                        if component["format"] == "postgres-custom-v1"
                        else "external-references.json"
                    )
                    output = target / output_name
                    digest_obj = hashlib.sha256()
                    size = 0
                    with output.open("xb") as handle:
                        for chunk in iter(
                            lambda: process.stdout.read(1024 * 1024), b""
                        ):
                            digest_obj.update(chunk)
                            size += len(chunk)
                            handle.write(chunk)
                    digest = digest_obj.hexdigest()
                    os.chmod(output, 0o600)
                stderr = process.stderr.read() if process.stderr else b""
                if process.wait() != 0:
                    raise BackupAutomationError(
                        "age component decryption failed: "
                        + stderr.decode("utf-8", errors="replace")[-1000:]
                    )
                if (
                    digest != component["logical_sha256"]
                    or size != component["logical_size"]
                ):
                    raise BackupAutomationError(
                        f"decrypted component checksum mismatch: {component['file']}"
                    )
            except BaseException:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                raise
        (target / "manifest.json").write_bytes(_canonical_json(manifest))
        (target / "RESTORE_PREPARED.json").write_bytes(
            _canonical_json(
                {
                    "schema_version": PREPARED_SCHEMA,
                    "backup_id": manifest["backup_id"],
                    "prepared_at": _utc_now(),
                    "destructive_restore_authorized": False,
                }
            )
        )
        return target
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)
        raise


def _docker_output(argv: list[str]) -> str:
    try:
        result = subprocess.run(
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False
        )
    except OSError as exc:
        command = argv[0] if argv else "command"
        raise BackupAutomationError(f"command failed to start ({command})") from exc
    if result.returncode != 0:
        raise BackupAutomationError(
            result.stderr.decode("utf-8", errors="replace")[-2000:]
        )
    return result.stdout.decode("utf-8", errors="replace")


def _validate_restore_db_metrics(values: list[str]) -> dict[str, int]:
    names = (
        "chunk_document_orphans",
        "active_leaf_invalid_embeddings",
        "indexed_documents",
        "active_generation_contract_violations",
        "active_generation_chunk_count_mismatches",
    )
    if len(values) != len(names):
        raise BackupAutomationError("restore database smoke returned invalid metrics")
    try:
        metrics = {name: int(value) for name, value in zip(names, values)}
    except ValueError as exc:
        raise BackupAutomationError("restore database smoke metrics are not integers") from exc
    violations = {
        name: value
        for name, value in metrics.items()
        if name != "indexed_documents" and value != 0
    }
    if violations:
        raise BackupAutomationError(
            f"active vector generation restore smoke failed: {violations}"
        )
    return metrics


def restore_smoke(prepared: Path, profile: dict[str, Any]) -> dict[str, Any]:
    marker = strict_json_bytes(
        (prepared / "RESTORE_PREPARED.json").read_bytes(), label="restore marker"
    )
    if marker.get("schema_version") != PREPARED_SCHEMA:
        raise BackupAutomationError("target is not a prepared disposable restore")
    manifest = strict_json_bytes(
        (prepared / "manifest.json").read_bytes(), label="prepared manifest"
    )
    image = manifest.get("compose_images", {}).get("csp-db")
    if not isinstance(image, str) or not image:
        raise BackupAutomationError("manifest lacks locked csp-db image")
    dump = prepared / "db-csp.dump"
    if not dump.is_file():
        raise BackupAutomationError("prepared restore lacks PostgreSQL dump")
    name = "anila-restore-smoke-" + secrets.token_hex(4)
    password = secrets.token_urlsafe(24)
    report: dict[str, Any] = {"container": name, "checks": {}}
    try:
        _docker_output([
            "docker", "run", "-d", "--rm", "--name", name,
            "--network", "none", "-e", f"POSTGRES_PASSWORD={password}", image,
        ])
        ready = False
        for _ in range(60):
            result = subprocess.run(
                ["docker", "exec", name, "pg_isready", "-U", "postgres"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if result.returncode == 0:
                ready = True
                break
            import time
            time.sleep(1)
        if not ready:
            raise BackupAutomationError("disposable PostgreSQL did not become ready")
        _docker_output(["docker", "cp", str(dump), f"{name}:/tmp/db-csp.dump"])
        _docker_output(["docker", "exec", name, "createdb", "-U", "postgres", "csp"])
        _docker_output([
            "docker", "exec", name, "pg_restore", "-U", "postgres",
            "-d", "csp", "--no-owner", "--no-privileges", "/tmp/db-csp.dump",
        ])
        query = (
            "SELECT "
            "(SELECT count(*) FROM document_chunks c LEFT JOIN ingestion_documents d "
            "ON d.id=c.document_id WHERE d.id IS NULL),"
            "(SELECT count(*) FROM document_chunks c "
            "JOIN ingestion_document_generations g ON g.id=c.generation_id "
            "JOIN ingestion_documents d ON d.id=g.document_id "
            "JOIN ingestion_collections ic ON ic.id=g.collection_id "
            "WHERE c.chunk_type='leaf' AND ((c.generation_id=d.active_generation_id "
            "AND (c.embedding IS NULL OR c.is_active_generation IS NOT TRUE "
            "OR c.document_id<>d.id OR c.collection_id<>d.collection_id "
            "OR g.document_id<>d.id OR g.collection_id<>d.collection_id "
            "OR vector_dims(c.embedding)<>g.embedding_dim)) "
            "OR (c.is_active_generation IS TRUE "
            "AND c.generation_id IS DISTINCT FROM d.active_generation_id))),"
            "(SELECT count(*) FROM ingestion_documents WHERE status='indexed'),"
            "(SELECT count(*) FROM ingestion_documents d "
            "LEFT JOIN ingestion_document_generations g ON g.id=d.active_generation_id "
            "AND g.document_id=d.id AND g.collection_id=d.collection_id "
            "LEFT JOIN ingestion_collections ic ON ic.id=d.collection_id "
            "WHERE d.active_generation_id IS NOT NULL AND (g.id IS NULL OR g.status<>'active' "
            "OR g.embedding_dim<=0 OR g.embedding_fingerprint "
            "!~ '^sha256:[0-9a-f]{64}$' OR ic.id IS NULL "
            "OR g.embedding_model<>ic.embedding_model "
            "OR g.embedding_fingerprint<>ic.embedding_fingerprint "
            "OR g.embedding_dim<>ic.embedding_dim)) ,"
            "(SELECT count(*) FROM ingestion_document_generations g "
            "JOIN ingestion_documents d ON d.active_generation_id=g.id "
            "WHERE g.chunk_count <> (SELECT count(*) FROM document_chunks c "
            "WHERE c.generation_id=g.id));"
        )
        values = _docker_output([
            "docker", "exec", name, "psql", "-U", "postgres", "-d", "csp",
            "-At", "-F", "|", "-c", query,
        ]).strip().split("|")
        metrics = _validate_restore_db_metrics(values)
        report["checks"].update(metrics)

        storage_paths = _docker_output([
            "docker", "exec", name, "psql", "-U", "postgres", "-d", "csp",
            "-At", "-c",
            "SELECT storage_path FROM ingestion_documents WHERE storage_path IS NOT NULL;",
        ]).splitlines()
        archive_surfaces = {
            surface["id"]: surface
            for surface in profile["surfaces"]
            if surface["backup_method"] in {
                "filesystem_archive", "volume_archive", "service_native",
                "redis_snapshot",
            }
        }
        missing: list[str] = []
        for raw in storage_paths:
            matched = False
            for surface_id, surface in archive_surfaces.items():
                for mount in surface["source"]["mounts"]:
                    prefix = mount["target"].rstrip("/") + "/"
                    if raw.startswith(prefix):
                        relative = raw[len(prefix):]
                        if (prepared / "surfaces" / surface_id / relative).is_file():
                            matched = True
                            break
                if matched:
                    break
            if not matched:
                missing.append(raw)
        if missing:
            raise BackupAutomationError(
                f"database blob references missing from restored surfaces: {missing[:10]}"
            )
        attachment_paths = _docker_output([
            "docker", "exec", name, "psql", "-U", "postgres", "-d", "csp",
            "-At", "-c", "SELECT storage_path FROM attachments;",
        ]).splitlines()
        artifact_keys = _docker_output([
            "docker", "exec", name, "psql", "-U", "postgres", "-d", "csp",
            "-At", "-c",
            "SELECT blob_key FROM artifact_versions WHERE blob_key IS NOT NULL;",
        ]).splitlines()

        def require_relative_files(surface_id: str, values: list[str]) -> None:
            surface_root = prepared / "surfaces" / surface_id
            absent: list[str] = []
            for value in values:
                pure = PurePosixPath(value)
                if pure.is_absolute() or ".." in pure.parts:
                    raise BackupAutomationError(
                        f"unsafe database blob reference for {surface_id}: {value}"
                    )
                if not surface_root.joinpath(*pure.parts).is_file():
                    absent.append(value)
            if absent:
                raise BackupAutomationError(
                    f"database blob references missing for {surface_id}: {absent[:10]}"
                )

        require_relative_files("csp-conversation-attachments", attachment_paths)
        require_relative_files("csp-artifact-blobs", artifact_keys)
        report["checks"]["ingestion_blob_references"] = len(storage_paths)
        report["checks"]["attachment_blob_references"] = len(attachment_paths)
        report["checks"]["artifact_blob_references"] = len(artifact_keys)
        report["status"] = "pass"
        (prepared / "RESTORE_SMOKE.json").write_bytes(_canonical_json(report))
        return report
    finally:
        subprocess.run(
            ["docker", "rm", "-f", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )


def _defaults() -> tuple[Path, Path]:
    repo = Path(__file__).resolve().parents[3]
    profile = repo / "infra/deployment/backup/production-backup-profile.v1.json"
    return repo, profile


def build_parser() -> argparse.ArgumentParser:
    repo, profile = _defaults()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=repo)
    parser.add_argument("--profile", type=Path, default=profile)
    sub = parser.add_subparsers(dest="command", required=True)
    backup = sub.add_parser("backup")
    backup.add_argument("--backup-root", type=Path, required=True)
    verify = sub.add_parser("verify")
    verify.add_argument("bundle", type=Path)
    prepare = sub.add_parser("restore-prepare")
    prepare.add_argument("bundle", type=Path)
    prepare.add_argument("--target", type=Path, required=True)
    smoke = sub.add_parser("restore-smoke")
    smoke.add_argument("bundle", type=Path)
    smoke.add_argument("--target", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo = args.repo_root.resolve()
    profile_path = args.profile.resolve()
    try:
        automation = ProductionBackup(repo, profile_path)
        if args.command == "backup":
            result = automation.backup(args.backup_root)
            print(result)
        elif args.command == "verify":
            automation.verify_profile()
            public_key = _required_file_env("ANILA_BACKUP_SIGNING_PUBLIC_KEY_FILE")
            bundle = args.bundle.expanduser()
            envelope = validate_envelope(bundle, public_key)
            identity = _required_file_env("ANILA_BACKUP_AGE_IDENTITY_FILE")
            manifest_raw = _decrypt_bytes(
                bundle / envelope["manifest"]["file"], identity
            )
            manifest_ref = envelope["manifest"]
            if (
                hashlib.sha256(manifest_raw).hexdigest()
                != manifest_ref["logical_sha256"]
                or len(manifest_raw) != manifest_ref["logical_size"]
            ):
                raise BackupAutomationError("decrypted manifest checksum mismatch")
            profile_hash, _ = sha256_file(profile_path)
            validate_manifest(
                strict_json_bytes(manifest_raw, label="manifest"),
                envelope,
                profile_hash,
            )
            print(f"backup verify PASS: {envelope['backup_id']}")
        elif args.command in {"restore-prepare", "restore-smoke"}:
            automation.verify_profile()
            prepared = prepare_restore(
                bundle=args.bundle,
                target=args.target,
                repo_root=repo,
                profile_path=profile_path,
            )
            if args.command == "restore-smoke":
                report = restore_smoke(prepared, automation.profile)
                print(json.dumps(report, ensure_ascii=False, sort_keys=True))
            else:
                print(prepared)
        return 0
    except BackupAutomationError as exc:
        print(f"production backup ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
