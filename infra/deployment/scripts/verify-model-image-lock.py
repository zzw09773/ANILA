#!/usr/bin/env python3
"""Verify content-ID locks for the optional ANILA model Compose stack.

The model Compose file deliberately keeps human-friendly image references (for
example ``vllm-gemma4:latest``).  An export records the immutable Docker image
ID behind each *enabled* model service in ``MODEL-IMAGE-LOCK.tsv``.  This
module verifies that the lock is a closed set derived from Compose and the
machine-readable inventory, and can optionally read the IDs back from Docker
images and running containers.

This is an image-content check only.  A passing check is not a signed release
envelope, SBOM, CA bundle hash, clean-host deployment, or Gate 6/P4 acceptance.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
DEFAULT_COMPOSE = ROOT / "infra/models/docker-compose.yml"
DEFAULT_INVENTORY = ROOT / "infra/deployment/intranet/model-image-inventory.tsv"
IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
SERVICE_RE = re.compile(r"^  (?P<service>[A-Za-z0-9][A-Za-z0-9_-]*):\s*$")
IMAGE_RE = re.compile(r"^    image:\s*(?P<image>.+?)\s*$")
PROFILES_RE = re.compile(r"^    profiles:\s*(?P<profiles>.+?)\s*$")
INTERPOLATION_RE = re.compile(
    r"^\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?:(?P<operator>:-|:\?)(?P<value>.*))?\}$"
)


class ModelImageLockError(RuntimeError):
    """The model image lock or its Compose/inventory closure is invalid."""


@dataclass(frozen=True)
class InventoryEntry:
    service: str
    image: str
    activation: str


@dataclass(frozen=True)
class ComposeEntry:
    service: str
    image: str
    profiles: tuple[str, ...]


@dataclass(frozen=True)
class LockEntry:
    service: str
    image: str
    image_id: str
    activation: str


def _data_lines(path: Path) -> list[tuple[int, list[str]]]:
    rows: list[tuple[int, list[str]]] = []
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        rows.append((line_number, raw.split("\t")))
    return rows


def read_inventory(path: Path = DEFAULT_INVENTORY) -> dict[str, InventoryEntry]:
    """Read and validate the model inventory's three-column TSV schema."""

    entries: dict[str, InventoryEntry] = {}
    for line_number, fields in _data_lines(path):
        if len(fields) != 3 or any(not field.strip() for field in fields):
            raise ModelImageLockError(
                f"{path}:{line_number}: expected three non-empty TSV fields"
            )
        entry = InventoryEntry(*(field.strip() for field in fields))
        if entry.service in entries:
            raise ModelImageLockError(
                f"{path}:{line_number}: duplicate service {entry.service}"
            )
        if entry.activation != "default" and not entry.activation.startswith(
            "profile:"
        ):
            raise ModelImageLockError(
                f"{path}:{line_number}: activation must be default or profile:<name>"
            )
        if entry.activation.startswith(
            "profile:"
        ) and not entry.activation.removeprefix("profile:"):
            raise ModelImageLockError(f"{path}:{line_number}: empty profile name")
        if "@sha256:" in entry.image:
            raise ModelImageLockError(
                f"{path}:{line_number}: inventory image must be a Compose image ref, not a digest"
            )
        entries[entry.service] = entry
    if not entries:
        raise ModelImageLockError(f"{path}: model inventory is empty")
    return entries


def _parse_profile_value(value: str, path: Path, line_number: int) -> tuple[str, ...]:
    value = value.strip()
    if not value.startswith("[") or not value.endswith("]"):
        raise ModelImageLockError(
            f"{path}:{line_number}: model Compose profiles must be an inline list"
        )
    body = value[1:-1].strip()
    if not body:
        return ()
    profiles: list[str] = []
    for raw_profile in body.split(","):
        profile = raw_profile.strip().strip("'\"")
        if not profile:
            raise ModelImageLockError(
                f"{path}:{line_number}: empty model Compose profile name"
            )
        profiles.append(profile)
    if len(set(profiles)) != len(profiles):
        raise ModelImageLockError(
            f"{path}:{line_number}: duplicate model Compose profile"
        )
    return tuple(profiles)


