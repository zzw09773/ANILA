#!/usr/bin/env python3
"""Verify Gate 1 formal-platform image content-ID pinning.

The air-gap exporter records the exact ``sha256:...`` Docker image ID for
every platform service.  Formal deployment copies those IDs into dedicated
Compose image variables, so a later same-name tag mutation cannot change what
``docker compose up`` runs.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
DEFAULT_INVENTORY = ROOT / "infra/deployment/intranet/platform-image-inventory.tsv"
DEFAULT_COMPOSE = ROOT / "infra/compose/platform.yml"
GATE2_PILOT_COMPOSE = ROOT / "infra/compose/gate2-pilot.yml"
IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
POSTURES = ("normal", "gate2-pilot")
GATE2_PILOT_ACTIVE_SERVICES = frozenset(
    {
        "gitlab",
        "n8n",
        "redis",
        "csp-db",
        "csp",
        "router",
        "anila-ui",
        "anilalm",
        "nginx",
    }
)
FORBIDDEN_AMBIENT_COMPOSE_ENV = (
    "COMPOSE_FILE",
    "COMPOSE_PROFILES",
    "COMPOSE_PROJECT_NAME",
    "COMPOSE_ENV_FILES",
    "COMPOSE_DISABLE_ENV_FILE",
    "COMPOSE_PATH_SEPARATOR",
)

IMAGE_ENV_BY_SERVICE = {
    "csp-db": "ANILA_IMAGE_CSP_DB",
    "csp": "ANILA_IMAGE_CSP",
    "redis": "ANILA_IMAGE_REDIS",
    "ingestion-worker": "ANILA_IMAGE_INGESTION_WORKER",
    "router": "ANILA_IMAGE_ROUTER",
    "nginx": "ANILA_IMAGE_NGINX",
    "pptx-renderer": "ANILA_IMAGE_PPTX_RENDERER",
    "anila-studio": "ANILA_IMAGE_ANILA_STUDIO",
    "anila-agent": "ANILA_IMAGE_ANILA_AGENT",
    "asr-gateway": "ANILA_IMAGE_ASR_GATEWAY",
    "anilalm": "ANILA_IMAGE_ANILALM",
    "anila-ui": "ANILA_IMAGE_ANILA_UI",
    "codeserver": "ANILA_IMAGE_CODESERVER",
    "n8n": "ANILA_IMAGE_N8N",
    "gitlab": "ANILA_IMAGE_GITLAB",
}


class ImageLockError(RuntimeError):
    """Formal image lock is absent, stale, or inconsistent."""


@dataclass(frozen=True)
class InventoryEntry:
    service: str
    image: str
    bundle: str
    activation: str
    source: str


@dataclass(frozen=True)
class LockEntry:
    service: str
    image: str
    image_id: str
    bundle: str
    activation: str


def active_services(
    inventory: dict[str, InventoryEntry],
    *,
    posture: str = "normal",
    include_optional: bool,
) -> set[str]:
    """Return the exact service set admitted by the selected runtime posture."""

    if posture == "gate2-pilot":
        if include_optional:
            raise ImageLockError(
                "gate2-pilot posture forbids optional Compose profiles"
            )
        missing = GATE2_PILOT_ACTIVE_SERVICES - set(inventory)
        if missing:
            raise ImageLockError(
                f"gate2-pilot image inventory missing services: {sorted(missing)}"
            )
        return set(GATE2_PILOT_ACTIVE_SERVICES)
    if posture != "normal":
        raise ImageLockError(f"unsupported image-lock posture {posture!r}")
    return {
        service
        for service, entry in inventory.items()
        if entry.activation == "default" or include_optional
    }


def _data_lines(path: Path) -> list[tuple[int, list[str]]]:
    rows: list[tuple[int, list[str]]] = []
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        rows.append((line_number, raw.split("\t")))
    return rows


def read_inventory(path: Path) -> dict[str, InventoryEntry]:
    entries: dict[str, InventoryEntry] = {}
    for line_number, fields in _data_lines(path):
        if len(fields) != 5:
            raise ImageLockError(f"{path}:{line_number}: expected 5 TSV fields")
        entry = InventoryEntry(*fields)
        if entry.service in entries:
            raise ImageLockError(f"{path}:{line_number}: duplicate {entry.service}")
        entries[entry.service] = entry
    if set(entries) != set(IMAGE_ENV_BY_SERVICE):
        missing = sorted(set(entries) - set(IMAGE_ENV_BY_SERVICE))
        stale = sorted(set(IMAGE_ENV_BY_SERVICE) - set(entries))
        raise ImageLockError(
            f"image-variable mapping drift; unmapped={missing}, stale={stale}"
        )
    return entries


def read_lock(
    path: Path, inventory: dict[str, InventoryEntry]
) -> dict[str, LockEntry]:
    entries: dict[str, LockEntry] = {}
    for line_number, fields in _data_lines(path):
        if len(fields) != 5:
            raise ImageLockError(f"{path}:{line_number}: expected 5 TSV fields")
        entry = LockEntry(*fields)
        if entry.service in entries:
            raise ImageLockError(f"{path}:{line_number}: duplicate {entry.service}")
        if not IMAGE_ID_RE.fullmatch(entry.image_id):
            raise ImageLockError(
                f"{path}:{line_number}: invalid image ID for {entry.service}"
            )
        declared = inventory.get(entry.service)
        if declared is None:
            raise ImageLockError(f"{path}:{line_number}: unknown service {entry.service}")
        if (entry.image, entry.bundle, entry.activation) != (
            declared.image,
            declared.bundle,
            declared.activation,
        ):
            raise ImageLockError(
                f"{path}:{line_number}: inventory metadata mismatch for {entry.service}"
            )
        entries[entry.service] = entry
    if set(entries) != set(inventory):
        raise ImageLockError(
            "lock service set mismatch: "
            f"missing={sorted(set(inventory) - set(entries))}, "
            f"extra={sorted(set(entries) - set(inventory))}"
        )
    return entries


def read_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if name in values:
            raise ImageLockError(f"{path}:{line_number}: duplicate variable {name}")
        values[name] = value
    return values


def verify_compose_wiring(
    compose_path: Path, inventory: dict[str, InventoryEntry]
) -> None:
    service_images: dict[str, str] = {}
    current: str | None = None
    in_services = False
    service_re = re.compile(r"^  ([A-Za-z0-9_-]+):\s*$")
    # Formal Compose must not retain a mutable tag fallback.  ``:?`` makes a
    # missing pin a Compose-rendering error before Docker can select an image;
    # ``:-tag`` would silently reintroduce the drift this gate is meant to stop.
    image_re = re.compile(r"^    image:\s*\$\{([A-Z0-9_]+):\?[^}]+\}\s*$")
    for raw in compose_path.read_text(encoding="utf-8").splitlines():
        if raw == "services:":
            in_services = True
            continue
        if in_services and raw and not raw.startswith((" ", "#")):
            break
        if not in_services:
            continue
        service_match = service_re.match(raw)
        if service_match:
            current = service_match.group(1)
            continue
        if current is None or not raw.startswith("    image:"):
            continue
        image_match = image_re.match(raw)
        if image_match is None:
            raise ImageLockError(
                f"{compose_path}: service {current} image is not variable-pinned"
            )
        service_images[current] = image_match.group(1)

    for service, declared in inventory.items():
        actual = service_images.get(service)
        expected = IMAGE_ENV_BY_SERVICE[service]
        if actual != expected:
            raise ImageLockError(
                f"{compose_path}: {service} wiring expected {expected}, got {actual}"
            )


def verify_env(
    env_file: Path,
    inventory: dict[str, InventoryEntry],
    lock: dict[str, LockEntry] | None,
    *,
    include_optional: bool,
    inspect_docker: bool,
    inspect_containers: bool = False,
    posture: str = "normal",
) -> None:
    values = read_dotenv(env_file)
    forbidden_in_file = [name for name in FORBIDDEN_AMBIENT_COMPOSE_ENV if name in values]
    if forbidden_in_file:
        raise ImageLockError(
            f"{env_file}: formal dotenv forbids Compose control variables: "
            + ", ".join(forbidden_in_file)
        )
    profile = values.get("ANILA_DEPLOYMENT_PROFILE", "")
    if profile not in {"prod-intranet-card", "prod-intranet-card-breakglass"}:
        raise ImageLockError(
            f"{env_file}: unsupported formal ANILA_DEPLOYMENT_PROFILE {profile!r}"
        )
    break_glass_fields = (
        "ANILA_BREAK_GLASS_OWNER",
        "ANILA_BREAK_GLASS_TICKET",
        "ANILA_BREAK_GLASS_EXPIRES_AT",
    )
    if profile == "prod-intranet-card-breakglass":
        missing_metadata = [name for name in break_glass_fields if not values.get(name)]
        if missing_metadata:
            raise ImageLockError(
                f"{env_file}: break-glass profile missing " + ", ".join(missing_metadata)
            )
    elif any(values.get(name) for name in break_glass_fields):
        raise ImageLockError(
            f"{env_file}: normal card profile must not retain break-glass metadata"
        )
    pilot_requested = values.get("ANILA_PILOT_MODE", "").strip().lower() == "true"
    if posture == "gate2-pilot" and not pilot_requested:
        raise ImageLockError(
            f"{env_file}: gate2-pilot posture requires ANILA_PILOT_MODE=true"
        )
    if posture == "normal" and pilot_requested:
        raise ImageLockError(
            f"{env_file}: ANILA_PILOT_MODE=true requires --posture gate2-pilot"
        )
    expected_services = active_services(
        inventory, posture=posture, include_optional=include_optional
    )
    for service in sorted(expected_services):
        variable = IMAGE_ENV_BY_SERVICE[service]
        value = values.get(variable, "")
        if not IMAGE_ID_RE.fullmatch(value):
            raise ImageLockError(
                f"{env_file}: {variable} for {service} must be sha256:<64 hex>"
            )
        if lock is not None and value != lock[service].image_id:
            raise ImageLockError(
                f"{env_file}: {variable} does not match bundle lock for {service}"
            )
        if inspect_docker or inspect_containers:
            result = subprocess.run(
                ["docker", "image", "inspect", "--format", "{{.Id}}", value],
                check=False,
                capture_output=True,
                text=True,
            )
            actual_id = result.stdout.strip() if result.returncode == 0 else ""
            if actual_id != value:
                raise ImageLockError(
                    f"Docker image missing/stale for {service}: expected {value}, "
                    f"actual {actual_id or 'missing'}"
                )

    if inspect_docker or inspect_containers:
        # ``--env-file`` does not override an already-exported shell variable.
        # Verify the effective Compose model as well as the file so an operator
        # cannot accidentally run a different image than the audited lock.
        compose_config = resolved_compose_config(
            env_file, include_optional=include_optional, posture=posture
        )
        verify_resolved_compose_images(
            values,
            inventory,
            compose_config,
            include_optional=include_optional,
            posture=posture,
        )
    if inspect_containers:
        verify_running_containers(
            values,
            inventory,
            env_file,
            include_optional=include_optional,
            posture=posture,
        )


def verify_resolved_compose_images(
    values: dict[str, str],
    inventory: dict[str, InventoryEntry],
    compose_config: dict[str, object],
    *,
    include_optional: bool,
    posture: str = "normal",
) -> None:
    """Compare pins with Compose's resolved config, including shell overrides."""

    services = compose_config.get("services")
    if not isinstance(services, dict):
        raise ImageLockError("docker compose config has no services object")
    if compose_config.get("name") != "anila-platform":
        raise ImageLockError(
            "resolved Compose project identity drift: expected 'anila-platform', "
            f"got {compose_config.get('name')!r}"
        )
    csp = services.get("csp")
    csp_environment = csp.get("environment") if isinstance(csp, dict) else None
    if not isinstance(csp_environment, dict):
        raise ImageLockError("resolved CSP service has no environment object")
    for name in (
        "ANILA_DEPLOYMENT_PROFILE",
        "ANILA_BREAK_GLASS_OWNER",
        "ANILA_BREAK_GLASS_TICKET",
        "ANILA_BREAK_GLASS_EXPIRES_AT",
    ):
        expected = values.get(name, "")
        actual = csp_environment.get(name, "")
        if actual != expected:
            raise ImageLockError(
                f"resolved CSP posture override for {name}: "
                f"dotenv={expected!r}, effective={actual!r}"
            )
    pilot_mode = str(csp_environment.get("ANILA_PILOT_MODE", "")).lower()
    pilot_marker = csp_environment.get("GATE2_PILOT_COMPOSE_POSTURE", "")
    if posture == "gate2-pilot":
        if pilot_mode != "true" or pilot_marker != "gate2-pilot-v1":
            raise ImageLockError(
                "resolved CSP is missing the Gate 2 pilot mode/posture marker"
            )
    elif pilot_marker:
        raise ImageLockError(
            "resolved normal CSP unexpectedly contains a Gate 2 pilot posture marker"
        )
    expected_services = active_services(
        inventory, posture=posture, include_optional=include_optional
    )
    actual_services = set(services)
    if actual_services != expected_services:
        raise ImageLockError(
            "resolved Compose service set drift: "
            f"missing={sorted(expected_services - actual_services)}, "
            f"unexpected={sorted(actual_services - expected_services)}"
        )
    for service in sorted(expected_services):
        service_config = services.get(service)
        if not isinstance(service_config, dict):
            raise ImageLockError(f"resolved Compose service missing: {service}")
        expected = values[IMAGE_ENV_BY_SERVICE[service]]
        actual = service_config.get("image")
        if actual != expected:
            raise ImageLockError(
                f"resolved Compose image override for {service}: "
                f"expected {expected}, got {actual!r}"
            )


