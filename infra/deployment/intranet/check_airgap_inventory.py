#!/usr/bin/env python3
"""Validate the formal Compose service/image set against the air-gap inventory."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


REQUIRED_ENV = {
    "ANILA_DEPLOYMENT_PROFILE": "prod-intranet-card",
    "ADMIN_PASSWORD": "inventory-check-admin-0123456789",
    "CARD_INITIAL_OWNERS": "990000001",
    "CARD_CRL_BUNDLE_PATH": "/etc/anila/pki/card-crl-bundle.pem",
    "CARD_CRL_SOURCE": "synthetic-inventory-feed",
    "CARD_REQUIRED_CERT_POLICY_OIDS": "1.3.6.1.4.1.55555.1.1",
    "CODESERVER_PASSWORD": "inventory-check-code-0123456789",
    "CSP_APP_DB_PASSWORD": "0123456789abcdef0123456789abcdef",
    "CSP_DB_PASSWORD": "abcdef0123456789abcdef0123456789",
    "CSP_SECRET_KEY": "0123456789abcdef0123456789abcdef",
    "CSP_SERVICE_TOKEN": "abcdef0123456789abcdef0123456789",
    "INTERNAL_PLATFORM_API_KEY": "sk-internal-0123456789abcdef0123456789",
    "SITE_URL": "https://anila.inventory-check.invalid",
    "ANILA_STATE_DIR": "/tmp/anila-inventory-state",
    "ANILA_SECRETS_DIR": "/tmp/anila-inventory-state/secrets",
    "ANILA_TLS_CERTS_DIR": "/tmp/anila-inventory-state/tls",
    "N8N_OWNER_EMAIL": "n8n-owner@inventory-check.invalid",
    "N8N_OWNER_PASSWORD_HASH": (
        "$2b$12$abcdefghijklmnopqrstuuABCDEFGHIJKLMNOPQRSTUVWXYZ01234"
    ),
    "N8N_ENCRYPTION_KEY": "0123456789abcdef0123456789abcdef",
    "GITLAB_ROOT_PASSWORD": "inventory-check-gitlab-root-0123456789",
    "GITLAB_SSH_BIND_IP": "10.53.100.15",
}
ALLOWED_BUNDLES = {
    "01-anila-built.tar.gz",
    "02-base.tar.gz",
    "03-cold.tar.gz",
}


class InventoryError(RuntimeError):
    """Raised when inventory and Compose are not a closed set."""


@dataclass(frozen=True)
class InventoryEntry:
    service: str
    image: str
    bundle: str
    activation: str
    source: str


@dataclass(frozen=True)
class ModelInventoryEntry:
    service: str
    image: str
    activation: str


def load_inventory(path: Path) -> list[InventoryEntry]:
    entries: list[InventoryEntry] = []
    seen_services: set[str] = set()
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        fields = raw.split("\t")
        if len(fields) != 5 or any(not field.strip() for field in fields):
            raise InventoryError(f"{path}:{line_number}: expected five non-empty TSV fields")
        entry = InventoryEntry(*(field.strip() for field in fields))
        if entry.service in seen_services:
            raise InventoryError(f"{path}:{line_number}: duplicate service {entry.service!r}")
        if entry.bundle not in ALLOWED_BUNDLES:
            raise InventoryError(
                f"{path}:{line_number}: unsupported bundle {entry.bundle!r}"
            )
        if entry.activation != "default" and not entry.activation.startswith("profile:"):
            raise InventoryError(
                f"{path}:{line_number}: activation must be default or profile:<name>"
            )
        if entry.activation.startswith("profile:") and not entry.activation.removeprefix(
            "profile:"
        ):
            raise InventoryError(f"{path}:{line_number}: empty profile name")
        if entry.source not in {"built", "upstream"}:
            raise InventoryError(
                f"{path}:{line_number}: source must be built or upstream"
            )
        seen_services.add(entry.service)
        entries.append(entry)
    if not entries:
        raise InventoryError(f"{path}: inventory is empty")
    present_bundles = {entry.bundle for entry in entries}
    if present_bundles != ALLOWED_BUNDLES:
        raise InventoryError(
            f"{path}: bundle set mismatch: expected={sorted(ALLOWED_BUNDLES)}, "
            f"actual={sorted(present_bundles)}"
        )
    for entry in entries:
        if entry.source == "built" and entry.bundle != "01-anila-built.tar.gz":
            raise InventoryError(
                f"{path}: built service {entry.service!r} must be in 01-anila-built.tar.gz"
            )
        if entry.source == "upstream" and entry.bundle == "01-anila-built.tar.gz":
            raise InventoryError(
                f"{path}: upstream service {entry.service!r} cannot be in 01-anila-built.tar.gz"
            )
    return entries


def load_model_inventory(path: Path) -> list[ModelInventoryEntry]:
    entries: list[ModelInventoryEntry] = []
    seen_services: set[str] = set()
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        fields = raw.split("\t")
        if len(fields) != 3 or any(not field.strip() for field in fields):
            raise InventoryError(f"{path}:{line_number}: expected three non-empty TSV fields")
        entry = ModelInventoryEntry(*(field.strip() for field in fields))
        if entry.service in seen_services:
            raise InventoryError(f"{path}:{line_number}: duplicate service {entry.service!r}")
        if entry.activation != "default" and not entry.activation.startswith("profile:"):
            raise InventoryError(
                f"{path}:{line_number}: activation must be default or profile:<name>"
            )
        if entry.activation.startswith("profile:") and not entry.activation.removeprefix(
            "profile:"
        ):
            raise InventoryError(f"{path}:{line_number}: empty profile name")
        seen_services.add(entry.service)
        entries.append(entry)
    if not entries:
        raise InventoryError(f"{path}: model inventory is empty")
    return entries


def compose_config(
    root: Path,
    *,
    all_profiles: bool,
    compose_file: Path | None = None,
    trusted_env: dict[str, str] | None = None,
) -> dict[str, object]:
    if shutil.which("docker") is None:
        raise InventoryError("docker CLI is not installed")
    env = os.environ.copy()
    env.pop("COMPOSE_FILE", None)
    env.pop("COMPOSE_PROFILES", None)
    for key, value in REQUIRED_ENV.items():
        if not env.get(key):
            env[key] = value
    # Inventory validation is a connected-build operation, not a formal
    # deployment.  Resolve every mandatory Compose pin to the tag declared by
    # the inventory so the comparison remains closed and deterministic.  Use
    # assignment (not setdefault) to prevent a caller's exported image variable
    # from making the checker validate a different graph.
    if trusted_env:
        env.update(trusted_env)
    command = ["docker", "compose"]
    if compose_file is not None:
        command.extend(["-f", str(compose_file)])
    if all_profiles:
        command.extend(["--profile", "*"])
    command.extend(["config", "--format", "json"])
    try:
        result = subprocess.run(
            command,
            cwd=root,
            env=env,
            check=False,
            capture_output=True,
            encoding="utf-8",
        )
    except OSError as exc:
        raise InventoryError(f"cannot execute docker compose: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise InventoryError(f"docker compose config failed: {detail}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise InventoryError(f"docker compose emitted invalid JSON: {exc}") from exc


def _service_images(config: dict[str, object]) -> dict[str, str]:
    services = config.get("services")
    if not isinstance(services, dict):
        raise InventoryError("compose config has no services object")
    project_name = config.get("name")
    output: dict[str, str] = {}
    for service, raw in services.items():
        if not isinstance(raw, dict):
            raise InventoryError(f"compose service {service!r} is not an object")
        image = raw.get("image")
        if not isinstance(image, str) and "build" in raw and isinstance(project_name, str):
            # Compose's JSON model omits the implicit image name for build-only
            # services; `docker compose config --images` resolves it this way.
            image = f"{project_name}-{service}"
        if not isinstance(image, str):
            raise InventoryError(f"compose service {service!r} has no resolved image")
        output[str(service)] = image
    return output


def _compare_mapping(
    label: str, expected: dict[str, str], actual: dict[str, str]
) -> list[str]:
    errors: list[str] = []
    missing = sorted(expected.keys() - actual.keys())
    extra = sorted(actual.keys() - expected.keys())
    if missing:
        errors.append(f"{label}: inventory services missing from Compose: {', '.join(missing)}")
    if extra:
        errors.append(f"{label}: Compose services missing from inventory: {', '.join(extra)}")
    for service in sorted(expected.keys() & actual.keys()):
        if expected[service] != actual[service]:
            errors.append(
                f"{label}: {service} image mismatch: inventory={expected[service]!r}, "
                f"compose={actual[service]!r}"
            )
    return errors


def validate(root: Path, inventory_path: Path) -> tuple[int, int]:
    entries = load_inventory(inventory_path)
    trusted_env = {"ANILA_DEPLOYMENT_PROFILE": "prod-intranet-card"}
    for entry in entries:
        variable = "ANILA_IMAGE_" + entry.service.upper().replace("-", "_")
        trusted_env[variable] = entry.image
    default_config = compose_config(
        root, all_profiles=False, trusted_env=trusted_env
    )
    all_config = compose_config(root, all_profiles=True, trusted_env=trusted_env)
    default_actual = _service_images(default_config)
    all_actual = _service_images(all_config)
    default_expected = {
        entry.service: entry.image for entry in entries if entry.activation == "default"
    }
    all_expected = {entry.service: entry.image for entry in entries}

    errors = _compare_mapping("default", default_expected, default_actual)
    errors.extend(_compare_mapping("all profiles", all_expected, all_actual))

    all_services = all_config["services"]
    for entry in entries:
        raw_service = all_services.get(entry.service, {})
        profiles = set(raw_service.get("profiles", []))
        expected_profiles = (
            set()
            if entry.activation == "default"
            else {entry.activation.removeprefix("profile:")}
        )
        if profiles != expected_profiles:
            errors.append(
                f"{entry.service}: profile mismatch: inventory={sorted(expected_profiles)}, "
                f"compose={sorted(profiles)}"
            )
        has_build = "build" in raw_service
        if entry.source == "built" and not has_build:
            errors.append(f"{entry.service}: inventory says built but Compose has no build")
        if entry.source == "upstream" and has_build:
            errors.append(f"{entry.service}: inventory says upstream but Compose has build")

    if errors:
        raise InventoryError("\n".join(errors))
    return len(default_expected), len(all_expected) - len(default_expected)


def validate_models(root: Path, inventory_path: Path) -> tuple[int, int, int]:
    entries = load_model_inventory(inventory_path)
    compose_file = root / "infra" / "models" / "docker-compose.yml"
    default_config = compose_config(
        root, all_profiles=False, compose_file=compose_file
    )
    all_config = compose_config(root, all_profiles=True, compose_file=compose_file)
    default_actual = _service_images(default_config)
    all_actual = _service_images(all_config)
    default_expected = {
        entry.service: entry.image for entry in entries if entry.activation == "default"
    }
    all_expected = {entry.service: entry.image for entry in entries}

    errors = _compare_mapping("model default", default_expected, default_actual)
    errors.extend(_compare_mapping("model all profiles", all_expected, all_actual))
    all_services = all_config["services"]
    for entry in entries:
        raw_service = all_services.get(entry.service, {})
        profiles = set(raw_service.get("profiles", []))
        expected_profiles = (
            set()
            if entry.activation == "default"
            else {entry.activation.removeprefix("profile:")}
        )
        if profiles != expected_profiles:
            errors.append(
                f"model {entry.service}: profile mismatch: "
                f"inventory={sorted(expected_profiles)}, compose={sorted(profiles)}"
            )
    if errors:
        raise InventoryError("\n".join(errors))
    unique_images = len({entry.image for entry in entries})
    return len(default_expected), len(all_expected) - len(default_expected), unique_images


def main(argv: list[str] | None = None) -> int:
    default_root = Path(__file__).resolve().parents[3]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=default_root)
    parser.add_argument(
        "--inventory",
        type=Path,
        default=Path(__file__).with_name("platform-image-inventory.tsv"),
    )
    parser.add_argument(
        "--model-inventory",
        type=Path,
        default=Path(__file__).with_name("model-image-inventory.tsv"),
    )
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    try:
        required, optional = validate(args.root.resolve(), args.inventory.resolve())
        model_default, model_optional, model_images = validate_models(
            args.root.resolve(), args.model_inventory.resolve()
        )
    except InventoryError as exc:
        print(f"air-gap inventory check FAILED:\n{exc}", file=sys.stderr)
        return 1
    if not args.quiet:
        print(
            "air-gap inventory check OK: "
            f"platform={required} default + {optional} optional profile service image(s); "
            f"models={model_default} default + {model_optional} optional profile service(s) "
            f"across {model_images} unique optional model image(s)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