def _resolve_image_ref(raw_image: str, path: Path, line_number: int) -> str:
    """Resolve only the safe default form used by the model Compose file.

    A shell override is intentionally not accepted here.  The inventory is the
    authoritative image reference and Docker inspection later verifies the tag
    points to the locked content ID.
    """

    image = raw_image.strip().strip("'\"")
    match = INTERPOLATION_RE.fullmatch(image)
    if match is None:
        if "${" in image or "}" in image:
            raise ModelImageLockError(
                f"{path}:{line_number}: unsupported image interpolation {raw_image!r}"
            )
        return image
    operator = match.group("operator")
    default = match.group("value")
    if operator != ":-" or default is None or not default:
        raise ModelImageLockError(
            f"{path}:{line_number}: model image interpolation must provide a fixed default"
        )
    variable = match.group("name")
    ambient = os.environ.get(variable)
    if ambient is not None and ambient != default:
        raise ModelImageLockError(
            f"{path}:{line_number}: ambient {variable} override changes model image ref"
        )
    return default


def read_compose(path: Path = DEFAULT_COMPOSE) -> dict[str, ComposeEntry]:
    """Read the model Compose service/image/profile subset without PyYAML."""

    entries: dict[str, ComposeEntry] = {}
    current_service: str | None = None
    current_image: str | None = None
    current_profiles: tuple[str, ...] = ()
    in_services = False

    def finish() -> None:
        nonlocal current_service, current_image, current_profiles
        if current_service is None:
            return
        if current_image is None:
            raise ModelImageLockError(
                f"{path}: service {current_service} has no image field"
            )
        if current_service in entries:
            raise ModelImageLockError(f"{path}: duplicate service {current_service}")
        entries[current_service] = ComposeEntry(
            current_service, current_image, current_profiles
        )
        current_service = None
        current_image = None
        current_profiles = ()

    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if raw == "services:":
            in_services = True
            continue
        if not in_services:
            continue
        if raw and not raw.startswith((" ", "#")):
            finish()
            break
        service_match = SERVICE_RE.match(raw)
        if service_match:
            finish()
            current_service = service_match.group("service")
            continue
        if current_service is None:
            continue
        image_match = IMAGE_RE.match(raw)
        if image_match:
            if current_image is not None:
                raise ModelImageLockError(
                    f"{path}:{line_number}: duplicate image field for {current_service}"
                )
            current_image = _resolve_image_ref(
                image_match.group("image"), path, line_number
            )
            continue
        profiles_match = PROFILES_RE.match(raw)
        if profiles_match:
            if current_profiles:
                raise ModelImageLockError(
                    f"{path}:{line_number}: duplicate profiles field for {current_service}"
                )
            current_profiles = _parse_profile_value(
                profiles_match.group("profiles"), path, line_number
            )
    finish()
    if not entries:
        raise ModelImageLockError(f"{path}: model Compose services are empty")
    return entries


def verify_compose_inventory(
    compose: dict[str, ComposeEntry], inventory: dict[str, InventoryEntry]
) -> None:
    """Require exact service, image, and activation/profile agreement."""

    compose_services = set(compose)
    inventory_services = set(inventory)
    if compose_services != inventory_services:
        raise ModelImageLockError(
            "model Compose/inventory service set mismatch: "
            f"missing={sorted(inventory_services - compose_services)}, "
            f"extra={sorted(compose_services - inventory_services)}"
        )
    for service, declared in inventory.items():
        actual = compose[service]
        expected_profiles = (
            ()
            if declared.activation == "default"
            else (declared.activation.removeprefix("profile:"),)
        )
        if actual.image != declared.image:
            raise ModelImageLockError(
                f"model image ref drift for {service}: "
                f"inventory={declared.image!r}, compose={actual.image!r}"
            )
        if actual.profiles != expected_profiles:
            raise ModelImageLockError(
                f"model profile drift for {service}: "
                f"inventory={list(expected_profiles)!r}, compose={list(actual.profiles)!r}"
            )


def active_services(
    inventory: dict[str, InventoryEntry],
    *,
    profiles: set[str] | None = None,
    include_optional: bool = False,
) -> set[str]:
    """Return default services plus explicitly enabled model profiles."""

    selected = selected_profiles(
        inventory, profiles=profiles, include_optional=include_optional
    )
    return {
        entry.service
        for entry in inventory.values()
        if entry.activation == "default"
        or entry.activation.removeprefix("profile:") in selected
    }


def selected_profiles(
    inventory: dict[str, InventoryEntry],
    *,
    profiles: set[str] | None = None,
    include_optional: bool = False,
) -> set[str]:
    """Normalize explicitly enabled profiles for Compose and lock operations."""

    selected = set(profiles or ())
    known = {
        entry.activation.removeprefix("profile:")
        for entry in inventory.values()
        if entry.activation.startswith("profile:")
    }
    if include_optional:
        selected = known
    unknown = selected - known
    if unknown:
        raise ModelImageLockError(f"unknown model profile(s): {sorted(unknown)}")
    return selected


