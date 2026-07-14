# -*- coding: utf-8 -*-
"""Immutable, CSP-owned artifact blob storage.

Clients provide bytes plus claims (size/hash/MIME); they never choose a path
or object key.  A blob is first fsynced to a private temporary file and then
published with a same-filesystem hard link.  ``os.link`` is atomic and refuses
an existing destination, so a collision can never overwrite prior bytes.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from app.services.content_sniffing import ContentValidationError, validate_content


_KEY_RE = re.compile(r"^[0-9a-f]{2}/[0-9a-f]{64}\.[a-z0-9]{1,8}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CHUNK_SIZE = 1024 * 1024
_MAX_ARTIFACT_BYTES = 100 * 1024 * 1024

_PRIMARY_FORMATS: dict[str, tuple[str, str]] = {
    "slides": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".pptx",
    ),
    "report": ("application/pdf", ".pdf"),
    "mindmap": ("image/svg+xml", ".svg"),
    "infographic": ("application/pdf", ".pdf"),
    "datatable": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xlsx",
    ),
}


class BlobValidationError(ValueError):
    """The supplied bytes do not match the declared artifact contract."""


@dataclass(frozen=True)
class StoredBlob:
    key: str
    size_bytes: int
    sha256: str
    media_type: str
    original_filename: str


def _root(path: str | Path) -> Path:
    root = Path(path).expanduser().absolute()
    try:
        if stat.S_ISLNK(root.lstat().st_mode):
            raise BlobValidationError("artifact blob root 不可為 symlink")
    except FileNotFoundError:
        pass
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        root.chmod(0o700)
    except OSError:
        pass
    return root


def _private_child_dir(root: Path, name: str) -> Path:
    child = root / name
    try:
        if stat.S_ISLNK(child.lstat().st_mode):
            raise BlobValidationError("artifact blob 子目錄不可為 symlink")
    except FileNotFoundError:
        child.mkdir(mode=0o700)
    if not child.is_dir():
        raise BlobValidationError("artifact blob 子路徑必須是目錄")
    return child


def _safe_filename(filename: str, *, extension: str) -> str:
    # Display metadata only. Never participates in the filesystem path.
    name = Path((filename or "").replace("\\", "/")).name.strip()
    if not name or name in {".", ".."}:
        name = f"artifact{extension}"
    name = "".join(ch for ch in name if ch >= " " and ch not in {'"', "\\", "/"})
    if len(name) > 240:
        name = name[: 240 - len(extension)] + extension
    if not name.lower().endswith(extension):
        raise BlobValidationError(f"filename 必須使用 {extension} 副檔名")
    return name


def store_stream(
    stream: BinaryIO,
    *,
    storage_root: str | Path,
    artifact_type: str,
    declared_sha256: str,
    declared_size: int,
    media_type: str,
    original_filename: str,
) -> StoredBlob:
    """Persist one immutable primary artifact and verify every client claim."""
    expected = _PRIMARY_FORMATS.get(artifact_type)
    if expected is None:
        raise BlobValidationError("不支援的 artifact_type")
    expected_media_type, extension = expected
    if media_type != expected_media_type:
        raise BlobValidationError("artifact_type 與 MIME 不符")
    if not _SHA256_RE.fullmatch(declared_sha256 or ""):
        raise BlobValidationError("content_sha256 必須是 64 字元小寫 hex")
    if declared_size <= 0:
        raise BlobValidationError("content_size 必須大於 0")
    if declared_size > _MAX_ARTIFACT_BYTES:
        raise BlobValidationError("artifact 超過 100 MiB 上限")
    filename = _safe_filename(original_filename, extension=extension)

    root = _root(storage_root)
    temp_dir = _private_child_dir(root, ".tmp")
    token = secrets.token_hex(32)
    temp_path = temp_dir / token
    fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    digest = hashlib.sha256()
    size = 0
    try:
        with os.fdopen(fd, "wb", closefd=True) as handle:
            while True:
                chunk = stream.read(_CHUNK_SIZE)
                if not chunk:
                    break
                if not isinstance(chunk, bytes):
                    raise BlobValidationError("upload stream 必須產生 bytes")
                size += len(chunk)
                if size > declared_size:
                    raise BlobValidationError("實際 bytes 超過宣告大小")
                digest.update(chunk)
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())

        actual_hash = digest.hexdigest()
        if size != declared_size:
            raise BlobValidationError("實際 bytes 與宣告大小不符")
        if actual_hash != declared_sha256:
            raise BlobValidationError("實際 bytes 與宣告 SHA-256 不符")
        try:
            validate_content(
                temp_path.read_bytes(),
                filename=filename,
                declared_mime=media_type,
                allowed_extensions=frozenset({extension}),
            )
        except ContentValidationError as exc:
            raise BlobValidationError(f"artifact 內容格式不符: {exc}") from exc

        key = f"{actual_hash[:2]}/{secrets.token_hex(32)}{extension}"
        final_path = _private_child_dir(root, actual_hash[:2]) / Path(key).name
        # Atomic no-replace publication. Existing bytes are never opened.
        os.link(temp_path, final_path)
        try:
            final_path.chmod(0o600)
        except OSError:
            pass
        return StoredBlob(
            key=key,
            size_bytes=size,
            sha256=actual_hash,
            media_type=media_type,
            original_filename=filename,
        )
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def resolve_blob_path(storage_root: str | Path, key: str) -> Path:
    """Resolve an opaque server key while rejecting traversal/legacy paths."""
    if not _KEY_RE.fullmatch(key or ""):
        raise BlobValidationError("非法 artifact blob key")
    root = _root(storage_root)
    parent = _private_child_dir(root, key[:2])
    path = parent / Path(key).name
    try:
        if stat.S_ISLNK(path.lstat().st_mode):
            raise BlobValidationError("artifact blob 不可為 symlink")
    except FileNotFoundError:
        pass
    if path.parent != parent or parent.parent != root:
        raise BlobValidationError("artifact blob key 越界")
    return path


def remove_blob(storage_root: str | Path, key: str) -> None:
    path = resolve_blob_path(storage_root, key)
    try:
        path.unlink()
    except FileNotFoundError:
        pass


__all__ = [
    "BlobValidationError",
    "StoredBlob",
    "remove_blob",
    "resolve_blob_path",
    "store_stream",
]
