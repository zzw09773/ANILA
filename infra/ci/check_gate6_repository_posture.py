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
import os
import re
import subprocess
import sys
import unicodedata
from collections.abc import Iterator, Sequence
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
P9_EVIDENCE_SCHEMA = "anila.gate6.p9.enabled-inference-callsite-evidence.v1"

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
_GATE6_SCOPED_SCAN_MAX_BYTES = 1024 * 1024
_DISABLED_P0_TEMPLATE_SENTINEL: dict[str, Any] = {
    "schema_version": "anila.gate6.production-acceptance.v1",
    "profile_id": "gate6-production-acceptance-template",
    "profile_version": "template-1",
    "enabled": False,
    "template_only": True,
    "approval_status": "disabled_template",
    "production_topology": None,
    "enabled_features": [],
    "disabled_features": [],
    "data_classification_ceiling": None,
    "rto_rpo": None,
    "slo_thresholds": None,
    "load_profile": None,
    "observation_window": None,
    "workflow_matrix": [],
    "p5_sample_n": None,
    "enabled_inference_callsite_inventory": None,
    "revocation_sla": None,
    "pki_policy": None,
    "severity_taxonomy": None,
    "finding_acceptance_rule": None,
    "revalidation_impact_matrix": None,
    "valid_from": None,
    "valid_until": None,
    "signer_roles": [
        "system_owner",
        "data_owner",
        "pki_owner",
        "security",
        "operations",
    ],
    "profile_content_sha256": None,
    "signatures": [],
}


class RepositoryPostureError(RuntimeError):
    """Raised when repository material could be mistaken for approval evidence."""


class _MalformedJsonMaterialError(RepositoryPostureError):
    """Raised for JSON syntax/encoding violations, distinct from I/O failures."""


def _import_local_packages(repo_root: Path) -> None:
    """Make the editable source package and namespace policies importable."""

    for import_root in (repo_root, repo_root / "packages/anila-security/src"):
        value = str(import_root)
        if import_root.is_dir() and value not in sys.path:
            sys.path.insert(0, value)


def _parse_json_text(path: Path, text: str, *, line_number: int | None = None) -> Any:
    location = str(path)
    if line_number is not None:
        location = f"{location} line {line_number}"

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise _MalformedJsonMaterialError(
                    f"duplicate JSON key {key!r} in {location}"
                )
            result[key] = value
        return result

    def reject_non_standard_constant(constant: str) -> Any:
        raise _MalformedJsonMaterialError(
            f"invalid JSON material: {location} (non-standard constant {constant!r})"
        )

    try:
        return json.loads(
            text,
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_non_standard_constant,
        )
    except json.JSONDecodeError as exc:
        raise _MalformedJsonMaterialError(
            f"invalid JSON material: {location}"
        ) from exc


