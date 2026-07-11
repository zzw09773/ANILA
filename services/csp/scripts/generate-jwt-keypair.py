#!/usr/bin/env python3
"""Generate an RSA-2048 keypair for RS256 JWT signing.

Sprint 9 / anila-studio extraction: CSP signs JWTs with a private key,
anila-studio verifies with the matching public key (fetched via JWKS).
This script writes both PEMs to ``backend/secrets/`` with strict
permissions so a misconfigured volume mount or stray ``git add .`` does
not leak signing material.

Usage::

    python scripts/generate-jwt-keypair.py
    python scripts/generate-jwt-keypair.py --output-dir /etc/anila/keys
    python scripts/generate-jwt-keypair.py --force            # overwrite

Safety guarantees:

* Private key file mode is ``0o600`` (owner read/write only).
* Public key file mode is ``0o644`` (readable by JWKS consumers).
* Existing files are not overwritten unless ``--force`` is passed —
  rotation should be deliberate.
* Private key is serialised as PKCS#8 / unencrypted PEM, matching the
  format ``python-jose`` accepts directly via ``jwt.encode``.
"""
from __future__ import annotations

import argparse
import os
import stat
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


# Default landing directory relative to the backend root. The actual
# absolute path resolves at runtime based on where the script is invoked
# from (cwd) — see ``main()``.
DEFAULT_OUTPUT_SUBDIR = "secrets"

PRIVATE_FILENAME = "jwt-private.pem"
PUBLIC_FILENAME = "jwt-public.pem"

PRIVATE_MODE = 0o600
PUBLIC_MODE = 0o644
MIN_RSA_KEY_SIZE = 2048


def _path_lexists(path: Path) -> bool:
    """Return True for regular entries and broken symlinks alike."""
    return os.path.lexists(path)


def _read_regular_nofollow(path: Path) -> bytes:
    """Read one existing regular file without following a final symlink."""
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"keypair entry is not a regular file: {path}")

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if nofollow:
        flags |= nofollow
    fd = os.open(path, flags)
    try:
        with os.fdopen(fd, "rb", closefd=False) as handle:
            return handle.read()
    finally:
        os.close(fd)


def validate_existing_keypair(private_path: Path, public_path: Path) -> None:
    """Require a complete, matching RSA keypair at two fixed paths."""
    private_pem = _read_regular_nofollow(private_path)
    public_pem = _read_regular_nofollow(public_path)
    private_key = serialization.load_pem_private_key(private_pem, password=None)
    public_key = serialization.load_pem_public_key(public_pem)
    if not isinstance(private_key, rsa.RSAPrivateKey):
        raise ValueError("JWT private key is not RSA")
    if not isinstance(public_key, rsa.RSAPublicKey):
        raise ValueError("JWT public key is not RSA")
    if private_key.key_size < MIN_RSA_KEY_SIZE:
        raise ValueError(
            f"JWT RSA key is too small: {private_key.key_size} < {MIN_RSA_KEY_SIZE}"
        )
    if private_key.public_key().public_numbers().e != 65537:
        raise ValueError("JWT RSA public exponent must be 65537")
    if private_key.public_key().public_numbers() != public_key.public_numbers():
        raise ValueError("JWT private/public keys do not match")

    # ``--ensure`` is invoked as the CSP runtime UID after the deployment
    # helper has taken ownership of the directory. Reassert modes without
    # widening directory access or involving the host operator.
    os.chmod(private_path, PRIVATE_MODE)
    os.chmod(public_path, PUBLIC_MODE)


def generate_keypair(key_size: int = 2048) -> rsa.RSAPrivateKey:
    """Generate a fresh RSA private key.

    ``key_size=2048`` matches NIST guidance through 2030+. The matching
    public key is derived via ``private_key.public_key()`` — we do not
    persist them as separate objects, only their PEM serialisations.
    """
    return rsa.generate_private_key(
        public_exponent=65537,
        key_size=key_size,
    )


def write_private_pem(private_key: rsa.RSAPrivateKey, path: Path) -> None:
    """Serialise ``private_key`` to PKCS#8 PEM and write with ``0o600``."""
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    # Write+chmod in two steps so the mode is applied before content
    # exists on disk in any temp-file race window.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, PRIVATE_MODE)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(pem)
    except Exception:
        # If write fails the fd is already closed by ``with`` — no leak.
        raise
    # Re-assert mode in case umask interfered (older platforms ignored
    # the mode arg on O_CREAT for already-existing files).
    os.chmod(path, PRIVATE_MODE)


def write_public_pem(private_key: rsa.RSAPrivateKey, path: Path) -> None:
    """Serialise the matching public key to SPKI PEM with ``0o644``."""
    public_key = private_key.public_key()
    pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    path.write_bytes(pem)
    os.chmod(path, PUBLIC_MODE)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate RSA-2048 keypair for RS256 JWT signing.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help=(
            "Directory to write jwt-private.pem and jwt-public.pem into "
            "(default: backend/secrets/)."
        ),
    )
    parser.add_argument(
        "--key-size",
        type=int,
        default=2048,
        help="RSA key size in bits (default: 2048).",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--force",
        action="store_true",
        help=(
            "Overwrite existing pem files. Without this flag the script "
            "refuses to clobber an existing private key."
        ),
    )
    mode.add_argument(
        "--ensure",
        action="store_true",
        help=(
            "Idempotently keep a complete matching keypair, or generate one "
            "when both files are absent. Partial, symlinked, malformed, or "
            "mismatched pairs fail closed."
        ),
    )
    return parser.parse_args(argv)


def resolve_output_dir(cli_dir: Path | None) -> Path:
    """Honour ``--output-dir`` when supplied; otherwise resolve to
    ``<backend-root>/secrets``.

    This script lives in ``backend/scripts/`` so the backend root is the
    parent of the script's directory. Computing it via ``__file__``
    keeps the default working regardless of cwd, so ``python
    scripts/generate-jwt-keypair.py`` from anywhere lands the files
    next to ``app/config.py``.
    """
    if cli_dir is not None:
        return cli_dir
    backend_root = Path(__file__).resolve().parent.parent
    return backend_root / DEFAULT_OUTPUT_SUBDIR


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    output_dir = resolve_output_dir(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    private_path = output_dir / PRIVATE_FILENAME
    public_path = output_dir / PUBLIC_FILENAME

    private_exists = _path_lexists(private_path)
    public_exists = _path_lexists(public_path)

    if args.ensure and (private_exists or public_exists):
        if not (private_exists and public_exists):
            print(
                "refusing partial JWT keypair: both jwt-private.pem and "
                "jwt-public.pem are required",
                file=sys.stderr,
            )
            return 1
        try:
            validate_existing_keypair(private_path, public_path)
        except (OSError, ValueError, TypeError) as exc:
            print(f"refusing invalid JWT keypair: {exc}", file=sys.stderr)
            return 1
        print(f"existing JWT keypair is valid: {private_path} / {public_path}")
        return 0

    if (private_exists or public_exists) and not args.force:
        print(
            "refusing to overwrite existing keypair without --force: "
            f"{private_path} / {public_path}",
            file=sys.stderr,
        )
        return 1

    private_key = generate_keypair(key_size=args.key_size)
    write_private_pem(private_key, private_path)
    write_public_pem(private_key, public_path)

    print(f"wrote {private_path} (mode 0o{PRIVATE_MODE:o})")
    print(f"wrote {public_path} (mode 0o{PUBLIC_MODE:o})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
