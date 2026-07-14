#!/usr/bin/env python3
"""Copy CSP runtime data from a read-only Docker mount into a backup mount.

This helper is intentionally small and dependency-free so ``anila-ops.sh`` can
pipe it to the already-loaded CSP image in an air-gapped deployment.  It runs
as root *inside the one-shot container* because the production bind mounts and
named volume are owned by the CSP runtime UID (10001) with mode 0700.

Security invariants:

* the source and destination roots are supplied as exact Docker mounts;
* JWT backup accepts only two fixed regular-file names and opens them with
  ``O_NOFOLLOW``;
* tree archives never dereference symlinks;
* outputs are created exclusively, chmod 0600, then returned to the invoking
  host operator's UID/GID.
"""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import sys
import tarfile
from pathlib import Path


JWT_FILES = ("jwt-private.pem", "jwt-public.pem")
TLS_FILES = ("server.key", "server.crt")
TREE_KINDS = {
    "uploads": "uploads.tar.gz",
    "attachments": "attachments.tar.gz",
    "source-snapshots": "source-snapshots.tar.gz",
}


class BackupError(RuntimeError):
    """An input violates the runtime-backup safety contract."""


def _require_directory(path: Path, label: str) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise BackupError(f"{label} does not exist: {path}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise BackupError(f"{label} must be a real directory, not a symlink: {path}")


def _open_regular_nofollow(path: Path) -> int:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise BackupError(f"required runtime secret is missing: {path.name}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise BackupError(f"runtime secret must be a regular file: {path.name}")

    # Linux is the only supported production host.  Failing rather than
    # silently dropping O_NOFOLLOW preserves the source-to-open race defence.
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise BackupError("O_NOFOLLOW is unavailable on this platform")
    flags = os.O_RDONLY | os.O_CLOEXEC | nofollow
    try:
        return os.open(path, flags)
    except OSError as exc:
        raise BackupError(f"refused to open runtime secret: {path.name}: {exc}") from exc


def _open_output(path: Path, uid: int, gid: int) -> int:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise BackupError("O_NOFOLLOW is unavailable on this platform")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | nofollow
    fd = os.open(path, flags, 0o600)
    os.fchmod(fd, 0o600)
    os.fchown(fd, uid, gid)
    return fd


def backup_fixed_files(
    source: Path,
    destination: Path,
    output_name: str,
    names: tuple[str, ...],
    uid: int,
    gid: int,
) -> None:
    _require_directory(source, f"{output_name} source")
    _require_directory(destination, "backup destination")

    output_directory = destination / output_name
    output_directory.mkdir(mode=0o700, exist_ok=False)
    os.chmod(output_directory, 0o700)
    os.chown(output_directory, uid, gid)

    for name in names:
        source_fd = _open_regular_nofollow(source / name)
        try:
            output_fd = _open_output(output_directory / name, uid, gid)
            try:
                with os.fdopen(source_fd, "rb", closefd=False) as input_file:
                    with os.fdopen(output_fd, "wb", closefd=False) as output_file:
                        shutil.copyfileobj(input_file, output_file)
                        output_file.flush()
                        os.fsync(output_fd)
            finally:
                os.close(output_fd)
        finally:
            os.close(source_fd)


def backup_secrets(source: Path, destination: Path, uid: int, gid: int) -> None:
    backup_fixed_files(source, destination, "secrets", JWT_FILES, uid, gid)


def backup_tls(source: Path, destination: Path, uid: int, gid: int) -> None:
    backup_fixed_files(source, destination, "tls", TLS_FILES, uid, gid)


def backup_tree(
    source: Path,
    destination: Path,
    kind: str,
    uid: int,
    gid: int,
) -> None:
    _require_directory(source, f"{kind} source")
    _require_directory(destination, "backup destination")

    output_path = destination / TREE_KINDS[kind]
    output_fd = _open_output(output_path, uid, gid)
    try:
        with os.fdopen(output_fd, "wb", closefd=False) as output_file:
            # dereference=False is the critical boundary: a malicious symlink
            # is represented as a link in the archive; its target is never read.
            with tarfile.open(
                fileobj=output_file,
                mode="w:gz",
                format=tarfile.PAX_FORMAT,
                dereference=False,
            ) as archive:
                archive.add(source, arcname=kind, recursive=True)
            output_file.flush()
            os.fsync(output_fd)
    finally:
        os.close(output_fd)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--source", type=Path, required=True)
    common.add_argument("--destination", type=Path, required=True)
    common.add_argument("--uid", type=int, required=True)
    common.add_argument("--gid", type=int, required=True)

    subparsers.add_parser("secrets", parents=[common])
    subparsers.add_parser("tls", parents=[common])
    tree = subparsers.add_parser("tree", parents=[common])
    tree.add_argument("--kind", choices=tuple(TREE_KINDS), required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "secrets":
            backup_secrets(args.source, args.destination, args.uid, args.gid)
        elif args.command == "tls":
            backup_tls(args.source, args.destination, args.uid, args.gid)
        else:
            backup_tree(
                args.source,
                args.destination,
                args.kind,
                args.uid,
                args.gid,
            )
    except (BackupError, OSError, tarfile.TarError) as exc:
        print(f"runtime backup refused: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