def read_lock(
    path: Path,
    inventory: dict[str, InventoryEntry],
    compose: dict[str, ComposeEntry],
    *,
    profiles: set[str] | None = None,
    include_optional: bool = False,
) -> dict[str, LockEntry]:
    """Validate lock rows, metadata, immutable IDs, and active service closure."""

    verify_compose_inventory(compose, inventory)
    entries: dict[str, LockEntry] = {}
    for line_number, fields in _data_lines(path):
        if len(fields) != 4 or any(not field.strip() for field in fields):
            raise ModelImageLockError(
                f"{path}:{line_number}: expected four non-empty TSV fields"
            )
        entry = LockEntry(*(field.strip() for field in fields))
        if entry.service in entries:
            raise ModelImageLockError(
                f"{path}:{line_number}: duplicate service {entry.service}"
            )
        if not IMAGE_ID_RE.fullmatch(entry.image_id):
            raise ModelImageLockError(
                f"{path}:{line_number}: invalid image ID for {entry.service}; "
                "expected sha256:<64 hex>"
            )
        declared = inventory.get(entry.service)
        if declared is None:
            raise ModelImageLockError(
                f"{path}:{line_number}: extra/unknown service {entry.service}"
            )
        if (entry.image, entry.activation) != (declared.image, declared.activation):
            raise ModelImageLockError(
                f"{path}:{line_number}: inventory metadata mismatch for {entry.service}"
            )
        entries[entry.service] = entry

    expected = active_services(
        inventory, profiles=profiles, include_optional=include_optional
    )
    actual = set(entries)
    if actual != expected:
        raise ModelImageLockError(
            "model image lock service set mismatch: "
            f"missing={sorted(expected - actual)}, extra={sorted(actual - expected)}"
        )
    return entries


def _run_utf8(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
    )


def inspect_image_id(image_ref: str) -> str:
    result = _run_utf8(["docker", "image", "inspect", "--format", "{{.Id}}", image_ref])
    actual = result.stdout.strip() if result.returncode == 0 else ""
    if not IMAGE_ID_RE.fullmatch(actual):
        raise ModelImageLockError(
            f"Docker image missing/invalid for {image_ref}: {actual or 'missing'}"
        )
    return actual


def verify_docker_images(lock: dict[str, LockEntry]) -> None:
    """Verify both the mutable tag and the locked immutable ID resolve identically."""

    for entry in lock.values():
        tag_id = inspect_image_id(entry.image)
        if tag_id != entry.image_id:
            raise ModelImageLockError(
                f"image tag drift for {entry.service}: expected {entry.image_id}, "
                f"tag resolves to {tag_id}"
            )
        digest_id = inspect_image_id(entry.image_id)
        if digest_id != entry.image_id:
            raise ModelImageLockError(
                f"locked image ID drift for {entry.service}: expected {entry.image_id}, "
                f"inspect returned {digest_id}"
            )


def _compose_base_command(
    compose_path: Path, *, profiles: set[str] | None = None
) -> list[str]:
    command = [
        "docker",
        "compose",
        "--project-name",
        "anila-models",
        "--file",
        str(compose_path.resolve()),
    ]
    for profile in sorted(profiles or ()):
        command.extend(("--profile", profile))
    return command


def verify_running_containers(
    lock: dict[str, LockEntry],
    compose_path: Path,
    *,
    profiles: set[str] | None = None,
) -> None:
    """Read immutable IDs back from exactly the expected running services."""

    base = _compose_base_command(compose_path, profiles=profiles)
    result = _run_utf8(base + ["ps", "--services", "--status", "running"])
    if result.returncode != 0:
        raise ModelImageLockError(
            "docker compose ps failed: "
            + (result.stderr.strip() or result.stdout.strip() or "unknown error")
        )
    expected = set(lock)
    running = {line.strip() for line in result.stdout.splitlines() if line.strip()}
    if running != expected:
        raise ModelImageLockError(
            "running model service set drift: "
            f"missing={sorted(expected - running)}, extra={sorted(running - expected)}"
        )
    for service, entry in lock.items():
        result = _run_utf8(base + ["ps", "-q", service])
        container_ids = [
            line.strip() for line in result.stdout.splitlines() if line.strip()
        ]
        if result.returncode != 0 or len(container_ids) != 1:
            raise ModelImageLockError(
                f"expected exactly one running container for {service}, "
                f"got {len(container_ids)}"
            )
        inspect = _run_utf8(
            [
                "docker",
                "container",
                "inspect",
                "--format",
                "{{.Image}}",
                container_ids[0],
            ]
        )
        actual = inspect.stdout.strip() if inspect.returncode == 0 else ""
        if actual != entry.image_id:
            raise ModelImageLockError(
                f"running container image drift for {service}: "
                f"expected {entry.image_id}, actual {actual or 'missing'}"
            )


