#!/usr/bin/env python3
"""Check the repository-side Gate 6 engineering contract posture.

This command is intentionally a *no-approval* check.  It proves that the
repository only carries the disabled P0 authoring template, that the P9
inspection path cannot turn that template into acceptance evidence, and that
production trust/signature material has not been checked in.  It does not run
P1-P9 drills and it never creates a signed profile or VERIFIED evidence.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
P0_TEMPLATE_RELATIVE = Path(
    "infra/policy/gate6/production-acceptance-profile.disabled-template.json"
)
P9_INVENTORY_RELATIVE = Path("infra/policy/gate5/model-governance-inventory.v1.json")
P9_TEMPLATE_RELATIVE = Path(
    "infra/policy/gate5/model-governance-profile.disabled-template.json"
)

_SKIP_PARTS = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
    }
)
_GATE6_NAME_MARKER_RE = re.compile(
    r"(?:gate6|production[-_.]?acceptance)", re.IGNORECASE
)
_PATH_WORD_RE = re.compile(
    r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+"
)
_CREDENTIAL_MARKERS = frozenset(
    {
        "evidence",
        "key",
        "keypair",
        "keypairs",
        "keys",
        "private",
        "privatekey",
        "privatekeypair",
        "secret",
        "secrets",
        "sign",
        "signature",
        "signatures",
        "signed",
        "signer",
        "signers",
        "signing",
        "trust",
        "trusted",
        "truststore",
    }
)
_MATERIAL_SUFFIXES = frozenset({".asc", ".crt", ".der", ".key", ".p12", ".pem", ".pfx", ".sig"})
_PEM_MARKER_RE = re.compile(r"-----BEGIN [A-Z0-9][A-Z0-9 ]*-----")


class RepositoryPostureError(RuntimeError):
    """Raised when repository material could be mistaken for approval evidence."""


def _import_local_packages(repo_root: Path) -> None:
    """Make the editable source package and namespace policies importable."""

    for import_root in (repo_root, repo_root / "packages/anila-security/src"):
        value = str(import_root)
        if import_root.is_dir() and value not in sys.path:
            sys.path.insert(0, value)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise RepositoryPostureError(f"cannot read JSON material: {path}") from exc
    if raw.startswith(b"\xef\xbb\xbf"):
        raise RepositoryPostureError(f"UTF-8 BOM is forbidden in JSON material: {path}")
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise RepositoryPostureError(
                    f"duplicate JSON key {key!r} in {path}"
                )
            result[key] = value
        return result

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RepositoryPostureError(f"invalid JSON material: {path}") from exc
    if not isinstance(value, dict):
        raise RepositoryPostureError(f"JSON root must be an object: {path}")
    return value


def _contains_key(value: Any, key: str) -> bool:
    if isinstance(value, dict):
        return key in value or any(_contains_key(item, key) for item in value.values())
    if isinstance(value, list):
        return any(_contains_key(item, key) for item in value)
    return False


def _contains_pem_marker(value: Any) -> bool:
    if isinstance(value, str):
        return _PEM_MARKER_RE.search(value) is not None
    if isinstance(value, dict):
        return any(_contains_pem_marker(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_pem_marker(item) for item in value)
    return False


def _assert_disabled_p0_template(path: Path) -> dict[str, Any]:
    """Require the checked-in P0 file to be an explicit non-approval template."""

    profile = _read_json(path)
    expected = {
        "enabled": False,
        "template_only": True,
        "approval_status": "disabled_template",
    }
    for field, value in expected.items():
        if profile.get(field) != value:
            raise RepositoryPostureError(
                f"P0 template must set {field}={value!r}: {path}"
            )
    if profile.get("signatures") != []:
        raise RepositoryPostureError("P0 template must contain no signatures")
    if profile.get("profile_content_sha256") is not None:
        raise RepositoryPostureError("P0 template must contain no content hash")

    _import_local_packages(path.parents[3])
    from anila_security.production_acceptance_profile import (
        ProductionAcceptanceProfileError,
        validate_production_acceptance_profile,
        verify_production_acceptance_profile,
    )

    try:
        validate_production_acceptance_profile(profile, allow_disabled_template=True)
    except ProductionAcceptanceProfileError as exc:
        raise RepositoryPostureError(
            f"P0 disabled template does not satisfy its authoring contract: {exc}"
        ) from exc

    # Runtime verification must reject the template even when a caller passes
    # an empty trust mapping.  This assertion protects against accidentally
    # adding an admission escape hatch to the shared verifier.
    try:
        verify_production_acceptance_profile(profile, {})
    except ProductionAcceptanceProfileError:
        pass
    else:
        raise RepositoryPostureError(
            "production profile verifier accepted the disabled P0 template"
        )
    return profile


def _assert_p9_disabled_mode(repo_root: Path) -> dict[str, Any]:
    """Exercise only the P9 disabled inspection mode; never create approval."""

    _import_local_packages(repo_root)
    from infra.policy.gate6.generate_p9_enabled_callsite_inventory import (
        generate_evidence,
    )

    try:
        evidence = generate_evidence(
            inventory_path=repo_root / P9_INVENTORY_RELATIVE,
            profile_path=repo_root / P9_TEMPLATE_RELATIVE,
            repo_root=repo_root,
            allow_disabled_template=True,
            generated_at="2026-01-01T00:00:00Z",
        )
    except Exception as exc:  # noqa: BLE001 - posture must fail closed
        raise RepositoryPostureError(f"P9 disabled inspection failed: {exc}") from exc

    expected = {
        "status": "NOT_ACCEPTANCE",
        "acceptance_status": "NOT_ACCEPTANCE",
        "environment": "non-production",
        "profile_enabled": False,
        "gate6_pass": False,
        "enabled_callsites": [],
    }
    for field, value in expected.items():
        if evidence.get(field) != value:
            raise RepositoryPostureError(
                f"P9 disabled mode must set {field}={value!r}, got {evidence.get(field)!r}"
            )
    return {field: evidence[field] for field in expected}


def _iter_repository_files(repo_root: Path) -> Sequence[Path]:
    files: list[Path] = []
    for path in repo_root.rglob("*"):
        if not path.is_file() or any(part in _SKIP_PARTS for part in path.parts):
            continue
        files.append(path)
    return tuple(files)


def _contains_credential_path_marker(relative_path: str) -> bool:
    """Match explicit sensitive words after separator/camel/acronym splitting."""

    tokens = {match.group(0).casefold() for match in _PATH_WORD_RE.finditer(relative_path)}
    return not tokens.isdisjoint(_CREDENTIAL_MARKERS)


def _assert_no_production_evidence(repo_root: Path, p0_template: Path) -> list[str]:
    """Reject checked-in P0 trust/signature/private material, fail closed."""

    template_resolved = p0_template.resolve()
    for path in _iter_repository_files(repo_root):
        if path.resolve() == template_resolved:
            continue
        relative = path.relative_to(repo_root).as_posix()
        lower = relative.lower()
        # Check the complete normalized relative path before any suffix-based
        # filtering.  Gate 6 and credential markers may be split across a
        # parent directory and a non-JSON child filename.
        if _GATE6_NAME_MARKER_RE.search(lower) and _contains_credential_path_marker(relative):
            raise RepositoryPostureError(
                f"Gate 6 production trust/signature material is checked in: {relative}"
            )
        if path.suffix.lower() in _MATERIAL_SUFFIXES and _GATE6_NAME_MARKER_RE.search(lower):
            raise RepositoryPostureError(
                f"Gate 6 key/signature material is checked in: {relative}"
            )
        if path.suffix.lower() not in {".json", ".jsonl"}:
            continue
        try:
            payload = _read_json(path)
        except RepositoryPostureError:
            # Unrelated JSON-like files are outside this posture contract.  A
            # Gate 6-named credential file was rejected above by its filename.
            continue
        if _contains_key(payload, "trusted_signers"):
            raise RepositoryPostureError(
                f"production trust-store material is checked in: {relative}"
            )
        if _contains_pem_marker(payload):
            raise RepositoryPostureError(
                f"PEM key/certificate material is checked in: {relative}"
            )
        if payload.get("schema_version") == "anila.gate6.production-acceptance.v1":
            if (
                payload.get("enabled") is not False
                or payload.get("template_only") is not True
                or payload.get("approval_status") != "disabled_template"
                or payload.get("signatures") != []
                or payload.get("profile_content_sha256") is not None
            ):
                raise RepositoryPostureError(
                    f"non-disabled Gate 6 acceptance evidence is checked in: {relative}"
                )
    # The list is deliberately empty on success.  Returning every JSON file
    # would make CI output noisy and could be mistaken for evidence inventory.
    return []


def check_repository_posture(repo_root: Path = REPO_ROOT) -> dict[str, Any]:
    """Run all no-approval assertions and return a small audit summary."""

    repo_root = Path(repo_root).resolve()
    p0_template = repo_root / P0_TEMPLATE_RELATIVE
    if not p0_template.is_file():
        raise RepositoryPostureError(f"missing disabled P0 template: {p0_template}")
    _assert_disabled_p0_template(p0_template)
    p9 = _assert_p9_disabled_mode(repo_root)
    evidence_files = _assert_no_production_evidence(repo_root, p0_template)
    return {
        "p0_template": P0_TEMPLATE_RELATIVE.as_posix(),
        "p0_approval": False,
        "p9": p9,
        "production_evidence_files": evidence_files,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check Gate 6 engineering contract repository posture (no approval)."
    )
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    args = parser.parse_args(argv)
    try:
        result = check_repository_posture(args.repo_root)
    except (OSError, RepositoryPostureError) as exc:
        print(f"FAIL: Gate 6 engineering contract posture: {exc}", file=sys.stderr)
        return 1
    print(
        "PASS: Gate 6 engineering contract posture confirms no production approval "
        f"or P9 acceptance evidence: {json.dumps(result, ensure_ascii=False, sort_keys=True)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
