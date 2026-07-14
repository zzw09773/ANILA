#!/usr/bin/env python3
"""Fail-closed verifier for the production backup coverage profile.

The verifier parses the authoritative production Compose YAML and inventories
every declared named volume plus every bind mount, including read-only config
and key references.  The checked-in
profile must account for each discovered persistence surface exactly once.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml


SCHEMA_VERSION = "anila.production-backup-profile.v1"
PROFILE_ID = "anila-platform-production"
COMPOSE_FILE = "infra/compose/platform.yml"
COMPOSE_PROJECT = "anila-platform"

_TOP_LEVEL_KEYS = {
    "schema_version",
    "profile_id",
    "scope",
    "policy",
    "automation",
    "surfaces",
}
_SCOPE_KEYS = {"compose_file", "compose_project"}
_POLICY = {
    "default_disposition": "reject",
    "unknown_surface": "reject",
    "missing_surface": "reject",
    "duplicate_surface": "reject",
    "invalid_surface": "reject",
}
_SURFACE_KEYS = {
    "id",
    "source",
    "disposition",
    "data_classification",
    "owner",
    "reason",
    "acceptance_gate",
    "backup_method",
    "restore_order",
}
_SOURCE_KEYS = {"kind", "name", "mounts"}
_MOUNT_KEYS = {"service", "target", "read_only"}
_AUTOMATION_KEYS = {
    "bundle_schema",
    "manifest_schema",
    "encryption",
    "signature",
    "off_host",
    "retention",
    "alert",
    "consistency",
    "external_references",
}
_ENCRYPTION_KEYS = {"tool", "recipients_file_env", "identity_file_env"}
_SIGNATURE_KEYS = {"tool", "signing_key_env", "verification_key_env"}
_OFF_HOST_KEYS = {"directory_env", "readback"}
_RETENTION_KEYS = {"keep_env", "default_keep"}
_ALERT_KEYS = {"hook_env", "protocol"}
_CONSISTENCY_KEYS = {
    "mode",
    "writer_services",
    "database_service",
    "redis_service",
}
_REFERENCE_KEYS = {"reference_env", "fingerprint_env"}
_REQUIRED_EXTERNAL_REFERENCES = {
    "csp-jwt-key-reference",
    "ingress-tls-key-reference",
    "gitlab-configuration",
    "n8n-encryption-key",
    "gitlab-secrets",
    "signed-release",
}
_DISPOSITIONS = {"required", "derivable", "excluded"}
_CLASSIFICATIONS = {"無機密", "營業秘密", "機密", "極機密"}
_BACKUP_METHODS = {
    "pg_dump",
    "filesystem_archive",
    "volume_archive",
    "redis_snapshot",
    "service_native",
    "key_management_reference",
    "rebuild_from_required_sources",
    "none",
}
_PLACEHOLDERS = {"", "n/a", "na", "none", "null", "tbd", "todo", "unknown"}
_GATE_RE = re.compile(r"^Gate [0-9]+ [A-Z][A-Za-z0-9.-]*$")

_MANDATORY_REQUIRED_NAMED = {
    "csp-pgdata",
    "redis-data",
    "csp-attachments",
    "anila-studio-artifacts",
    "n8n_data",
    "gitlab_config",
    "gitlab_logs",
    "gitlab_data",
}
_MANDATORY_REQUIRED_BINDS = {
    "../../share/uploads/ingestion",
    "../../share/uploads",
    "../../share/uploads/flux",
    "../../share/pki",
    "../../share/static",
    (
        "${ANILA_SECRETS_DIR:?ANILA_SECRETS_DIR must be an absolute path "
        "outside the repo}"
    ),
    (
        "${ANILA_TLS_CERTS_DIR:?ANILA_TLS_CERTS_DIR must be an absolute path "
        "outside the repo}"
    ),
    (
        "${ANILA_STATE_DIR:?ANILA_STATE_DIR must be an absolute path outside "
        "the repo}/source-snapshots"
    ),
    (
        "${ANILA_STATE_DIR:?ANILA_STATE_DIR must be an absolute path outside "
        "the repo}/artifact-blobs"
    ),
}


class BackupProfileError(RuntimeError):
    """The profile or Compose inventory violates the fail-closed contract."""


class _UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects duplicate mapping keys."""