def resolved_compose_config(
    env_file: Path,
    *,
    include_optional: bool,
    posture: str = "normal",
) -> dict[str, object]:
    ambient = [name for name in FORBIDDEN_AMBIENT_COMPOSE_ENV if name in os.environ]
    if ambient:
        raise ImageLockError(
            "formal lifecycle forbids ambient Compose control variables: "
            + ", ".join(ambient)
        )
    command = [
        "docker",
        "compose",
    ]
    if posture == "gate2-pilot":
        if include_optional:
            raise ImageLockError(
                "gate2-pilot posture forbids optional Compose profiles"
            )
        command.extend(
            (
                "--project-name",
                "anila-platform",
                "--file",
                str(DEFAULT_COMPOSE.resolve()),
                "--file",
                str(GATE2_PILOT_COMPOSE.resolve()),
            )
        )
    elif posture != "normal":
        raise ImageLockError(f"unsupported image-lock posture {posture!r}")
    command.extend(("--env-file", str(env_file.resolve())))
    if include_optional:
        command.extend(("--profile", "developer-tools"))
    command.extend(("config", "--format", "json"))
    result = subprocess.run(
        command,
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
    )
    if result.returncode != 0:
        raise ImageLockError(
            "docker compose config failed: "
            + (result.stderr.strip() or result.stdout.strip() or "unknown error")
        )
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ImageLockError(f"docker compose emitted invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ImageLockError("docker compose config must be a JSON object")
    return value


def _compose_base_command(
    env_file: Path,
    *,
    include_optional: bool,
    posture: str = "normal",
) -> list[str]:
    ambient = [name for name in FORBIDDEN_AMBIENT_COMPOSE_ENV if name in os.environ]
    if ambient:
        raise ImageLockError(
            "formal lifecycle forbids ambient Compose control variables: "
            + ", ".join(ambient)
        )
    command = [
        "docker",
        "compose",
    ]
    if posture == "gate2-pilot":
        if include_optional:
            raise ImageLockError(
                "gate2-pilot posture forbids optional Compose profiles"
            )
        command.extend(
            (
                "--project-name",
                "anila-platform",
                "--file",
                str(DEFAULT_COMPOSE.resolve()),
                "--file",
                str(GATE2_PILOT_COMPOSE.resolve()),
            )
        )
    elif posture != "normal":
        raise ImageLockError(f"unsupported image-lock posture {posture!r}")
    command.extend(("--env-file", str(env_file.resolve())))
    if include_optional:
        command.extend(("--profile", "developer-tools"))
    return command


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


def verify_running_containers(
    values: dict[str, str],
    inventory: dict[str, InventoryEntry],
    env_file: Path,
    *,
    include_optional: bool,
    posture: str = "normal",
) -> None:
    """Read back immutable image IDs from the currently running containers."""

    base = _compose_base_command(
        env_file, include_optional=include_optional, posture=posture
    )
    expected_services = active_services(
        inventory, posture=posture, include_optional=include_optional
    )
    result = _run_utf8(base + ["ps", "--services", "--status", "running"])
    if result.returncode != 0:
        raise ImageLockError(
            "docker compose ps failed: "
            + (result.stderr.strip() or result.stdout.strip() or "unknown error")
        )
    running_services = {line.strip() for line in result.stdout.splitlines() if line.strip()}
    if running_services != expected_services:
        raise ImageLockError(
            "running Compose service set drift: "
            f"missing={sorted(expected_services - running_services)}, "
            f"unexpected={sorted(running_services - expected_services)}"
        )

    for service in sorted(expected_services):
        result = _run_utf8(base + ["ps", "-q", service])
        container_ids = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if result.returncode != 0 or len(container_ids) != 1:
            raise ImageLockError(
                f"expected exactly one running container for {service}, "
                f"got {len(container_ids)}"
            )
        inspect = _run_utf8(
            ["docker", "container", "inspect", "--format", "{{.Image}}", container_ids[0]]
        )
        actual_id = inspect.stdout.strip() if inspect.returncode == 0 else ""
        expected_id = values[IMAGE_ENV_BY_SERVICE[service]]
        if actual_id != expected_id:
            raise ImageLockError(
                f"running container image drift for {service}: "
                f"expected {expected_id}, actual {actual_id or 'missing'}"
            )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument("--compose", type=Path, default=DEFAULT_COMPOSE)
    parser.add_argument("--posture", choices=POSTURES, default="normal")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("check-wiring")

    emit = sub.add_parser("emit-env")
    emit.add_argument("--lock", type=Path, required=True)

    sub.add_parser(
        "emit-build-env",
        help="emit trusted tag-valued variables for the connected exporter",
    )

    verify = sub.add_parser("verify-env")
    verify.add_argument(
        "--posture",
        choices=POSTURES,
        default=argparse.SUPPRESS,
        help="runtime service posture (default: normal)",
    )
    verify.add_argument("--env-file", type=Path, required=True)
    verify.add_argument("--lock", type=Path)
    verify.add_argument("--include-optional", action="store_true")
    verify.add_argument("--inspect-docker", action="store_true")
    verify.add_argument("--inspect-containers", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        inventory = read_inventory(args.inventory)
        verify_compose_wiring(args.compose, inventory)
        if args.command == "check-wiring":
            print("compose image-lock wiring verified")
            return 0
        if args.command == "emit-env":
            lock = read_lock(args.lock, inventory)
            for service in inventory:
                print(f"{IMAGE_ENV_BY_SERVICE[service]}\t{lock[service].image_id}")
            return 0
        if args.command == "emit-build-env":
            print("ANILA_DEPLOYMENT_PROFILE\tprod-intranet-card")
            for service, entry in inventory.items():
                print(f"{IMAGE_ENV_BY_SERVICE[service]}\t{entry.image}")
            return 0
        lock = read_lock(args.lock, inventory) if args.lock else None
        verify_env(
            args.env_file,
            inventory,
            lock,
            include_optional=args.include_optional,
            inspect_docker=args.inspect_docker,
            inspect_containers=args.inspect_containers,
            posture=args.posture,
        )
        print("formal compose image lock verified")
        return 0
    except (ImageLockError, OSError) as exc:
        print(f"image-lock verification failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