def _read_json_value(path: Path) -> Any:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise RepositoryPostureError(f"cannot read JSON material: {path}") from exc
    if raw.startswith(b"\xef\xbb\xbf"):
        raise _MalformedJsonMaterialError(
            f"UTF-8 BOM is forbidden in JSON material: {path}"
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _MalformedJsonMaterialError(f"invalid JSON material: {path}") from exc
    return _parse_json_text(path, text)


def _read_json(path: Path) -> dict[str, Any]:
    value = _read_json_value(path)
    if not isinstance(value, dict):
        raise RepositoryPostureError(f"JSON root must be an object: {path}")
    return value


def _read_json_like(path: Path) -> tuple[Any, ...]:
    """Read a JSON/JSONL file without weakening parse or duplicate-key checks."""

    if path.suffix.lower() != ".jsonl":
        return (_read_json_value(path),)

    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise RepositoryPostureError(f"cannot read JSON material: {path}") from exc
    if raw.startswith(b"\xef\xbb\xbf"):
        raise _MalformedJsonMaterialError(
            f"UTF-8 BOM is forbidden in JSON material: {path}"
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _MalformedJsonMaterialError(f"invalid JSON material: {path}") from exc

    values: list[Any] = []
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            raise _MalformedJsonMaterialError(
                f"invalid JSONL material: {path} line {line_number}"
            )
        values.append(_parse_json_text(path, line, line_number=line_number))
    return tuple(values)


def _walk_json(value: Any) -> Iterator[Any]:
    """Yield every JSON node, including object member names, recursively."""

    yield value
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _walk_json(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_json(item)


def _contains_key(value: Any, key: str) -> bool:
    return any(isinstance(item, dict) and key in item for item in _walk_json(value))


def _contains_pem_marker(value: Any) -> bool:
    return any(
        isinstance(item, str) and _PEM_MARKER_RE.search(item) is not None
        for item in _walk_json(value)
    )


def _assert_no_sensitive_json_material(value: Any, location: str) -> None:
    if _contains_key(value, "trusted_signers"):
        raise RepositoryPostureError(
            f"production trust-store material is checked in: {location}"
        )
    if _contains_pem_marker(value):
        raise RepositoryPostureError(f"PEM key/certificate material is checked in: {location}")


def _assert_no_scoped_text_pem_marker(path: Path, location: str) -> None:
    """Scan bounded Gate 6 material text without inspecting unrelated files."""

    try:
        size = path.stat().st_size
    except OSError as exc:
        raise RepositoryPostureError(
            f"cannot inspect Gate 6 scoped material: {location}"
        ) from exc
    if size > _GATE6_SCOPED_SCAN_MAX_BYTES:
        raise RepositoryPostureError(
            f"Gate 6 scoped material exceeds {_GATE6_SCOPED_SCAN_MAX_BYTES} bytes: {location}"
        )
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise RepositoryPostureError(
            f"cannot read Gate 6 scoped material: {location}"
        ) from exc
    if len(raw) > _GATE6_SCOPED_SCAN_MAX_BYTES:
        raise RepositoryPostureError(
            f"Gate 6 scoped material exceeds {_GATE6_SCOPED_SCAN_MAX_BYTES} bytes: {location}"
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RepositoryPostureError(
            f"invalid UTF-8 in Gate 6 scoped material: {location}"
        ) from exc
    if _PEM_MARKER_RE.search(text):
        raise RepositoryPostureError(f"PEM key/certificate material is checked in: {location}")


def _assert_disabled_p0_template(path: Path) -> dict[str, Any]:
    """Require the checked-in P0 file to be an explicit non-approval template."""

    profile = _read_json(path)
    _assert_no_sensitive_json_material(profile, str(path))
    if set(profile) != set(_DISABLED_P0_TEMPLATE_SENTINEL):
        missing = sorted(set(_DISABLED_P0_TEMPLATE_SENTINEL) - set(profile))
        unexpected = sorted(set(profile) - set(_DISABLED_P0_TEMPLATE_SENTINEL))
        raise RepositoryPostureError(
            "P0 disabled template sentinel fields differ: "
            f"missing={missing}, unexpected={unexpected}"
        )
    for field, value in _DISABLED_P0_TEMPLATE_SENTINEL.items():
        if profile.get(field) != value:
            raise RepositoryPostureError(
                f"P0 disabled template sentinel must set {field}={value!r}: {path}"
            )

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


def _decode_git_paths(output: bytes, *, repo_root: Path) -> tuple[Path, ...]:
    """Decode NUL-delimited Git paths and reject paths outside the scan root."""

    paths: list[Path] = []
    for raw in output.split(b"\0"):
        if not raw:
            continue
        relative = Path(raw.decode(sys.getfilesystemencoding(), "surrogateescape"))
        if relative.is_absolute() or ".." in relative.parts:
            raise RepositoryPostureError(
                f"Git returned a path outside repository root: {relative}"
            )
        paths.append(relative)
    return tuple(paths)


def _git_ls_files(repo_root: Path, *options: str) -> tuple[Path, ...]:
    """Return one Git path class or fail closed when enumeration fails."""

    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "ls-files", "-z", *options, "--"],
            check=False,
            capture_output=True,
        )
    except OSError as exc:
        raise RepositoryPostureError("cannot enumerate repository files with Git") from exc
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise RepositoryPostureError(
            f"cannot enumerate repository files with Git: {detail or 'unknown error'}"
        )
    return _decode_git_paths(result.stdout, repo_root=repo_root)


def _iter_git_repository_files(repo_root: Path) -> Sequence[Path] | None:
    """Use Git authority for tracked files and bounded non-generated untracked files."""

    try:
        probe = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--show-toplevel"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        if (repo_root / ".git").exists():
            raise RepositoryPostureError("cannot inspect Git repository root") from exc
        return None
    if probe.returncode != 0:
        if (repo_root / ".git").exists():
            raise RepositoryPostureError("cannot inspect Git repository root")
        return None
    try:
        git_root = Path(probe.stdout.strip()).resolve(strict=True)
    except OSError as exc:
        raise RepositoryPostureError("Git repository root is unavailable") from exc
    if git_root != repo_root.resolve():
        raise RepositoryPostureError(
            f"repository scan root must be the Git top-level: {git_root}"
        )

    tracked = _git_ls_files(repo_root, "--cached")
    untracked = _git_ls_files(repo_root, "--others", "--exclude-standard")
    relative_paths = [
        *tracked,
        *(
            path
            for path in untracked
            if not any(part in _SKIP_PARTS for part in path.parts)
        ),
    ]
    files: list[Path] = []
    seen: set[Path] = set()
    for relative in relative_paths:
        path = repo_root / relative
        if relative in seen or not path.is_file():
            continue
        seen.add(relative)
        files.append(path)
    return tuple(files)


def _iter_repository_files(repo_root: Path) -> Sequence[Path]:
    git_files = _iter_git_repository_files(repo_root)
    if git_files is not None:
        return git_files

    files: list[Path] = []
    for directory, directory_names, file_names in os.walk(repo_root):
        directory_names[:] = [
            name for name in directory_names if name not in _SKIP_PARTS
        ]
        files.extend(
            path
            for name in file_names
            if (path := Path(directory) / name).is_file()
        )
    return tuple(files)


def _contains_credential_path_marker(relative_path: str) -> bool:
    """Match explicit sensitive words after separator/camel/acronym splitting."""

    tokens = set(_path_tokens(relative_path))
    return not tokens.isdisjoint(_CREDENTIAL_MARKERS)


def _path_tokens(relative_path: str) -> tuple[str, ...]:
    """Return normalized lexical path tokens across separators and camel case."""

    normalized = unicodedata.normalize("NFKC", relative_path)
    return tuple(
        match.group(0).casefold() for match in _PATH_WORD_RE.finditer(normalized)
    )


def _is_gate6_scoped_path(relative_path: str) -> bool:
    """Recognize Gate 6 scope by adjacent lexical tokens, never substrings."""

    tokens = _path_tokens(relative_path)
    return "productionacceptance" in tokens or any(
        pair in {("gate", "6"), ("production", "acceptance")}
        for pair in zip(tokens, tokens[1:])
    )


def _assert_no_production_evidence(repo_root: Path, p0_template: Path) -> list[str]:
    """Reject checked-in P0 trust/signature/private material, fail closed."""

    for path in _iter_repository_files(repo_root):
        relative = path.relative_to(repo_root).as_posix()
        is_gate6_scoped = _is_gate6_scoped_path(relative)
        # Check the complete normalized relative path before any suffix-based
        # filtering.  Gate 6 and credential markers may be split across a
        # parent directory and a non-JSON child filename.
        if is_gate6_scoped and _contains_credential_path_marker(relative):
            raise RepositoryPostureError(
                f"Gate 6 production trust/signature material is checked in: {relative}"
            )
        if path.suffix.lower() in _MATERIAL_SUFFIXES and is_gate6_scoped:
            raise RepositoryPostureError(
                f"Gate 6 key/signature material is checked in: {relative}"
            )
        if is_gate6_scoped and path.suffix.lower() not in {".json", ".jsonl"}:
            _assert_no_scoped_text_pem_marker(path, relative)
        if path.suffix.lower() not in {".json", ".jsonl"}:
            continue
        try:
            payloads = _read_json_like(path)
        except _MalformedJsonMaterialError:
            if is_gate6_scoped or _contains_credential_path_marker(relative):
                raise
            continue
        for payload in payloads:
            _assert_no_sensitive_json_material(payload, relative)
            for profile in _walk_json(payload):
                if not isinstance(profile, dict):
                    continue
                schema_version = profile.get("schema_version")
                if schema_version == P9_EVIDENCE_SCHEMA:
                    raise RepositoryPostureError(
                        f"generated Gate 6 P9 evidence is checked in: {relative}"
                    )
                if schema_version != "anila.gate6.production-acceptance.v1":
                    continue
                if profile != _DISABLED_P0_TEMPLATE_SENTINEL:
                    raise RepositoryPostureError(
                        "non-canonical Gate 6 acceptance evidence is checked in: "
                        f"{relative}"
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
