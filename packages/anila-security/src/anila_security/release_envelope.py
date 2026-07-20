"""Offline verification contract for a Gate 6 P4 release envelope.

This module verifies an *attestation*; it does not build a release, mint an
acceptance decision, or sign any bytes.  The envelope and its release-owner
trust store live outside the bundle directory.  ``bundle_inventory`` is a
closed set of regular files rooted at the explicit ``bundle_root`` argument,
so the envelope cannot accidentally hash itself.

The verifier intentionally returns a deterministic non-acceptance result even
when every synthetic cryptographic check succeeds.  Production evidence (a
real release-owner trust decision, production SBOM generation, clean-host
air-gap deployment, and runtime-envelope readback) remains an external Gate 6
requirement.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import math
import os
import re
import secrets
import stat
import tempfile
import weakref
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from anila_security.production_acceptance_profile import (
    VerifiedProductionAcceptanceProfile,
    verify_production_acceptance_profile,
)


RELEASE_ENVELOPE_SCHEMA = "anila.gate6.release-envelope.v1"
RELEASE_TRUST_STORE_SCHEMA = "anila.gate6.release-trust.v1"
RELEASE_VERIFICATION_SCHEMA = "anila.gate6.release-verification.v1"
RELEASE_ENVELOPE_STATUS = "VERIFIED_NON_ACCEPTANCE"
RELEASE_ENVELOPE_ACCEPTANCE_STATUS = "NOT_ACCEPTANCE"
RELEASE_ENVELOPE_EVIDENCE_CLASS = "non-acceptance-release-envelope"

RELEASE_ENVELOPE_ERROR_DUPLICATE_JSON_KEY = "RELEASE_ENVELOPE_DUPLICATE_JSON_KEY"
RELEASE_ENVELOPE_ERROR_INVALID_JSON = "RELEASE_ENVELOPE_INVALID_JSON"
RELEASE_ENVELOPE_ERROR_RESOURCE_LIMIT = "RELEASE_ENVELOPE_RESOURCE_LIMIT"
RELEASE_ENVELOPE_ERROR_INVALID_ENVELOPE = "RELEASE_ENVELOPE_INVALID_ENVELOPE"
RELEASE_ENVELOPE_ERROR_INVALID_SIGNATURE = "RELEASE_ENVELOPE_INVALID_SIGNATURE"
RELEASE_ENVELOPE_ERROR_INVALID_TRUST = "RELEASE_ENVELOPE_INVALID_TRUST"
RELEASE_ENVELOPE_ERROR_INVALID_P0 = "RELEASE_ENVELOPE_INVALID_P0"
RELEASE_ENVELOPE_ERROR_INVALID_BUNDLE = "RELEASE_ENVELOPE_INVALID_BUNDLE"
RELEASE_ENVELOPE_ERROR_NOT_EFFECTIVE = "RELEASE_ENVELOPE_NOT_EFFECTIVE"

RELEASE_ENVELOPE_MISSING_EXTERNAL_EVIDENCE = (
    "real release-owner signature/production trust",
    "SBOM production generation",
    "clean-host air-gap deploy log",
    "runtime-envelope readback match",
)

# These limits apply to both file input and direct Python values.  They are
# deliberately conservative: this verifier is intended to run on a clean,
# offline host before any release contents are admitted.
RELEASE_ENVELOPE_MAX_BYTES = 4 * 1024 * 1024
RELEASE_ENVELOPE_MAX_JSON_DEPTH = 32
RELEASE_ENVELOPE_MAX_TOTAL_NODES = 100_000
RELEASE_ENVELOPE_MAX_STRING_LENGTH = 4_096
RELEASE_ENVELOPE_MAX_TOTAL_STRING_BYTES = RELEASE_ENVELOPE_MAX_BYTES
RELEASE_ENVELOPE_MAX_CONTAINER_ITEMS = 4_096
RELEASE_ENVELOPE_MAX_BUNDLE_FILES = RELEASE_ENVELOPE_MAX_CONTAINER_ITEMS
RELEASE_ENVELOPE_MAX_BUNDLE_FILE_BYTES = 4 * 1024 * 1024 * 1024
RELEASE_ENVELOPE_MAX_INTEGER = 9_223_372_036_854_775_807
RELEASE_ENVELOPE_MIN_INTEGER = -9_223_372_036_854_775_808

# Convenient aliases follow the names used by the P5 contract and help
# callers avoid depending on an implementation-specific prefix.
MAX_RELEASE_ENVELOPE_BYTES = RELEASE_ENVELOPE_MAX_BYTES
MAX_RELEASE_ENVELOPE_JSON_DEPTH = RELEASE_ENVELOPE_MAX_JSON_DEPTH
MAX_RELEASE_ENVELOPE_TOTAL_NODES = RELEASE_ENVELOPE_MAX_TOTAL_NODES

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_IMAGE_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_CODE_LINE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
_ROOT_FIELDS = frozenset(
    {
        "schema_version",
        "envelope_id",
        "envelope_version",
        "code_line",
        "code_commit",
        "p0_profile",
        "release_owner",
        "bundle_inventory",
        "platform_images",
        "enabled_models",
        "enabled_artifacts",
        "sbom",
        "ca_bundle",
        "deployment_config",
        "topology",
        "features",
        "data_classification_ceiling",
        "startup_posture",
        "generated_at",
        "valid_from",
        "valid_until",
        "content_sha256",
        "signature",
    }
)
_P0_FIELDS = frozenset(
    {
        "profile_id",
        "profile_version",
        "profile_content_sha256",
        "inventory_version",
        "inventory_sha256",
    }
)
_OWNER_FIELDS = frozenset({"owner_id", "key_id"})
_TRUST_FIELDS = frozenset({"schema_version", "trusted_release_owners"})
_TRUST_ENTRY_FIELDS = frozenset({"owner_id", "key_id", "public_key"})
_BUNDLE_ENTRY_FIELDS = frozenset({"path", "sha256", "size"})
_CA_FIELDS = frozenset({"path", "sha256"})
_CONFIG_FIELDS = frozenset({"sha256"})
_TOPOLOGY_FIELDS = frozenset({"topology_id", "sha256"})
_FEATURE_FIELDS = frozenset({"enabled", "disabled"})
_SBOM_ENTRY_FIELDS = frozenset({"component_id", "digest"})


class ReleaseEnvelopeError(ValueError):
    """Safe, stable release-envelope verification error.

    Public errors contain only a machine-readable code.  In particular, they
    never include a path, file content, exception cause, key material, or
    operating-system detail.
    """

    def __init__(self, code: str) -> None:
        if not code or any(character.isspace() for character in code):
            code = RELEASE_ENVELOPE_ERROR_INVALID_ENVELOPE
        super().__init__(code)
        self.code = code


class _DuplicateJSONKey(Exception):
    pass


class _InvalidJSON(Exception):
    pass


class _ResourceLimit(Exception):
    pass


class _InvalidEnvelope(Exception):
    pass


class _InvalidSignature(Exception):
    pass


class _InvalidTrust(Exception):
    pass


class _InvalidP0(Exception):
    pass


class _InvalidBundle(Exception):
    pass


class _NotEffective(Exception):
    pass


class _SnapshotCleanupError(Exception):
    """A verifier-owned snapshot could not be safely released."""

    pass


def _fail(error_type: type[Exception] = _InvalidEnvelope) -> None:
    raise error_type


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJSONKey
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> Any:
    del value
    raise _InvalidJSON


def _directory_open_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _path_components(path: str | Path) -> tuple[bool, tuple[str, ...]]:
    """Return lexical path components without resolving any symlink.

    All untrusted filesystem paths used by this verifier are opened one
    component at a time below an already-open directory descriptor.  This
    prevents a symlink in a *parent* component from redirecting an authority
    or bundle read after validation.
    """

    try:
        raw = os.fsdecode(os.fspath(path))
    except (TypeError, ValueError):
        raise _InvalidBundle from None
    if not raw or "\x00" in raw:
        raise _InvalidBundle
    absolute = os.path.isabs(raw)
    components = tuple(component for component in raw.split(os.sep) if component)
    if not components or any(component in {".", ".."} for component in components):
        raise _InvalidBundle
    return absolute, components


def _open_directory_path(path: str | Path) -> int:
    """Open a directory path while rejecting links in every component."""

    absolute, components = _path_components(path)
    fd: int | None = None
    transferred = False
    try:
        fd = os.open("/" if absolute else ".", _directory_open_flags())
        for component in components:
            next_fd = os.open(component, _directory_open_flags(), dir_fd=fd)
            os.close(fd)
            fd = next_fd
        transferred = True
        return fd
    except OSError:
        raise _InvalidBundle from None
    finally:
        if fd is not None and not transferred:
            with contextlib.suppress(OSError):
                os.close(fd)


def _open_parent_and_final(path: str | Path, final_flags: int) -> int:
    """Open one regular-file final component below no-follow parents."""

    absolute, components = _path_components(path)
    if not components:
        raise _InvalidBundle
    directory_fd: int | None = None
    try:
        directory_fd = os.open("/" if absolute else ".", _directory_open_flags())
        for component in components[:-1]:
            next_fd = os.open(component, _directory_open_flags(), dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        return os.open(components[-1], final_flags, dir_fd=directory_fd)
    except OSError:
        raise _InvalidBundle from None
    finally:
        if directory_fd is not None:
            with contextlib.suppress(OSError):
                os.close(directory_fd)


def _check_encoded_depth(raw: bytes) -> None:
    depth = 0
    in_string = False
    escaped = False
    for byte in raw:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:
                escaped = True
            elif byte == 0x22:
                in_string = False
            continue
        if byte == 0x22:
            in_string = True
        elif byte in (0x7B, 0x5B):
            depth += 1
            if depth > RELEASE_ENVELOPE_MAX_JSON_DEPTH:
                raise _ResourceLimit
        elif byte in (0x7D, 0x5D):
            depth -= 1
            if depth < 0:
                raise _InvalidJSON


def _check_value_limits(value: Any) -> None:
    """Bound arbitrary direct values without recursively walking attacker data."""

    stack: list[tuple[Any, int]] = [(value, 0)]
    total_nodes = 0
    total_string_bytes = 0
    while stack:
        item, parent_depth = stack.pop()
        total_nodes += 1
        if total_nodes > RELEASE_ENVELOPE_MAX_TOTAL_NODES:
            raise _ResourceLimit
        if isinstance(item, str):
            if len(item) > RELEASE_ENVELOPE_MAX_STRING_LENGTH:
                raise _ResourceLimit
            total_string_bytes += len(item.encode("utf-8"))
            if total_string_bytes > RELEASE_ENVELOPE_MAX_TOTAL_STRING_BYTES:
                raise _ResourceLimit
            continue
        if isinstance(item, float):
            if not math.isfinite(item):
                raise _InvalidEnvelope
            continue
        if isinstance(item, int) and not isinstance(item, bool):
            if not RELEASE_ENVELOPE_MIN_INTEGER <= item <= RELEASE_ENVELOPE_MAX_INTEGER:
                raise _ResourceLimit
            continue
        if isinstance(item, Mapping):
            depth = parent_depth + 1
            if depth > RELEASE_ENVELOPE_MAX_JSON_DEPTH:
                raise _ResourceLimit
            if len(item) > RELEASE_ENVELOPE_MAX_CONTAINER_ITEMS:
                raise _ResourceLimit
            for key, child in item.items():
                stack.append((key, depth))
                stack.append((child, depth))
            continue
        if isinstance(item, (list, tuple)):
            depth = parent_depth + 1
            if depth > RELEASE_ENVELOPE_MAX_JSON_DEPTH:
                raise _ResourceLimit
            if len(item) > RELEASE_ENVELOPE_MAX_CONTAINER_ITEMS:
                raise _ResourceLimit
            stack.extend((child, depth) for child in item)
            continue
        # ``None`` and bool are the only remaining JSON scalar values.  Other
        # Python objects are rejected by canonical JSON below.
        if item is not None and not isinstance(item, bool):
            raise _InvalidEnvelope


def _read_json_file(path: str | Path) -> dict[str, Any]:
    """Read one JSON object through an identity-checked no-follow descriptor.

    A path check followed by ``Path.read_bytes`` is not sufficient here: an
    attacker can swap a symlink or rewrite the file between those operations.
    Opening with ``O_NOFOLLOW`` and comparing descriptor metadata before and
    after a bounded read keeps the bytes and the identity tied together.
    """

    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    fd: int | None = None
    try:
        fd = _open_parent_and_final(path, flags)
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size > RELEASE_ENVELOPE_MAX_BYTES
        ):
            raise _InvalidJSON if not stat.S_ISREG(before.st_mode) else _ResourceLimit
        raw_buffer = bytearray()
        while len(raw_buffer) <= RELEASE_ENVELOPE_MAX_BYTES:
            chunk = os.read(fd, min(1024 * 1024, RELEASE_ENVELOPE_MAX_BYTES + 1 - len(raw_buffer)))
            if not chunk:
                break
            raw_buffer.extend(chunk)
        if len(raw_buffer) > RELEASE_ENVELOPE_MAX_BYTES:
            raise _ResourceLimit
        after = os.fstat(fd)
        identity_fields = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_nlink",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if any(getattr(before, field) != getattr(after, field) for field in identity_fields):
            raise _InvalidJSON
        raw = bytes(raw_buffer)
    except (_InvalidJSON, _ResourceLimit):
        raise
    except (OSError, MemoryError):
        raise _InvalidJSON
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
    if len(raw) > RELEASE_ENVELOPE_MAX_BYTES or raw.startswith(b"\xef\xbb\xbf"):
        raise _ResourceLimit if len(raw) > RELEASE_ENVELOPE_MAX_BYTES else _InvalidJSON
    try:
        _check_encoded_depth(raw)
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicates,
            parse_constant=_reject_nonfinite,
        )
    except (_DuplicateJSONKey,):
        raise
    except _ResourceLimit:
        raise
    except (_InvalidJSON, UnicodeError, json.JSONDecodeError, RecursionError, MemoryError):
        raise _InvalidJSON
    if not isinstance(value, dict):
        raise _InvalidJSON
    try:
        _check_value_limits(value)
    except (_ResourceLimit, _InvalidEnvelope):
        raise
    except (UnicodeError, RecursionError, MemoryError):
        raise _ResourceLimit
    return value


def read_release_envelope(path: str | Path) -> dict[str, Any]:
    """Read one strict release-envelope JSON file with stable safe errors."""

    try:
        return _read_json_file(path)
    except _DuplicateJSONKey:
        raise ReleaseEnvelopeError(RELEASE_ENVELOPE_ERROR_DUPLICATE_JSON_KEY) from None
    except _ResourceLimit:
        raise ReleaseEnvelopeError(RELEASE_ENVELOPE_ERROR_RESOURCE_LIMIT) from None
    except _InvalidJSON:
        raise ReleaseEnvelopeError(RELEASE_ENVELOPE_ERROR_INVALID_JSON) from None
    except Exception:
        raise ReleaseEnvelopeError(RELEASE_ENVELOPE_ERROR_INVALID_JSON) from None


def canonical_release_json(value: Any) -> bytes:
    """Return deterministic strict JSON bytes for release hashes/signatures."""

    try:
        _check_value_limits(value)
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (_ResourceLimit,):
        raise ReleaseEnvelopeError(RELEASE_ENVELOPE_ERROR_RESOURCE_LIMIT) from None
    except Exception:
        raise ReleaseEnvelopeError(RELEASE_ENVELOPE_ERROR_INVALID_ENVELOPE) from None


def release_envelope_content_sha256(envelope: Mapping[str, Any]) -> str:
    """Hash envelope content excluding the self-referential hash/signature."""

    if not isinstance(envelope, Mapping):
        raise ReleaseEnvelopeError(RELEASE_ENVELOPE_ERROR_INVALID_ENVELOPE)
    unsigned = dict(envelope)
    unsigned.pop("content_sha256", None)
    unsigned.pop("signature", None)
    try:
        return hashlib.sha256(canonical_release_json(unsigned)).hexdigest()
    except ReleaseEnvelopeError:
        raise
    except Exception:
        raise ReleaseEnvelopeError(RELEASE_ENVELOPE_ERROR_INVALID_ENVELOPE) from None


def _exact(value: Any, expected: frozenset[str]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise _InvalidEnvelope
    return value


def _string(value: Any, *, max_length: int = 512) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > max_length:
        raise _InvalidEnvelope
    return value


def _identifier(value: Any) -> str:
    if not isinstance(value, str) or _IDENTIFIER_RE.fullmatch(value) is None:
        raise _InvalidEnvelope
    return value


def _code_line(value: Any) -> str:
    if not isinstance(value, str) or _CODE_LINE_RE.fullmatch(value) is None:
        raise _InvalidEnvelope
    return value


def _hash(value: Any) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise _InvalidEnvelope
    return value


def _image_digest(value: Any) -> str:
    if not isinstance(value, str) or _IMAGE_DIGEST_RE.fullmatch(value) is None:
        raise _InvalidEnvelope
    return value


def _unique_strings(
    value: Any, *, allow_empty: bool = False, max_items: int = 4_096
) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > max_items or (not allow_empty and not value):
        raise _InvalidEnvelope
    items = tuple(_string(item, max_length=256) for item in value)
    if len(items) != len(set(items)) or list(items) != sorted(items):
        raise _InvalidEnvelope
    return items


def _rfc3339(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise _InvalidEnvelope
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise _InvalidEnvelope from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _InvalidEnvelope
    return parsed.astimezone(timezone.utc)


def _canonical_relative_path(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise _InvalidBundle
    if value != value.strip() or "\\" in value or "\x00" in value:
        raise _InvalidBundle
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise _InvalidBundle
    path = PurePosixPath(value)
    if path.is_absolute() or value.startswith("//"):
        raise _InvalidBundle
    if not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise _InvalidBundle
    if path.as_posix() != value:
        raise _InvalidBundle
    # Reject a Windows drive-looking component even on POSIX hosts.
    if re.fullmatch(r"[A-Za-z]:", path.parts[0]):
        raise _InvalidBundle
    return value


def _bundle_inventory(value: Any) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, list) or not value or len(value) > RELEASE_ENVELOPE_MAX_BUNDLE_FILES:
        raise _InvalidBundle
    seen: set[str] = set()
    normalized: list[Mapping[str, Any]] = []
    for item in value:
        try:
            entry = _exact(item, _BUNDLE_ENTRY_FIELDS)
            path = _canonical_relative_path(entry["path"])
            digest = _hash(entry["sha256"])
            size = entry["size"]
            if (
                isinstance(size, bool)
                or not isinstance(size, int)
                or not 0 <= size <= RELEASE_ENVELOPE_MAX_BUNDLE_FILE_BYTES
            ):
                raise _InvalidBundle
        except _InvalidEnvelope:
            raise _InvalidBundle from None
        if path in seen:
            raise _InvalidBundle
        seen.add(path)
        normalized.append({"path": path, "sha256": digest, "size": size})
    if [entry["path"] for entry in normalized] != sorted(seen):
        raise _InvalidBundle
    return tuple(normalized)


def _digest_map(value: Any, *, allow_empty: bool = True) -> Mapping[str, str]:
    if not isinstance(value, Mapping) or len(value) > RELEASE_ENVELOPE_MAX_CONTAINER_ITEMS:
        raise _InvalidEnvelope
    if not allow_empty and not value:
        raise _InvalidEnvelope
    result: dict[str, str] = {}
    for key, digest in value.items():
        _identifier(key)
        result[key] = _image_digest(digest)
    if list(result) != sorted(result):
        raise _InvalidEnvelope
    return MappingProxyType(result)


def _validate_p0(value: Any) -> Mapping[str, str]:
    item = _exact(value, _P0_FIELDS)
    result = {
        "profile_id": _identifier(item["profile_id"]),
        "profile_version": _string(item["profile_version"], max_length=128),
        "profile_content_sha256": _hash(item["profile_content_sha256"]),
        "inventory_version": _string(item["inventory_version"], max_length=128),
        "inventory_sha256": _hash(item["inventory_sha256"]),
    }
    return MappingProxyType(result)


def _validate_release_owner(value: Any) -> Mapping[str, str]:
    item = _exact(value, _OWNER_FIELDS)
    return MappingProxyType(
        {"owner_id": _identifier(item["owner_id"]), "key_id": _identifier(item["key_id"])}
    )


def _validate_sbom(value: Any) -> tuple[Mapping[str, str], ...]:
    if (
        not isinstance(value, list)
        or not value
        or len(value) > RELEASE_ENVELOPE_MAX_CONTAINER_ITEMS
    ):
        raise _InvalidEnvelope
    seen: set[str] = set()
    result: list[Mapping[str, str]] = []
    for item in value:
        entry = _exact(item, _SBOM_ENTRY_FIELDS)
        component = _identifier(entry["component_id"])
        digest = _image_digest(entry["digest"])
        if component in seen:
            raise _InvalidEnvelope
        seen.add(component)
        result.append({"component_id": component, "digest": digest})
    if [entry["component_id"] for entry in result] != sorted(seen):
        raise _InvalidEnvelope
    return tuple(result)


def _validate_envelope(value: Any) -> dict[str, Any]:
    _check_value_limits(value)
    root = _exact(value, _ROOT_FIELDS)
    if root["schema_version"] != RELEASE_ENVELOPE_SCHEMA:
        raise _InvalidEnvelope
    envelope_id = _identifier(root["envelope_id"])
    envelope_version = _string(root["envelope_version"], max_length=128)
    code_line = _code_line(root["code_line"])
    code_commit = root["code_commit"]
    if not isinstance(code_commit, str) or _COMMIT_RE.fullmatch(code_commit) is None:
        raise _InvalidEnvelope
    p0 = _validate_p0(root["p0_profile"])
    owner = _validate_release_owner(root["release_owner"])
    inventory = _bundle_inventory(root["bundle_inventory"])
    platform_images = _digest_map(root["platform_images"], allow_empty=False)
    enabled_models = _digest_map(root["enabled_models"])
    enabled_artifacts = _digest_map(root["enabled_artifacts"])
    sbom = _validate_sbom(root["sbom"])
    ca = _exact(root["ca_bundle"], _CA_FIELDS)
    ca_path = _canonical_relative_path(ca["path"])
    ca_hash = _hash(ca["sha256"])
    config = _exact(root["deployment_config"], _CONFIG_FIELDS)
    config_hash = _hash(config["sha256"])
    topology = _exact(root["topology"], _TOPOLOGY_FIELDS)
    topology_id = _identifier(topology["topology_id"])
    topology_hash = _hash(topology["sha256"])
    features = _exact(root["features"], _FEATURE_FIELDS)
    enabled_features = _unique_strings(features["enabled"])
    disabled_features = _unique_strings(features["disabled"], allow_empty=True)
    if set(enabled_features) & set(disabled_features):
        raise _InvalidEnvelope
    ceiling = root["data_classification_ceiling"]
    # Avoid importing anila-contracts solely for this small wire contract.
    if ceiling not in {"無機密", "營業秘密", "機密", "極機密", "絕對機密"}:
        raise _InvalidEnvelope
    posture = _unique_strings(root["startup_posture"])
    generated_at_text = root["generated_at"]
    valid_from_text = root["valid_from"]
    valid_until_text = root["valid_until"]
    generated_at = _rfc3339(generated_at_text)
    valid_from = _rfc3339(valid_from_text)
    valid_until = _rfc3339(valid_until_text)
    if valid_until <= valid_from or generated_at > valid_from:
        raise _InvalidEnvelope
    if (valid_until - valid_from).total_seconds() > 365 * 24 * 60 * 60:
        raise _InvalidEnvelope
    content_hash = _hash(root["content_sha256"])
    signature = root["signature"]
    if not isinstance(signature, str) or not signature:
        raise _InvalidSignature
    return {
        "schema_version": RELEASE_ENVELOPE_SCHEMA,
        "envelope_id": envelope_id,
        "envelope_version": envelope_version,
        "code_line": code_line,
        "code_commit": code_commit,
        "p0_profile": dict(p0),
        "release_owner": dict(owner),
        "bundle_inventory": [dict(entry) for entry in inventory],
        "platform_images": dict(platform_images),
        "enabled_models": dict(enabled_models),
        "enabled_artifacts": dict(enabled_artifacts),
        "sbom": [dict(entry) for entry in sbom],
        "ca_bundle": {"path": ca_path, "sha256": ca_hash},
        "deployment_config": {"sha256": config_hash},
        "topology": {"topology_id": topology_id, "sha256": topology_hash},
        "features": {"enabled": list(enabled_features), "disabled": list(disabled_features)},
        "data_classification_ceiling": ceiling,
        "startup_posture": list(posture),
        "generated_at": generated_at_text,
        "valid_from": valid_from_text,
        "valid_until": valid_until_text,
        "content_sha256": content_hash,
        "signature": signature,
    }


def _validate_trust_store(value: Any) -> Mapping[tuple[str, str], str]:
    if not isinstance(value, Mapping) or set(value) != _TRUST_FIELDS:
        raise _InvalidTrust
    if value["schema_version"] != RELEASE_TRUST_STORE_SCHEMA:
        raise _InvalidTrust
    entries = value["trusted_release_owners"]
    if (
        not isinstance(entries, list)
        or not entries
        or len(entries) > RELEASE_ENVELOPE_MAX_CONTAINER_ITEMS
    ):
        raise _InvalidTrust
    result: dict[tuple[str, str], str] = {}
    owner_ids: set[str] = set()
    key_ids: set[str] = set()
    fingerprints: set[str] = set()
    for raw in entries:
        try:
            entry = _exact(raw, _TRUST_ENTRY_FIELDS)
            owner_id = _identifier(entry["owner_id"])
            key_id = _identifier(entry["key_id"])
            pem = entry["public_key"]
            if not isinstance(pem, str) or not pem.strip() or len(pem) > 16_384:
                raise _InvalidTrust
            key = serialization.load_pem_public_key(pem.encode("ascii"))
            if not isinstance(key, Ed25519PublicKey):
                raise _InvalidTrust
            fingerprint = hashlib.sha256(
                key.public_bytes(
                    serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
                )
            ).hexdigest()
        except _InvalidTrust:
            raise
        except Exception:
            raise _InvalidTrust from None
        if (
            (owner_id, key_id) in result
            or owner_id in owner_ids
            or key_id in key_ids
            or fingerprint in fingerprints
        ):
            raise _InvalidTrust
        result[(owner_id, key_id)] = pem
        owner_ids.add(owner_id)
        key_ids.add(key_id)
        fingerprints.add(fingerprint)
    return MappingProxyType(result)


def _verify_release_signature(envelope: Mapping[str, Any], trust_store: Mapping[str, Any]) -> None:
    trusted = _validate_trust_store(trust_store)
    owner = envelope["release_owner"]
    key_text = trusted.get((owner["owner_id"], owner["key_id"]))
    if key_text is None:
        raise _InvalidTrust
    expected_hash = release_envelope_content_sha256(envelope)
    if envelope["content_sha256"] != expected_hash:
        raise _InvalidSignature
    try:
        key = serialization.load_pem_public_key(key_text.encode("ascii"))
        signature = base64.b64decode(envelope["signature"], validate=True)
        if len(signature) != 64:
            raise ValueError
        # The signature is over the canonical content plus its content hash;
        # including the signature field itself would create an impossible
        # self-referential signing cycle.
        payload = dict(envelope)
        payload.pop("signature", None)
        key.verify(signature, canonical_release_json(payload))
    except Exception:
        raise _InvalidSignature from None


def _stat_identity(info: os.stat_result) -> tuple[int, int, int, int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_uid,
    )


def _open_bundle_file(root_fd: int, relative: str) -> int:
    """Open a bundle file beneath an anchored directory fd, never following links."""

    directory_flags = _directory_open_flags()
    file_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    current_fd: int | None = None
    try:
        current_fd = os.dup(root_fd)
        parts = PurePosixPath(relative).parts
        for component in parts[:-1]:
            next_fd = os.open(component, directory_flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        return os.open(parts[-1], file_flags, dir_fd=current_fd)
    except OSError:
        raise _InvalidBundle from None
    finally:
        if current_fd is not None:
            with contextlib.suppress(OSError):
                os.close(current_fd)


def _open_relative_directory(root_fd: int, components: Sequence[str]) -> int:
    """Open a directory below an already-anchored root descriptor."""

    current_fd: int | None = None
    transferred = False
    try:
        current_fd = os.dup(root_fd)
        for component in components:
            next_fd = os.open(component, _directory_open_flags(), dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        transferred = True
        return current_fd
    except OSError:
        raise _InvalidBundle from None
    finally:
        if current_fd is not None and not transferred:
            with contextlib.suppress(OSError):
                os.close(current_fd)


def _discover_bundle(
    root_fd: int,
    *,
    snapshot_owner_uid: int | None = None,
) -> dict[str, os.stat_result]:
    """Descriptor-walk a closed bundle set without pathname re-resolution."""

    discovered: dict[str, os.stat_result] = {}
    seen_inodes: set[tuple[int, int]] = set()

    def visit(directory_fd: int, prefix: tuple[str, ...]) -> None:
        try:
            names = sorted(os.listdir(directory_fd))
        except OSError:
            raise _InvalidBundle from None
        for name in names:
            if not name or name in {".", ".."}:
                raise _InvalidBundle
            try:
                info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except OSError:
                raise _InvalidBundle from None
            if stat.S_ISLNK(info.st_mode):
                raise _InvalidBundle
            relative_parts = (*prefix, name)
            relative = _canonical_relative_path("/".join(relative_parts))
            if stat.S_ISDIR(info.st_mode):
                if snapshot_owner_uid is not None and (
                    info.st_uid != snapshot_owner_uid or stat.S_IMODE(info.st_mode) != 0o700
                ):
                    raise _InvalidBundle
                child_fd: int | None = None
                try:
                    child_fd = os.open(name, _directory_open_flags(), dir_fd=directory_fd)
                    child_info = os.fstat(child_fd)
                    if _stat_identity(child_info) != _stat_identity(info):
                        raise _InvalidBundle
                    visit(child_fd, relative_parts)
                except OSError:
                    raise _InvalidBundle from None
                finally:
                    if child_fd is not None:
                        with contextlib.suppress(OSError):
                            os.close(child_fd)
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise _InvalidBundle
            if snapshot_owner_uid is not None and (
                info.st_uid != snapshot_owner_uid or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise _InvalidBundle
            inode = (info.st_dev, info.st_ino)
            if inode in seen_inodes or len(discovered) >= RELEASE_ENVELOPE_MAX_BUNDLE_FILES:
                raise (
                    _ResourceLimit
                    if len(discovered) >= RELEASE_ENVELOPE_MAX_BUNDLE_FILES
                    else _InvalidBundle
                )
            if relative in discovered:
                raise _InvalidBundle
            discovered[relative] = info
            seen_inodes.add(inode)

    visit(root_fd, ())
    return discovered


def _prepare_snapshot_directories(
    snapshot_fd: int, inventory: Sequence[Mapping[str, Any]], owner_uid: int
) -> None:
    """Create the exact private directory tree before copying any file."""

    created: set[tuple[str, ...]] = set()
    for entry in inventory:
        components = PurePosixPath(entry["path"]).parts[:-1]
        current_fd: int | None = None
        prefix: tuple[str, ...] = ()
        try:
            current_fd = os.dup(snapshot_fd)
            for component in components:
                prefix = (*prefix, component)
                if prefix not in created:
                    try:
                        os.mkdir(component, 0o700, dir_fd=current_fd)
                    except FileExistsError:
                        # The private root began empty; an unexpected entry is
                        # an injection, not a reusable deployment directory.
                        raise _InvalidBundle from None
                    created.add(prefix)
                    # ``mkdir(mode=...)`` is still subject to umask.  Repair
                    # the verifier-owned final component before opening it.
                    os.chmod(component, 0o700, dir_fd=current_fd, follow_symlinks=False)
                next_fd = os.open(component, _directory_open_flags(), dir_fd=current_fd)
                os.close(current_fd)
                current_fd = next_fd
                info = os.fstat(current_fd)
                if not stat.S_ISDIR(info.st_mode) or info.st_uid != owner_uid:
                    raise _InvalidBundle
                os.fchmod(current_fd, 0o700)
                if stat.S_IMODE(os.fstat(current_fd).st_mode) != 0o700:
                    raise _InvalidBundle
        except OSError:
            raise _InvalidBundle from None
        finally:
            if current_fd is not None:
                with contextlib.suppress(OSError):
                    os.close(current_fd)


def _write_snapshot_file(
    snapshot_fd: int,
    relative: str,
    source_fd: int,
    expected: Mapping[str, Any],
    owner_uid: int,
) -> None:
    """Hash a stable source descriptor and copy it to a private fd tree."""

    source_before = os.fstat(source_fd)
    if (
        not stat.S_ISREG(source_before.st_mode)
        or source_before.st_nlink != 1
        or source_before.st_size > RELEASE_ENVELOPE_MAX_BUNDLE_FILE_BYTES
        or source_before.st_size != expected["size"]
    ):
        raise _InvalidBundle
    parts = PurePosixPath(relative).parts
    parent_fd: int | None = None
    snapshot_file_fd: int | None = None
    try:
        parent_fd = _open_relative_directory(snapshot_fd, parts[:-1])
        snapshot_flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        snapshot_file_fd = os.open(parts[-1], snapshot_flags, 0o600, dir_fd=parent_fd)
        os.fchmod(snapshot_file_fd, 0o600)
        digest = hashlib.sha256()
        read_size = 0
        while read_size <= expected["size"]:
            chunk = os.read(source_fd, min(1024 * 1024, expected["size"] + 1 - read_size))
            if not chunk:
                break
            read_size += len(chunk)
            if read_size > expected["size"]:
                raise _InvalidBundle
            digest.update(chunk)
            written = 0
            while written < len(chunk):
                written += os.write(snapshot_file_fd, chunk[written:])
        source_after = os.fstat(source_fd)
        snapshot_after = os.fstat(snapshot_file_fd)
        if (
            _stat_identity(source_before) != _stat_identity(source_after)
            or not stat.S_ISREG(snapshot_after.st_mode)
            or snapshot_after.st_uid != owner_uid
            or stat.S_IMODE(snapshot_after.st_mode) != 0o600
            or snapshot_after.st_nlink != 1
            or snapshot_after.st_size != expected["size"]
            or read_size != expected["size"]
            or digest.hexdigest() != expected["sha256"]
        ):
            raise _InvalidBundle
    except (OSError, MemoryError):
        raise _InvalidBundle from None
    finally:
        if snapshot_file_fd is not None:
            with contextlib.suppress(OSError):
                os.close(snapshot_file_fd)
        if parent_fd is not None:
            with contextlib.suppress(OSError):
                os.close(parent_fd)


def _verify_snapshot_hashes(
    snapshot_fd: int, expected: Mapping[str, Mapping[str, Any]], owner_uid: int
) -> None:
    """Re-hash the private copy after its closed-set metadata scan."""

    for relative in sorted(expected):
        expected_entry = expected[relative]
        file_fd: int | None = None
        try:
            file_fd = _open_bundle_file(snapshot_fd, relative)
            before = os.fstat(file_fd)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != owner_uid
                or stat.S_IMODE(before.st_mode) != 0o600
                or before.st_nlink != 1
                or before.st_size != expected_entry["size"]
            ):
                raise _InvalidBundle
            digest = hashlib.sha256()
            total = 0
            while total <= expected_entry["size"]:
                chunk = os.read(file_fd, min(1024 * 1024, expected_entry["size"] + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                if total > expected_entry["size"]:
                    raise _InvalidBundle
                digest.update(chunk)
            after = os.fstat(file_fd)
            if (
                _stat_identity(before) != _stat_identity(after)
                or total != expected_entry["size"]
                or digest.hexdigest() != expected_entry["sha256"]
            ):
                raise _InvalidBundle
        except (OSError, MemoryError):
            raise _InvalidBundle from None
        finally:
            if file_fd is not None:
                with contextlib.suppress(OSError):
                    os.close(file_fd)


def _snapshot_identity_matches(
    info: os.stat_result, identity: tuple[int, int, int], owner_uid: int
) -> bool:
    return (
        (info.st_dev, info.st_ino, info.st_uid) == identity
        and stat.S_ISDIR(info.st_mode)
        and stat.S_IMODE(info.st_mode) == 0o700
        and info.st_uid == owner_uid
    )


def _remove_snapshot_tree(directory_fd: int, owner_uid: int) -> None:
    """Remove only entries below an already-verified directory descriptor."""

    try:
        names = os.listdir(directory_fd)
    except OSError:
        raise _SnapshotCleanupError from None
    for name in names:
        try:
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if info.st_uid != owner_uid:
                raise _SnapshotCleanupError
            if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                child_fd = os.open(name, _directory_open_flags(), dir_fd=directory_fd)
                try:
                    _remove_snapshot_tree(child_fd, owner_uid)
                finally:
                    os.close(child_fd)
                os.rmdir(name, dir_fd=directory_fd)
            else:
                # Unlinking a symlink/special file is safe because this is
                # anchored below our private descriptor and never follows it.
                os.unlink(name, dir_fd=directory_fd)
        except _SnapshotCleanupError:
            raise
        except OSError:
            raise _SnapshotCleanupError from None


def _close_snapshot_descriptors(
    parent_fd: int,
    root_fd: int,
    name: str,
    identity: tuple[int, int, int],
    owner_uid: int,
) -> None:
    """Safely delete exactly the temporary directory we originally opened."""

    try:
        if not _snapshot_identity_matches(os.fstat(root_fd), identity, owner_uid):
            raise _SnapshotCleanupError
        entry = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not _snapshot_identity_matches(entry, identity, owner_uid):
            # The name was replaced.  Never delete a successor directory.
            raise _SnapshotCleanupError
        _remove_snapshot_tree(root_fd, owner_uid)
        entry_after = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not _snapshot_identity_matches(entry_after, identity, owner_uid):
            raise _SnapshotCleanupError
        os.rmdir(name, dir_fd=parent_fd)
    except _SnapshotCleanupError:
        raise
    except OSError:
        raise _SnapshotCleanupError from None
    finally:
        with contextlib.suppress(OSError):
            os.close(root_fd)
        with contextlib.suppress(OSError):
            os.close(parent_fd)


def _finalize_snapshot(
    parent_fd: int,
    root_fd: int,
    name: str,
    identity: tuple[int, int, int],
    owner_uid: int,
) -> None:
    # Finalizers cannot report failures.  Explicit ``close`` remains strict;
    # this best-effort fallback exists solely for a forgotten consumer lease.
    with contextlib.suppress(_SnapshotCleanupError):
        _close_snapshot_descriptors(parent_fd, root_fd, name, identity, owner_uid)


class _SnapshotLease:
    """Private ownership token; callers cannot manufacture a cleanup path."""

    __slots__ = (
        "_path",
        "_parent_fd",
        "_root_fd",
        "_identity",
        "_owner_uid",
        "_closed",
        "_finalizer",
        "__weakref__",
    )

    def __init__(self, path: Path, parent_fd: int, root_fd: int) -> None:
        info = os.fstat(root_fd)
        owner_uid = os.geteuid()
        identity = (info.st_dev, info.st_ino, info.st_uid)
        if not _snapshot_identity_matches(info, identity, owner_uid):
            raise _SnapshotCleanupError
        self._path = path
        self._parent_fd = parent_fd
        self._root_fd = root_fd
        self._identity = identity
        self._owner_uid = owner_uid
        self._closed = False
        self._finalizer = weakref.finalize(
            self,
            _finalize_snapshot,
            parent_fd,
            root_fd,
            path.name,
            self._identity,
            owner_uid,
        )

    @property
    def path(self) -> Path:
        if self._closed:
            raise _SnapshotCleanupError
        try:
            if not _snapshot_identity_matches(
                os.fstat(self._root_fd), self._identity, self._owner_uid
            ):
                raise _SnapshotCleanupError
            entry = os.stat(self._path.name, dir_fd=self._parent_fd, follow_symlinks=False)
            if not _snapshot_identity_matches(entry, self._identity, self._owner_uid):
                raise _SnapshotCleanupError
        except OSError:
            raise _SnapshotCleanupError from None
        return self._path

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._finalizer.detach()
        _close_snapshot_descriptors(
            self._parent_fd,
            self._root_fd,
            self._path.name,
            self._identity,
            self._owner_uid,
        )


def _create_snapshot_lease() -> _SnapshotLease:
    parent_path = Path(tempfile.gettempdir())
    parent_fd: int | None = None
    root_fd: int | None = None
    name: str | None = None
    created = False
    try:
        parent_fd = _open_directory_path(parent_path)
        # Build the temporary directory through an anchored parent descriptor,
        # rather than creating it by pathname and opening it later.  The token
        # is cryptographically random and an existing name is never reused.
        for _ in range(128):
            candidate = f"anila-release-envelope-{secrets.token_hex(24)}"
            try:
                os.mkdir(candidate, 0o700, dir_fd=parent_fd)
            except FileExistsError:
                continue
            name = candidate
            created = True
            break
        if name is None:
            raise _SnapshotCleanupError
        # ``mkdir(mode=...)`` is subject to umask.  Repair the new final
        # component through the same descriptor before opening it.
        os.chmod(name, 0o700, dir_fd=parent_fd, follow_symlinks=False)
        root_fd = os.open(name, _directory_open_flags(), dir_fd=parent_fd)
        os.fchmod(root_fd, 0o700)
        info = os.fstat(root_fd)
        if not _snapshot_identity_matches(
            info, (info.st_dev, info.st_ino, info.st_uid), os.geteuid()
        ):
            raise _SnapshotCleanupError
        lease = _SnapshotLease(parent_path / name, parent_fd, root_fd)
        parent_fd = None
        root_fd = None
        return lease
    except (OSError, _SnapshotCleanupError):
        if root_fd is not None and parent_fd is not None and name is not None:
            info = os.fstat(root_fd)
            with contextlib.suppress(_SnapshotCleanupError):
                _close_snapshot_descriptors(
                    parent_fd,
                    root_fd,
                    name,
                    (info.st_dev, info.st_ino, info.st_uid),
                    os.geteuid(),
                )
            root_fd = None
            parent_fd = None
        # Before a descriptor exists the directory is still freshly-created,
        # empty, and addressed through the anchored parent.  Remove only that
        # exact new entry; a replacement causes ``rmdir`` to fail closed.
        if root_fd is None and created and parent_fd is not None and name is not None:
            with contextlib.suppress(OSError):
                os.rmdir(name, dir_fd=parent_fd)
        if root_fd is not None:
            with contextlib.suppress(OSError):
                os.close(root_fd)
        if parent_fd is not None:
            with contextlib.suppress(OSError):
                os.close(parent_fd)
        raise _InvalidBundle from None


def _verify_bundle(
    root_value: str | Path, inventory: Sequence[Mapping[str, Any]], ca_path: str, ca_hash: str
) -> _SnapshotLease:
    expected = {entry["path"]: entry for entry in inventory}
    if ca_path not in expected or expected[ca_path]["sha256"] != ca_hash:
        raise _InvalidBundle
    root_fd: int | None = None
    lease: _SnapshotLease | None = None
    transferred = False
    try:
        root_fd = _open_directory_path(root_value)
        root_info = os.fstat(root_fd)
        if not stat.S_ISDIR(root_info.st_mode):
            raise _InvalidBundle
        source_inventory = _discover_bundle(root_fd)
        if set(source_inventory) != set(expected):
            raise _InvalidBundle
        lease = _create_snapshot_lease()
        snapshot_path = lease.path
        del snapshot_path  # Proves lease ownership before any copy begins.
        _prepare_snapshot_directories(lease._root_fd, inventory, lease._owner_uid)
        for relative in sorted(expected):
            source_fd = _open_bundle_file(root_fd, relative)
            try:
                if _stat_identity(os.fstat(source_fd)) != _stat_identity(
                    source_inventory[relative]
                ):
                    raise _InvalidBundle
                _write_snapshot_file(
                    lease._root_fd,
                    relative,
                    source_fd,
                    expected[relative],
                    lease._owner_uid,
                )
            finally:
                with contextlib.suppress(OSError):
                    os.close(source_fd)
        # Re-scan the private copy, not merely the mutable source tree.  This
        # catches injected entries, stale permissions, and file substitutions
        # before the lease becomes visible to a deployment consumer.
        snapshot_info = os.fstat(lease._root_fd)
        if not _snapshot_identity_matches(snapshot_info, lease._identity, lease._owner_uid):
            raise _InvalidBundle
        snapshot_files = _discover_bundle(lease._root_fd, snapshot_owner_uid=lease._owner_uid)
        if set(snapshot_files) != set(expected):
            raise _InvalidBundle
        for relative, entry in expected.items():
            info = snapshot_files[relative]
            if info.st_size != entry["size"]:
                raise _InvalidBundle
        _verify_snapshot_hashes(lease._root_fd, expected, lease._owner_uid)
        # The source must also still be the same closed set at transfer time.
        source_final = _discover_bundle(root_fd)
        if set(source_final) != set(expected) or any(
            _stat_identity(source_final[relative]) != _stat_identity(source_inventory[relative])
            for relative in expected
        ):
            raise _InvalidBundle
        transferred = True
        return lease
    except _ResourceLimit:
        raise
    except (_InvalidBundle, _SnapshotCleanupError):
        raise _InvalidBundle from None
    except (OSError, ValueError, MemoryError):
        raise _InvalidBundle from None
    finally:
        if root_fd is not None:
            with contextlib.suppress(OSError):
                os.close(root_fd)
        if lease is not None and not transferred:
            # Never use path-based ``rmtree`` here.  A cleanup failure is a
            # verifier failure, not a silently orphaned sensitive snapshot.
            try:
                lease.close()
            except _SnapshotCleanupError:
                raise _InvalidBundle from None


def _verify_p0(
    p0_profile: Mapping[str, Any] | str | Path,
    p0_trust_store: Mapping[str, Any] | str | Path,
    p0_inventory: Mapping[str, Any] | str | Path,
    *,
    now: datetime,
) -> VerifiedProductionAcceptanceProfile:
    try:
        if isinstance(p0_profile, (str, Path)) or isinstance(p0_trust_store, (str, Path)):
            if not isinstance(p0_profile, (str, Path)) or not isinstance(
                p0_trust_store, (str, Path)
            ):
                raise _InvalidP0
            if not isinstance(p0_inventory, (str, Path)):
                raise _InvalidP0
            # The existing P0 path helper uses an unchecked Path.read_bytes
            # loader.  Read every external JSON object through this module's
            # descriptor-bound reader first, then invoke the mapping verifier.
            profile = _read_json_file(p0_profile)
            trust_store = _read_json_file(p0_trust_store)
            inventory = _read_json_file(p0_inventory)
            version = inventory.get("version", inventory.get("inventory_version"))
            if not isinstance(version, str) or not version.strip():
                raise _InvalidP0
            return verify_production_acceptance_profile(
                profile,
                trust_store,
                expected_inventory_sha256=hashlib.sha256(
                    canonical_release_json(inventory)
                ).hexdigest(),
                expected_inventory_version=version,
                now=now,
            )
        if not isinstance(p0_inventory, Mapping):
            raise _InvalidP0
        inventory = dict(p0_inventory)
        version = inventory.get("version", inventory.get("inventory_version"))
        if not isinstance(version, str):
            raise _InvalidP0
        return verify_production_acceptance_profile(
            p0_profile,
            p0_trust_store,
            expected_inventory_sha256=hashlib.sha256(canonical_release_json(inventory)).hexdigest(),
            expected_inventory_version=version,
            now=now,
        )
    except (_DuplicateJSONKey, _ResourceLimit):
        raise
    except (ReleaseEnvelopeError, _InvalidP0):
        raise _InvalidP0 from None
    except Exception:
        raise _InvalidP0 from None


def _normalise_now(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise _NotEffective
    return current.astimezone(timezone.utc)


def _verify_internal(
    envelope: Mapping[str, Any],
    release_trust_store: Mapping[str, Any],
    bundle_root: str | Path,
    p0_profile: Mapping[str, Any] | str | Path,
    p0_trust_store: Mapping[str, Any] | str | Path,
    p0_inventory: Mapping[str, Any] | str | Path,
    *,
    now: datetime | None,
) -> "VerifiedReleaseEnvelope":
    current = _normalise_now(now)
    normal = _validate_envelope(envelope)
    valid_from = _rfc3339(normal["valid_from"])
    valid_until = _rfc3339(normal["valid_until"])
    if not valid_from <= current < valid_until:
        raise _NotEffective
    _verify_release_signature(normal, release_trust_store)
    p0 = _verify_p0(p0_profile, p0_trust_store, p0_inventory, now=current)
    p0_ref = normal["p0_profile"]
    if (
        p0_ref["profile_id"] != p0.profile_id
        or p0_ref["profile_version"] != p0.profile_version
        or p0_ref["profile_content_sha256"] != p0.profile_content_sha256
        or p0_ref["inventory_version"] != p0.inventory_version
        or p0_ref["inventory_sha256"] != p0.inventory_sha256
    ):
        raise _InvalidP0
    profile = p0.profile
    p0_topology = profile["production_topology"]
    expected_topology_hash = hashlib.sha256(canonical_release_json(p0_topology)).hexdigest()
    if (
        normal["topology"]["topology_id"] != p0_topology["topology_id"]
        or normal["topology"]["sha256"] != expected_topology_hash
    ):
        raise _InvalidP0
    p0_enabled = sorted(profile["enabled_features"])
    p0_disabled = sorted(profile["disabled_features"])
    if normal["features"]["enabled"] != p0_enabled or normal["features"]["disabled"] != p0_disabled:
        raise _InvalidP0
    if normal["data_classification_ceiling"] != p0.data_classification_ceiling:
        raise _InvalidP0
    snapshot = _verify_bundle(
        bundle_root,
        normal["bundle_inventory"],
        normal["ca_bundle"]["path"],
        normal["ca_bundle"]["sha256"],
    )
    return VerifiedReleaseEnvelope(
        envelope_id=normal["envelope_id"],
        envelope_version=normal["envelope_version"],
        code_line=normal["code_line"],
        code_commit=normal["code_commit"],
        p0_profile_id=p0.profile_id,
        p0_profile_version=p0.profile_version,
        p0_profile_content_sha256=p0.profile_content_sha256,
        release_owner_id=normal["release_owner"]["owner_id"],
        release_owner_key_id=normal["release_owner"]["key_id"],
        content_sha256=normal["content_sha256"],
        generated_at=_rfc3339(normal["generated_at"]),
        valid_from=valid_from,
        valid_until=valid_until,
        _snapshot=snapshot,
        envelope=_freeze(normal),
    )


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class VerifiedReleaseEnvelope:
    """Immutable, cryptographically checked P4 result (never acceptance)."""

    envelope_id: str
    envelope_version: str
    code_line: str
    code_commit: str
    p0_profile_id: str
    p0_profile_version: str
    p0_profile_content_sha256: str
    release_owner_id: str
    release_owner_key_id: str
    content_sha256: str
    generated_at: datetime
    valid_from: datetime
    valid_until: datetime
    envelope: Mapping[str, Any]
    _snapshot: _SnapshotLease = field(repr=False, compare=False)

    @property
    def status(self) -> str:
        return RELEASE_ENVELOPE_STATUS

    @property
    def acceptance_status(self) -> str:
        return RELEASE_ENVELOPE_ACCEPTANCE_STATUS

    @property
    def gate6_pass(self) -> bool:
        return False

    @property
    def production_approval(self) -> bool:
        return False

    @property
    def bundle_snapshot_root(self) -> Path:
        """Return the open verifier-owned consumer snapshot.

        The path is a convenience view only; its cleanup authority stays in a
        private descriptor lease, so callers cannot turn a forged path into a
        recursive delete target.  Access after :meth:`close` fails closed.
        """

        try:
            return self._snapshot.path
        except _SnapshotCleanupError:
            raise ReleaseEnvelopeError(RELEASE_ENVELOPE_ERROR_INVALID_BUNDLE) from None

    @property
    def snapshot_root(self) -> Path:
        """Short alias for the verifier-owned consumer snapshot."""

        return self.bundle_snapshot_root

    def close(self) -> None:
        """Release the private bundle snapshot exactly once.

        Cleanup is descriptor-anchored and verifies the captured device,
        inode, owner and mode before deletion.  If a path replacement or a
        filesystem failure is observed, it fails closed rather than deleting a
        successor directory or hiding an orphaned sensitive copy.
        """

        try:
            self._snapshot.close()
        except _SnapshotCleanupError:
            raise ReleaseEnvelopeError(RELEASE_ENVELOPE_ERROR_INVALID_BUNDLE) from None

    def cleanup_snapshot(self) -> None:
        """Compatibility alias for :meth:`close`."""

        self.close()

    def __enter__(self) -> "VerifiedReleaseEnvelope":
        # Validate the lease before handing it to a deployment consumer.
        _ = self.bundle_snapshot_root
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        del exc_type, exc, traceback
        self.close()
        return False

    def as_dict(self) -> dict[str, Any]:
        """Return the deterministic report shape without signature/key material."""

        return {
            "schema_version": RELEASE_VERIFICATION_SCHEMA,
            "status": RELEASE_ENVELOPE_STATUS,
            "acceptance_status": RELEASE_ENVELOPE_ACCEPTANCE_STATUS,
            "gate6_pass": False,
            "production_approval": False,
            "evidence_class": RELEASE_ENVELOPE_EVIDENCE_CLASS,
            "envelope_id": self.envelope_id,
            "envelope_version": self.envelope_version,
            "code_line": self.code_line,
            "code_commit": self.code_commit,
            "p0_profile_id": self.p0_profile_id,
            "p0_profile_version": self.p0_profile_version,
            "p0_profile_content_sha256": self.p0_profile_content_sha256,
            "release_owner_id": self.release_owner_id,
            "release_owner_key_id": self.release_owner_key_id,
            "content_sha256": self.content_sha256,
            "missing_external_p4_evidence": list(RELEASE_ENVELOPE_MISSING_EXTERNAL_EVIDENCE),
        }

    def serialized_report(self) -> bytes:
        """Serialize :meth:`as_dict` with the canonical JSON encoder."""

        return canonical_release_json(self.as_dict())

    def to_json(self) -> bytes:
        """Compatibility alias for :meth:`serialized_report`."""

        return self.serialized_report()


def verify_release_envelope(
    envelope: Mapping[str, Any],
    release_trust_store: Mapping[str, Any],
    bundle_root: str | Path,
    p0_profile: Mapping[str, Any] | str | Path,
    p0_trust_store: Mapping[str, Any] | str | Path,
    p0_inventory: Mapping[str, Any] | str | Path,
    *,
    now: datetime | None = None,
) -> VerifiedReleaseEnvelope:
    """Verify direct JSON values against external trust and P0 evidence."""

    try:
        if not isinstance(envelope, Mapping) or not isinstance(release_trust_store, Mapping):
            raise _InvalidEnvelope
        return _verify_internal(
            envelope,
            release_trust_store,
            bundle_root,
            p0_profile,
            p0_trust_store,
            p0_inventory,
            now=now,
        )
    except _DuplicateJSONKey:
        raise ReleaseEnvelopeError(RELEASE_ENVELOPE_ERROR_DUPLICATE_JSON_KEY) from None
    except _ResourceLimit:
        raise ReleaseEnvelopeError(RELEASE_ENVELOPE_ERROR_RESOURCE_LIMIT) from None
    except _InvalidSignature:
        raise ReleaseEnvelopeError(RELEASE_ENVELOPE_ERROR_INVALID_SIGNATURE) from None
    except _InvalidTrust:
        raise ReleaseEnvelopeError(RELEASE_ENVELOPE_ERROR_INVALID_TRUST) from None
    except _InvalidP0:
        raise ReleaseEnvelopeError(RELEASE_ENVELOPE_ERROR_INVALID_P0) from None
    except _InvalidBundle:
        raise ReleaseEnvelopeError(RELEASE_ENVELOPE_ERROR_INVALID_BUNDLE) from None
    except _NotEffective:
        raise ReleaseEnvelopeError(RELEASE_ENVELOPE_ERROR_NOT_EFFECTIVE) from None
    except ReleaseEnvelopeError:
        raise
    except Exception:
        raise ReleaseEnvelopeError(RELEASE_ENVELOPE_ERROR_INVALID_ENVELOPE) from None


def verify_signed_release_envelope(
    envelope_path: str | Path,
    release_trust_store_path: str | Path,
    bundle_root: str | Path,
    p0_profile_path: str | Path,
    p0_trust_store_path: str | Path,
    p0_inventory_path: str | Path,
    *,
    now: datetime | None = None,
) -> VerifiedReleaseEnvelope:
    """Read and verify one envelope using only external trust/evidence paths."""

    try:
        envelope = _read_json_file(envelope_path)
        trust = _read_json_file(release_trust_store_path)
    except _DuplicateJSONKey:
        raise ReleaseEnvelopeError(RELEASE_ENVELOPE_ERROR_DUPLICATE_JSON_KEY) from None
    except _ResourceLimit:
        raise ReleaseEnvelopeError(RELEASE_ENVELOPE_ERROR_RESOURCE_LIMIT) from None
    except _InvalidJSON:
        raise ReleaseEnvelopeError(RELEASE_ENVELOPE_ERROR_INVALID_JSON) from None
    except Exception:
        raise ReleaseEnvelopeError(RELEASE_ENVELOPE_ERROR_INVALID_JSON) from None
    try:
        return _verify_internal(
            envelope,
            trust,
            bundle_root,
            p0_profile_path,
            p0_trust_store_path,
            p0_inventory_path,
            now=now,
        )
    except _DuplicateJSONKey:
        raise ReleaseEnvelopeError(RELEASE_ENVELOPE_ERROR_DUPLICATE_JSON_KEY) from None
    except _ResourceLimit:
        raise ReleaseEnvelopeError(RELEASE_ENVELOPE_ERROR_RESOURCE_LIMIT) from None
    except _InvalidSignature:
        raise ReleaseEnvelopeError(RELEASE_ENVELOPE_ERROR_INVALID_SIGNATURE) from None
    except _InvalidTrust:
        raise ReleaseEnvelopeError(RELEASE_ENVELOPE_ERROR_INVALID_TRUST) from None
    except _InvalidP0:
        raise ReleaseEnvelopeError(RELEASE_ENVELOPE_ERROR_INVALID_P0) from None
    except _InvalidBundle:
        raise ReleaseEnvelopeError(RELEASE_ENVELOPE_ERROR_INVALID_BUNDLE) from None
    except _NotEffective:
        raise ReleaseEnvelopeError(RELEASE_ENVELOPE_ERROR_NOT_EFFECTIVE) from None
    except ReleaseEnvelopeError:
        raise
    except Exception:
        raise ReleaseEnvelopeError(RELEASE_ENVELOPE_ERROR_INVALID_ENVELOPE) from None


verify_release_envelope_file = verify_signed_release_envelope


__all__ = [
    "MAX_RELEASE_ENVELOPE_BYTES",
    "MAX_RELEASE_ENVELOPE_JSON_DEPTH",
    "MAX_RELEASE_ENVELOPE_TOTAL_NODES",
    "RELEASE_ENVELOPE_ACCEPTANCE_STATUS",
    "RELEASE_ENVELOPE_EVIDENCE_CLASS",
    "RELEASE_ENVELOPE_ERROR_DUPLICATE_JSON_KEY",
    "RELEASE_ENVELOPE_ERROR_INVALID_BUNDLE",
    "RELEASE_ENVELOPE_ERROR_INVALID_ENVELOPE",
    "RELEASE_ENVELOPE_ERROR_INVALID_JSON",
    "RELEASE_ENVELOPE_ERROR_INVALID_P0",
    "RELEASE_ENVELOPE_ERROR_INVALID_SIGNATURE",
    "RELEASE_ENVELOPE_ERROR_INVALID_TRUST",
    "RELEASE_ENVELOPE_ERROR_NOT_EFFECTIVE",
    "RELEASE_ENVELOPE_ERROR_RESOURCE_LIMIT",
    "RELEASE_ENVELOPE_MAX_BUNDLE_FILE_BYTES",
    "RELEASE_ENVELOPE_MAX_BUNDLE_FILES",
    "RELEASE_ENVELOPE_MAX_BYTES",
    "RELEASE_ENVELOPE_MAX_CONTAINER_ITEMS",
    "RELEASE_ENVELOPE_MAX_JSON_DEPTH",
    "RELEASE_ENVELOPE_MAX_STRING_LENGTH",
    "RELEASE_ENVELOPE_MAX_TOTAL_NODES",
    "RELEASE_ENVELOPE_MISSING_EXTERNAL_EVIDENCE",
    "RELEASE_ENVELOPE_SCHEMA",
    "RELEASE_ENVELOPE_STATUS",
    "RELEASE_ENVELOPE_MIN_INTEGER",
    "RELEASE_ENVELOPE_MAX_INTEGER",
    "RELEASE_TRUST_STORE_SCHEMA",
    "RELEASE_VERIFICATION_SCHEMA",
    "ReleaseEnvelopeError",
    "VerifiedReleaseEnvelope",
    "canonical_release_json",
    "read_release_envelope",
    "release_envelope_content_sha256",
    "verify_release_envelope",
    "verify_release_envelope_file",
    "verify_signed_release_envelope",
]
