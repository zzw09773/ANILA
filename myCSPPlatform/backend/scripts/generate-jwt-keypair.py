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
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Overwrite existing pem files. Without this flag the script "
            "refuses to clobber an existing private key."
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

    if (private_path.exists() or public_path.exists()) and not args.force:
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