def verify_lock(
    lock_path: Path,
    *,
    compose_path: Path = DEFAULT_COMPOSE,
    inventory_path: Path = DEFAULT_INVENTORY,
    profiles: set[str] | None = None,
    include_optional: bool = False,
    inspect_docker: bool = False,
    inspect_containers: bool = False,
) -> dict[str, LockEntry]:
    inventory = read_inventory(inventory_path)
    compose = read_compose(compose_path)
    lock = read_lock(
        lock_path,
        inventory,
        compose,
        profiles=profiles,
        include_optional=include_optional,
    )
    if inspect_docker or inspect_containers:
        verify_docker_images(lock)
    if inspect_containers:
        verify_running_containers(
            lock,
            compose_path,
            profiles=selected_profiles(
                inventory, profiles=profiles, include_optional=include_optional
            ),
        )
    return lock


def emit_lock(
    *,
    compose_path: Path = DEFAULT_COMPOSE,
    inventory_path: Path = DEFAULT_INVENTORY,
    profiles: set[str] | None = None,
    include_optional: bool = False,
) -> str:
    """Inspect enabled local image tags and return a deterministic lock TSV."""

    inventory = read_inventory(inventory_path)
    compose = read_compose(compose_path)
    verify_compose_inventory(compose, inventory)
    enabled = active_services(
        inventory, profiles=profiles, include_optional=include_optional
    )
    rows = ["# service\timage\timage_id\tactivation"]
    for service, declared in inventory.items():
        if service not in enabled:
            continue
        image_id = inspect_image_id(declared.image)
        rows.append("\t".join((service, declared.image, image_id, declared.activation)))
    if len(rows) == 1:
        raise ModelImageLockError("no enabled model services to lock")
    return "\n".join(rows) + "\n"


def _add_profile_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--profile",
        dest="profiles_override",
        action="append",
        default=None,
        help="enable one model Compose profile (repeatable)",
    )
    parser.add_argument(
        "--include-optional",
        action="store_true",
        help="enable every inventory profile (not just default services)",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compose", type=Path, default=DEFAULT_COMPOSE)
    parser.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument(
        "--profile", dest="root_profiles", action="append", default=None
    )
    parser.add_argument(
        "--include-optional", dest="root_include_optional", action="store_true"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check-wiring")
    _add_profile_options(check)

    emit = sub.add_parser("emit-lock")
    _add_profile_options(emit)
    emit.add_argument("--output", type=Path)

    verify = sub.add_parser("verify-lock", aliases=["verify"])
    _add_profile_options(verify)
    verify.add_argument("--lock", type=Path, required=True)
    verify.add_argument("--inspect-docker", action="store_true")
    verify.add_argument("--inspect-containers", action="store_true")
    return parser


def _selected_profiles(args: argparse.Namespace) -> set[str]:
    override = getattr(args, "profiles_override", None)
    profiles = (
        override if override is not None else getattr(args, "root_profiles", None)
    )
    return set(profiles or ())


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        profiles = _selected_profiles(args)
        include_optional = bool(
            getattr(args, "root_include_optional", False)
            or getattr(args, "include_optional", False)
        )
        inventory = read_inventory(args.inventory)
        compose = read_compose(args.compose)
        verify_compose_inventory(compose, inventory)
        if args.command == "check-wiring":
            active_services(
                inventory, profiles=profiles, include_optional=include_optional
            )
            print("model Compose/inventory wiring verified")
            return 0
        if args.command == "emit-lock":
            output = emit_lock(
                compose_path=args.compose,
                inventory_path=args.inventory,
                profiles=profiles,
                include_optional=include_optional,
            )
            if args.output:
                args.output.write_text(output, encoding="utf-8")
            else:
                print(output, end="")
            return 0
        read_lock(
            args.lock,
            inventory,
            compose,
            profiles=profiles,
            include_optional=include_optional,
        )
        verify_lock(
            args.lock,
            compose_path=args.compose,
            inventory_path=args.inventory,
            profiles=profiles,
            include_optional=include_optional,
            inspect_docker=args.inspect_docker,
            inspect_containers=args.inspect_containers,
        )
        print("model image content-ID lock verified")
        return 0
    except (ModelImageLockError, OSError) as exc:
        print(f"model image-lock verification failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