def _construct_unique_mapping(
    loader: _UniqueKeyLoader, node: yaml.nodes.MappingNode, deep: bool = False,
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise BackupProfileError(f"duplicate YAML key: {key!r}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BackupProfileError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupProfileError(f"cannot parse backup profile: {exc}") from exc
    if not isinstance(value, dict):
        raise BackupProfileError("backup profile root must be an object")
    return value


def _load_compose(path: Path) -> dict[str, Any]:
    try:
        value = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)
    except BackupProfileError:
        raise
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise BackupProfileError(f"cannot parse production Compose YAML: {exc}") from exc
    if not isinstance(value, dict):
        raise BackupProfileError("production Compose root must be a mapping")
    return value


def _require_exact_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise BackupProfileError(
            f"{label} keys invalid; missing={missing}, extra={extra}"
        )


def _parse_short_mount(raw: str) -> tuple[str, str, bool]:
    if not raw or raw != raw.strip():
        raise BackupProfileError("Compose volume short syntax must be non-empty and trimmed")
    read_only = False
    without_mode = raw
    if raw.endswith(":ro") or raw.endswith(":rw"):
        without_mode, mode = raw.rsplit(":", 1)
        read_only = mode == "ro"
    if ":" not in without_mode:
        raise BackupProfileError(f"anonymous or target-only volume is not inventoryable: {raw}")
    source, target = without_mode.rsplit(":", 1)
    if not source or not target.startswith("/"):
        raise BackupProfileError(f"invalid Compose volume mount: {raw}")
    return source, target, read_only


def _parse_mount(
    raw: Any, declared_named: set[str], service: str,
) -> tuple[tuple[str, str], dict[str, Any]] | None:
    if isinstance(raw, str):
        source, target, read_only = _parse_short_mount(raw)
        kind = "named_volume" if source in declared_named else "bind"
    elif isinstance(raw, dict):
        mount_type = raw.get("type")
        source = raw.get("source")
        target = raw.get("target")
        read_only = raw.get("read_only", False)
        if mount_type not in {"volume", "bind"}:
            raise BackupProfileError(
                f"unsupported Compose mount type for {service}: {mount_type!r}"
            )
        if not isinstance(source, str) or not source:
            raise BackupProfileError(f"Compose mount source missing for {service}")
        if not isinstance(target, str) or not target.startswith("/"):
            raise BackupProfileError(f"Compose mount target invalid for {service}")
        if not isinstance(read_only, bool):
            raise BackupProfileError(f"Compose mount read_only must be boolean for {service}")
        kind = "named_volume" if mount_type == "volume" else "bind"
        if kind == "named_volume" and source not in declared_named:
            raise BackupProfileError(
                f"undeclared named volume cannot be covered safely: {source}"
            )
    else:
        raise BackupProfileError(f"invalid Compose mount entry for {service}")

    if kind == "named_volume" and source not in declared_named:
        raise BackupProfileError(f"undeclared named volume: {source}")
    occurrence = {"service": service, "target": target, "read_only": read_only}
    return (kind, source), occurrence


def discover_persistent_surfaces(
    compose: dict[str, Any],
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    volumes = compose.get("volumes")
    services = compose.get("services")
    if not isinstance(volumes, dict) or not volumes:
        raise BackupProfileError("production Compose volumes must be a non-empty mapping")
    if not isinstance(services, dict) or not services:
        raise BackupProfileError("production Compose services must be a non-empty mapping")
    if any(not isinstance(name, str) or not name for name in volumes):
        raise BackupProfileError("production Compose volume names must be non-empty strings")
    declared_named = set(volumes)
    discovered: dict[tuple[str, str], list[dict[str, Any]]] = {
        ("named_volume", name): [] for name in declared_named
    }
    seen_occurrences: set[tuple[str, str, str, str]] = set()

    for service, config in services.items():
        if not isinstance(service, str) or not service or not isinstance(config, dict):
            raise BackupProfileError("production Compose service entries must be mappings")
        mounts = config.get("volumes", [])
        if mounts is None:
            mounts = []
        if not isinstance(mounts, list):
            raise BackupProfileError(f"Compose service volumes must be a list: {service}")
        for raw in mounts:
            parsed = _parse_mount(raw, declared_named, service)
            if parsed is None:
                continue
            key, occurrence = parsed
            occurrence_key = (
                key[0], key[1], occurrence["service"], occurrence["target"]
            )
            if occurrence_key in seen_occurrences:
                raise BackupProfileError(
                    f"duplicate persistent mount occurrence: {occurrence_key}"
                )
            seen_occurrences.add(occurrence_key)
            discovered.setdefault(key, []).append(occurrence)

    unused = sorted(name for kind, name in discovered if kind == "named_volume" and not discovered[(kind, name)])
    if unused:
        raise BackupProfileError(f"declared named volumes are not mounted: {unused}")
    for occurrences in discovered.values():
        occurrences.sort(key=lambda item: (item["service"], item["target"], item["read_only"]))
    return discovered


def _meaningful_string(value: Any, field: str, *, minimum: int = 3) -> str:
    if not isinstance(value, str) or value != value.strip() or len(value) < minimum:
        raise BackupProfileError(f"{field} must be a meaningful trimmed string")
    if value.lower() in _PLACEHOLDERS:
        raise BackupProfileError(f"{field} cannot be a fail-open placeholder")
    return value


def _validate_automation(raw: Any) -> None:
    if not isinstance(raw, dict):
        raise BackupProfileError("profile automation must be an object")
    _require_exact_keys(raw, _AUTOMATION_KEYS, "automation")
    if raw["bundle_schema"] != "anila.production-backup-envelope.v1":
        raise BackupProfileError("unsupported backup envelope schema")
    if raw["manifest_schema"] != "anila.production-backup-manifest.v1":
        raise BackupProfileError("unsupported backup manifest schema")

    encryption = raw["encryption"]
    signature = raw["signature"]
    off_host = raw["off_host"]
    retention = raw["retention"]
    alert = raw["alert"]
    consistency = raw["consistency"]
    references = raw["external_references"]
    for value, keys, label in (
        (encryption, _ENCRYPTION_KEYS, "automation.encryption"),
        (signature, _SIGNATURE_KEYS, "automation.signature"),
        (off_host, _OFF_HOST_KEYS, "automation.off_host"),
        (retention, _RETENTION_KEYS, "automation.retention"),
        (alert, _ALERT_KEYS, "automation.alert"),
        (consistency, _CONSISTENCY_KEYS, "automation.consistency"),
    ):
        if not isinstance(value, dict):
            raise BackupProfileError(f"{label} must be an object")
        _require_exact_keys(value, keys, label)

    expected_scalars = {
        ("encryption", "tool"): "age",
        ("signature", "tool"): "openssl-dgst-sha256",
        ("off_host", "readback"): "sha256",
        ("alert", "protocol"): "json-stdin-v1",
        ("consistency", "mode"): "quiesced-writers-v1",
    }
    for (section, key), expected in expected_scalars.items():
        if raw[section][key] != expected:
            raise BackupProfileError(f"automation.{section}.{key} must be {expected}")
    for section, key in (
        ("encryption", "recipients_file_env"),
        ("encryption", "identity_file_env"),
        ("signature", "signing_key_env"),
        ("signature", "verification_key_env"),
        ("off_host", "directory_env"),
        ("retention", "keep_env"),
        ("alert", "hook_env"),
        ("consistency", "database_service"),
        ("consistency", "redis_service"),
    ):
        _meaningful_string(raw[section][key], f"automation.{section}.{key}")
    keep = retention["default_keep"]
    if isinstance(keep, bool) or not isinstance(keep, int) or not 1 <= keep <= 3650:
        raise BackupProfileError("automation retention default_keep must be 1..3650")
    writers = consistency["writer_services"]
    if (
        not isinstance(writers, list)
        or not writers
        or len(writers) != len(set(writers))
        or any(not isinstance(item, str) or not item for item in writers)
    ):
        raise BackupProfileError("automation writer_services must be unique names")
    if not isinstance(references, dict) or set(references) != _REQUIRED_EXTERNAL_REFERENCES:
        raise BackupProfileError("automation external reference inventory drifted")
    for name, reference in references.items():
        if not isinstance(reference, dict):
            raise BackupProfileError(f"external reference must be object: {name}")
        _require_exact_keys(reference, _REFERENCE_KEYS, f"external reference {name}")
        for key in _REFERENCE_KEYS:
            value = _meaningful_string(reference[key], f"external reference {name}.{key}")
            if not re.fullmatch(r"ANILA_BACKUP_[A-Z0-9_]+", value):
                raise BackupProfileError(f"external reference env is invalid: {name}.{key}")


def _validate_surface(
    raw: Any,
) -> tuple[tuple[str, str], dict[str, Any]]:
    if not isinstance(raw, dict):
        raise BackupProfileError("each backup surface must be an object")
    _require_exact_keys(raw, _SURFACE_KEYS, "surface")
    surface_id = _meaningful_string(raw["id"], "surface.id")
    source = raw["source"]
    if not isinstance(source, dict):
        raise BackupProfileError(f"surface.source must be an object: {surface_id}")
    _require_exact_keys(source, _SOURCE_KEYS, f"surface.source[{surface_id}]")
    kind = source["kind"]
    name = source["name"]
    if kind not in {"named_volume", "bind"}:
        raise BackupProfileError(f"surface kind is invalid: {surface_id}")
    if not isinstance(name, str) or not name or name != name.strip():
        raise BackupProfileError(f"surface source name is invalid: {surface_id}")
    mounts = source["mounts"]
    if not isinstance(mounts, list) or not mounts:
        raise BackupProfileError(f"surface mounts must be non-empty: {surface_id}")
    normalized_mounts: list[dict[str, Any]] = []
    seen_mounts: set[tuple[str, str, bool]] = set()
    for mount in mounts:
        if not isinstance(mount, dict):
            raise BackupProfileError(f"surface mount must be an object: {surface_id}")
        _require_exact_keys(mount, _MOUNT_KEYS, f"surface.mount[{surface_id}]")
        service = _meaningful_string(mount["service"], "mount.service", minimum=1)
        target = mount["target"]
        read_only = mount["read_only"]
        if not isinstance(target, str) or not target.startswith("/"):
            raise BackupProfileError(f"mount target is invalid: {surface_id}")
        if not isinstance(read_only, bool):
            raise BackupProfileError(f"mount read_only must be boolean: {surface_id}")
        identity = (service, target, read_only)
        if identity in seen_mounts:
            raise BackupProfileError(f"duplicate mount in profile: {surface_id}")
        seen_mounts.add(identity)
        normalized_mounts.append(
            {"service": service, "target": target, "read_only": read_only}
        )
    normalized_mounts.sort(
        key=lambda item: (item["service"], item["target"], item["read_only"])
    )

    disposition = raw["disposition"]
    classification = raw["data_classification"]
    method = raw["backup_method"]
    if disposition not in _DISPOSITIONS:
        raise BackupProfileError(f"invalid disposition for {surface_id}: {disposition!r}")
    if classification not in _CLASSIFICATIONS:
        raise BackupProfileError(f"invalid classification for {surface_id}")
    if method not in _BACKUP_METHODS:
        raise BackupProfileError(f"invalid backup method for {surface_id}: {method!r}")
    if disposition == "required" and method in {"none", "rebuild_from_required_sources"}:
        raise BackupProfileError(f"required surface has non-backup method: {surface_id}")
    if disposition == "derivable" and method != "rebuild_from_required_sources":
        raise BackupProfileError(f"derivable surface must declare rebuild method: {surface_id}")
    if disposition == "excluded" and method != "none":
        raise BackupProfileError(f"excluded surface must use backup_method=none: {surface_id}")

    _meaningful_string(raw["owner"], f"surface.owner[{surface_id}]")
    _meaningful_string(raw["reason"], f"surface.reason[{surface_id}]", minimum=12)
    gate = _meaningful_string(
        raw["acceptance_gate"], f"surface.acceptance_gate[{surface_id}]"
    )
    if not _GATE_RE.fullmatch(gate):
        raise BackupProfileError(f"acceptance gate is invalid: {surface_id}")
    order = raw["restore_order"]
    if isinstance(order, bool) or not isinstance(order, int) or not 1 <= order <= 9999:
        raise BackupProfileError(f"restore_order must be an integer 1..9999: {surface_id}")

    normalized = dict(raw)
    normalized["source"] = {"kind": kind, "name": name, "mounts": normalized_mounts}
    return (kind, name), normalized


def verify_profile(
    *, profile_path: Path, compose_path: Path,
) -> dict[str, int]:
    profile = _load_json(profile_path)
    compose = _load_compose(compose_path)
    _require_exact_keys(profile, _TOP_LEVEL_KEYS, "profile")
    if profile["schema_version"] != SCHEMA_VERSION:
        raise BackupProfileError("unsupported production backup profile schema")
    if profile["profile_id"] != PROFILE_ID:
        raise BackupProfileError("unexpected production backup profile id")
    scope = profile["scope"]
    if not isinstance(scope, dict):
        raise BackupProfileError("profile scope must be an object")
    _require_exact_keys(scope, _SCOPE_KEYS, "scope")
    if scope != {"compose_file": COMPOSE_FILE, "compose_project": COMPOSE_PROJECT}:
        raise BackupProfileError("profile scope does not name the production Compose SSOT")
    if compose.get("name") != COMPOSE_PROJECT:
        raise BackupProfileError("production Compose project name drifted")
    policy = profile["policy"]
    if not isinstance(policy, dict):
        raise BackupProfileError("profile policy must be an object")
    _require_exact_keys(policy, set(_POLICY), "policy")
    if policy != _POLICY:
        raise BackupProfileError("backup profile policy must remain fail-closed")
    _validate_automation(profile["automation"])

    discovered = discover_persistent_surfaces(compose)
    raw_surfaces = profile["surfaces"]
    if not isinstance(raw_surfaces, list) or not raw_surfaces:
        raise BackupProfileError("profile surfaces must be a non-empty list")
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    ids: set[str] = set()
    restore_orders: set[int] = set()
    for raw in raw_surfaces:
        key, surface = _validate_surface(raw)
        surface_id = surface["id"]
        if surface_id in ids:
            raise BackupProfileError(f"duplicate surface id: {surface_id}")
        if key in by_key:
            raise BackupProfileError(f"duplicate surface source: {key}")
        if surface["restore_order"] in restore_orders:
            raise BackupProfileError(
                f"duplicate restore_order: {surface['restore_order']}"
            )
        ids.add(surface_id)
        restore_orders.add(surface["restore_order"])
        by_key[key] = surface

    discovered_keys = set(discovered)
    profile_keys = set(by_key)
    missing = sorted(discovered_keys - profile_keys)
    extra = sorted(profile_keys - discovered_keys)
    if missing or extra:
        raise BackupProfileError(
            f"backup surface coverage mismatch; missing={missing}, extra={extra}"
        )
    for key, occurrences in discovered.items():
        if by_key[key]["source"]["mounts"] != occurrences:
            raise BackupProfileError(f"mount inventory drift for surface: {key}")

    mandatory = {
        *(('named_volume', name) for name in _MANDATORY_REQUIRED_NAMED),
        *(('bind', name) for name in _MANDATORY_REQUIRED_BINDS),
    }
    mandatory_missing = sorted(mandatory - discovered_keys)
    if mandatory_missing:
        raise BackupProfileError(
            f"mandatory production persistence surfaces disappeared: {mandatory_missing}"
        )
    not_required = sorted(
        key for key in mandatory if by_key[key]["disposition"] != "required"
    )
    if not_required:
        raise BackupProfileError(
            f"mandatory production persistence surfaces must be required: {not_required}"
        )
    key_reference_ids = {
        surface["id"]
        for surface in by_key.values()
        if surface["backup_method"] == "key_management_reference"
    }
    missing_reference_automation = sorted(
        key_reference_ids - set(profile["automation"]["external_references"])
    )
    if missing_reference_automation:
        raise BackupProfileError(
            "key reference surfaces lack executable reference mapping: "
            f"{missing_reference_automation}"
        )

    counts = {disposition: 0 for disposition in sorted(_DISPOSITIONS)}
    for surface in by_key.values():
        counts[surface["disposition"]] += 1
    counts["total"] = len(by_key)
    return counts


def _parser() -> argparse.ArgumentParser:
    repo_root = Path(__file__).resolve().parents[3]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile",
        type=Path,
        default=repo_root / "infra/deployment/backup/production-backup-profile.v1.json",
    )
    parser.add_argument(
        "--compose",
        type=Path,
        default=repo_root / COMPOSE_FILE,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        counts = verify_profile(profile_path=args.profile, compose_path=args.compose)
    except BackupProfileError as exc:
        print(f"production backup profile rejected: {exc}", file=sys.stderr)
        return 1
    print(
        "production backup profile PASS "
        f"(total={counts['total']}, required={counts['required']}, "
        f"derivable={counts['derivable']}, excluded={counts['excluded']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
