#!/usr/bin/env python3
"""Gate 1 F7 capability-freeze policy check.

The check compares capability-bearing source surfaces with a reviewed,
machine-readable baseline.  Additions fail closed unless an active exception
names the exact change and supplies an architecture owner, rationale, ticket,
approval date, and expiry date.

Only Python's standard library is used so this can run before project
dependencies are installed in CI.
"""

from __future__ import annotations

import argparse
import ast
import datetime as dt
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable


DEFAULT_BASELINE = "infra/policy/gate1/capability-freeze-baseline.json"
DEFAULT_EXCEPTIONS = "infra/policy/gate1/capability-freeze-exceptions.json"
SOURCE_SUFFIXES = {".py", ".js", ".jsx", ".ts", ".tsx", ".vue", ".yml", ".yaml"}


class PolicyError(RuntimeError):
    """A malformed or unreadable policy input."""


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PolicyError(f"required policy file is missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise PolicyError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise PolicyError(f"policy file must contain a JSON object: {path}")
    return value


def _json_object(text: str, source: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PolicyError(f"invalid JSON in {source}: {exc}") from exc
    if not isinstance(value, dict):
        raise PolicyError(f"policy source must contain a JSON object: {source}")
    return value


def enforce_trusted_baseline(
    working: dict[str, Any], trusted: dict[str, Any], source: str
) -> dict[str, Any]:
    """Reject a PR that tries to bless additions by editing its baseline."""
    if working != trusted:
        raise PolicyError(
            "capability baseline differs from trusted PR base "
            f"({source}); use an exact, active exception instead of editing the baseline"
        )
    return trusted


def _run_git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run a read-only git query or fail with a policy-level diagnostic."""

    try:
        return subprocess.run(
            ["git", *args],
            cwd=root,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise PolicyError(f"cannot execute git for trusted baseline: {exc}") from exc


def _baseline_for_run(
    root: Path,
    relative: str,
    base_ref: str | None,
    bootstrap_if_missing: bool,
) -> tuple[dict[str, Any], str | None]:
    working = _load_json(root / relative)
    if not base_ref:
        return working, None

    verify = _run_git(root, "rev-parse", "--verify", f"{base_ref}^{{commit}}")
    if verify.returncode != 0:
        raise PolicyError(
            f"trusted base ref is unavailable: {base_ref}; CI must fetch full base history"
        )

    object_name = f"{base_ref}:{relative}"
    exists = _run_git(root, "cat-file", "-e", object_name)
    if exists.returncode != 0:
        if bootstrap_if_missing:
            return working, (
                f"trusted base {base_ref} has no F7 baseline; bootstrap accepted for initial rollout"
            )
        raise PolicyError(
            f"trusted base {base_ref} has no {relative}; "
            "--bootstrap-if-missing is allowed only for the initial F7 rollout"
        )

    shown = _run_git(root, "show", object_name)
    if shown.returncode != 0:
        raise PolicyError(f"cannot read trusted baseline {object_name}: {shown.stderr.strip()}")
    trusted = _json_object(shown.stdout, object_name)
    return enforce_trusted_baseline(working, trusted, object_name), None


def _source(root: Path, relative: str) -> str:
    path = root / relative
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise PolicyError(f"controlled source is missing: {relative}") from exc
    except UnicodeDecodeError as exc:
        raise PolicyError(f"controlled source is not UTF-8: {relative}") from exc


def _python_tree(root: Path, relative: str) -> ast.Module:
    text = _source(root, relative)
    try:
        return ast.parse(text, filename=relative)
    except SyntaxError as exc:
        raise PolicyError(f"cannot parse controlled Python source {relative}: {exc}") from exc


def _literal_string(node: ast.AST) -> str | None:
    try:
        value = ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return None
    return value if isinstance(value, str) else None


def _is_repo_local_dependency(path: Path, root: Path) -> bool:
    """Return whether ``path`` is below a proven repository-local venv.

    Directory names such as ``venv`` are valid repository source names and
    cannot establish a dependency boundary.  Python virtual environments do
    carry ``pyvenv.cfg`` at their root on both POSIX and Windows, independently
    of whether dependencies live below ``lib`` or ``Lib``.  Only that marker
    is strong enough to exclude recursively discovered source.
    """

    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    ancestor = root
    for part in relative.parts[:-1]:
        ancestor /= part
        if (ancestor / "pyvenv.cfg").is_file():
            return True
    return False


def _enum_values(root: Path, spec: dict[str, Any]) -> set[str]:
    tree = _python_tree(root, spec["path"])
    class_name = spec["symbol"]
    target = next(
        (node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name),
        None,
    )
    if target is None:
        raise PolicyError(f"enum {class_name!r} not found in {spec['path']}")
    values: set[str] = set()
    for node in target.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        value_node = node.value
        if value_node is None:
            continue
        value = _literal_string(value_node)
        if value is not None:
            values.add(value)
    if not values:
        raise PolicyError(f"enum {class_name!r} has no string values in {spec['path']}")
    return values


def _python_modules(root: Path, spec: dict[str, Any]) -> set[str]:
    relative_root = spec["root"].rstrip("/")
    directory = root / relative_root
    if not directory.is_dir():
        raise PolicyError(f"controlled module directory is missing: {relative_root}")
    exclude_init = bool(spec.get("exclude_init", False))
    return {
        path.relative_to(root).as_posix()
        for path in directory.rglob("*.py")
        if path.is_file()
        and not _is_repo_local_dependency(path, root)
        and not (exclude_init and path.name == "__init__.py")
    }


def _iter_python_files(root: Path, spec: dict[str, Any]) -> Iterable[Path]:
    if "paths" in spec:
        for relative in spec["paths"]:
            path = root / relative
            if not path.is_file():
                raise PolicyError(f"controlled source is missing: {relative}")
            yield path
        return
    relative_root = spec["root"].rstrip("/")
    directory = root / relative_root
    if not directory.is_dir():
        raise PolicyError(f"controlled source directory is missing: {relative_root}")
    yield from (
        path
        for path in directory.rglob("*.py")
        if path.is_file() and not _is_repo_local_dependency(path, root)
    )


def _python_public_symbols(root: Path, spec: dict[str, Any]) -> set[str]:
    include_private = bool(spec.get("include_private", False))
    pattern = re.compile(spec["name_pattern"]) if spec.get("name_pattern") else None
    values: set[str] = set()
    for path in _iter_python_files(root, spec):
        relative = path.relative_to(root).as_posix()
        tree = _python_tree(root, relative)
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if not include_private and node.name.startswith("_"):
                continue
            if pattern is not None and not pattern.search(node.name):
                continue
            values.add(f"{relative}::{node.name}")
    return values


def _python_class_methods(root: Path, spec: dict[str, Any]) -> set[str]:
    tree = _python_tree(root, spec["path"])
    class_name = spec["symbol"]
    target = next(
        (node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name),
        None,
    )
    if target is None:
        raise PolicyError(f"class {class_name!r} not found in {spec['path']}")
    include_private = bool(spec.get("include_private", False))
    return {
        f"{class_name}.{node.name}"
        for node in target.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and (include_private or not node.name.startswith("_"))
    }


def _python_public_class_methods(root: Path, spec: dict[str, Any]) -> set[str]:
    values: set[str] = set()
    for path in _iter_python_files(root, spec):
        relative = path.relative_to(root).as_posix()
        tree = _python_tree(root, relative)
        for class_node in (node for node in tree.body if isinstance(node, ast.ClassDef)):
            for method in class_node.body:
                if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if method.name.startswith("_"):
                    continue
                values.add(f"{relative}::{class_node.name}.{method.name}")
    return values


def _python_all_exports(root: Path, spec: dict[str, Any]) -> set[str]:
    pattern = re.compile(spec["name_pattern"]) if spec.get("name_pattern") else None
    values: set[str] = set()
    for path in _iter_python_files(root, spec):
        relative = path.relative_to(root).as_posix()
        tree = _python_tree(root, relative)
        for node in tree.body:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if not any(isinstance(target, ast.Name) and target.id == "__all__" for target in targets):
                continue
            if node.value is None:
                continue
            try:
                exports = ast.literal_eval(node.value)
            except (ValueError, TypeError, SyntaxError) as exc:
                raise PolicyError(f"non-literal __all__ in controlled source {relative}") from exc
            if not isinstance(exports, (list, tuple)) or not all(isinstance(v, str) for v in exports):
                raise PolicyError(f"controlled __all__ must be a string list in {relative}")
            for export in exports:
                if pattern is None or pattern.search(export):
                    values.add(f"{relative}::{export}")
    return values


def _compose_declared_names(root: Path, spec: dict[str, Any]) -> set[str]:
    marker = spec["marker"]
    names: set[str] = set()
    for relative in spec["paths"]:
        lines = _source(root, relative).splitlines()
        for index, line in enumerate(lines):
            if marker not in line:
                continue
            marker_indent = len(line) - len(line.lstrip())
            block: list[str] = []
            for following in lines[index + 1 :]:
                stripped = following.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                indent = len(following) - len(following.lstrip())
                if indent <= marker_indent:
                    break
                block.append(stripped)
            names.update(re.findall(r'"name"\s*:\s*"([^"]+)"', "\n".join(block)))
    return names


def _glob_paths(root: Path, spec: dict[str, Any]) -> set[str]:
    values: set[str] = set()
    for pattern in spec["patterns"]:
        values.update(
            path.relative_to(root).as_posix()
            for path in root.glob(pattern)
            if path.is_file() and not _is_repo_local_dependency(path, root)
        )
    return values


def _fastapi_routes(root: Path, spec: dict[str, Any]) -> set[str]:
    values: set[str] = set()
    for relative in spec["paths"]:
        tree = _python_tree(root, relative)
        prefix = ""
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            if not any(isinstance(target, ast.Name) and target.id == "router" for target in node.targets):
                continue
            if not isinstance(node.value, ast.Call):
                continue
            for keyword in node.value.keywords:
                if keyword.arg == "prefix":
                    prefix = _literal_string(keyword.value) or ""
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
                    continue
                owner = decorator.func.value
                if not isinstance(owner, ast.Name) or owner.id != "router":
                    continue
                method = decorator.func.attr.upper()
                if method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"}:
                    continue
                route = _literal_string(decorator.args[0]) if decorator.args else ""
                if route is None:
                    raise PolicyError(f"non-literal controlled route in {relative}::{node.name}")
                values.add(f"{method} {prefix}{route}")
    return values


def _text_matched_files(root: Path, spec: dict[str, Any]) -> set[str]:
    token = re.compile(spec["pattern"], re.IGNORECASE if spec.get("ignore_case") else 0)
    values: set[str] = set()
    for relative_root in spec["roots"]:
        directory = root / relative_root
        if not directory.is_dir():
            raise PolicyError(f"controlled scan root is missing: {relative_root}")
        for path in directory.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in SOURCE_SUFFIXES:
                continue
            if _is_repo_local_dependency(path, root):
                continue
            relative = path.relative_to(root).as_posix()
            if any(part in {"tests", "test", "__pycache__", "node_modules", "dist"} for part in path.parts):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError as exc:
                raise PolicyError(f"controlled source is not UTF-8: {relative}") from exc
            if token.search(text):
                values.add(relative)
    return values


COLLECTORS = {
    "python_enum": _enum_values,
    "python_modules": _python_modules,
    "python_public_symbols": _python_public_symbols,
    "python_class_methods": _python_class_methods,
    "python_public_class_methods": _python_public_class_methods,
    "python_all_exports": _python_all_exports,
    "compose_declared_names": _compose_declared_names,
    "glob_paths": _glob_paths,
    "fastapi_routes": _fastapi_routes,
    "text_matched_files": _text_matched_files,
}


def collect_inventory(root: Path, baseline: dict[str, Any]) -> dict[str, list[str]]:
    if baseline.get("schema_version") != 1:
        raise PolicyError("capability baseline schema_version must be 1")
    surfaces = baseline.get("surfaces")
    if not isinstance(surfaces, list) or not surfaces:
        raise PolicyError("capability baseline must define a non-empty surfaces list")
    inventory: dict[str, list[str]] = {}
    seen: set[str] = set()
    for surface in surfaces:
        if not isinstance(surface, dict):
            raise PolicyError("each capability surface must be an object")
        surface_id = surface.get("id")
        if not isinstance(surface_id, str) or not surface_id or surface_id in seen:
            raise PolicyError(f"invalid or duplicate capability surface id: {surface_id!r}")
        seen.add(surface_id)
        collector_spec = surface.get("collector")
        if not isinstance(collector_spec, dict):
            raise PolicyError(f"surface {surface_id} has no collector object")
        collector_type = collector_spec.get("type")
        collector = COLLECTORS.get(collector_type)
        if collector is None:
            raise PolicyError(f"surface {surface_id} has unknown collector: {collector_type!r}")
        inventory[surface_id] = sorted(collector(root, collector_spec))
    return inventory


def _parse_date(value: Any, field: str, exception_id: str) -> dt.date:
    if not isinstance(value, str):
        raise PolicyError(f"exception {exception_id}: {field} must be YYYY-MM-DD")
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise PolicyError(f"exception {exception_id}: invalid {field} {value!r}") from exc


def validate_exceptions(document: dict[str, Any], as_of: dt.date) -> dict[str, dict[str, Any]]:
    if document.get("schema_version") != 1:
        raise PolicyError("exception registry schema_version must be 1")
    entries = document.get("exceptions")
    if not isinstance(entries, list):
        raise PolicyError("exception registry must contain an exceptions list")
    by_change: dict[str, dict[str, Any]] = {}
    ids: set[str] = set()
    placeholders = {"tbd", "todo", "unknown", "n/a", "none", "owner"}
    for entry in entries:
        if not isinstance(entry, dict):
            raise PolicyError("each capability exception must be an object")
        exception_id = entry.get("id")
        if not isinstance(exception_id, str) or not exception_id or exception_id in ids:
            raise PolicyError(f"invalid or duplicate capability exception id: {exception_id!r}")
        ids.add(exception_id)
        owner = entry.get("architecture_owner")
        if (
            not isinstance(owner, str)
            or len(owner.strip()) < 3
            or owner.strip().lower() in placeholders
        ):
            raise PolicyError(f"exception {exception_id}: architecture_owner must name a person")
        rationale = entry.get("rationale")
        if not isinstance(rationale, str) or len(rationale.strip()) < 20:
            raise PolicyError(f"exception {exception_id}: rationale must be written and specific")
        ticket = entry.get("ticket")
        if (
            not isinstance(ticket, str)
            or ticket.strip().lower() in placeholders
            or not re.search(r"(?:https?://\S+|[A-Z][A-Z0-9_-]+-\d+|GH-\d+|#\d+)", ticket)
        ):
            raise PolicyError(f"exception {exception_id}: ticket must be a URL or tracked ticket id")
        approved_on = _parse_date(entry.get("approved_on"), "approved_on", exception_id)
        expires_on = _parse_date(entry.get("expires_on"), "expires_on", exception_id)
        if approved_on > as_of:
            raise PolicyError(f"exception {exception_id}: approval date is in the future")
        if expires_on < as_of:
            raise PolicyError(f"exception {exception_id}: expired on {expires_on.isoformat()}")
        if expires_on <= approved_on:
            raise PolicyError(f"exception {exception_id}: expiry must be after approval")
        changes = entry.get("changes")
        if not isinstance(changes, list) or not changes or not all(isinstance(v, str) and "::" in v for v in changes):
            raise PolicyError(f"exception {exception_id}: changes must name exact surface::value entries")
        for change in changes:
            if change in by_change:
                raise PolicyError(
                    f"change {change!r} is authorized by multiple exceptions: "
                    f"{by_change[change]['id']} and {exception_id}"
                )
            by_change[change] = entry
    return by_change


def evaluate(
    baseline: dict[str, Any],
    current: dict[str, list[str]],
    exceptions: dict[str, Any],
    as_of: dt.date,
) -> dict[str, Any]:
    authorized = validate_exceptions(exceptions, as_of)
    violations: list[dict[str, str]] = []
    approved: list[dict[str, str]] = []
    removals: list[dict[str, str]] = []
    reviewed_inventory = baseline.get("reviewed_inventory")
    if not isinstance(reviewed_inventory, dict):
        raise PolicyError("capability baseline must contain reviewed_inventory")
    surface_ids = {surface["id"] for surface in baseline["surfaces"]}
    for change in authorized:
        surface_id, value = change.split("::", 1)
        if surface_id not in surface_ids:
            raise PolicyError(f"exception references unknown capability surface: {surface_id}")
        allowed_values = reviewed_inventory.get(surface_id)
        if isinstance(allowed_values, list) and value in allowed_values:
            raise PolicyError(f"exception does not describe an addition: {change}")
    for surface in baseline["surfaces"]:
        surface_id = surface["id"]
        allowed = reviewed_inventory.get(surface_id)
        if not isinstance(allowed, list) or not all(isinstance(v, str) for v in allowed):
            raise PolicyError(f"surface {surface_id}: allowed must be a string list")
        baseline_values = set(allowed)
        current_values = set(current.get(surface_id, []))
        for value in sorted(current_values - baseline_values):
            change = f"{surface_id}::{value}"
            exception = authorized.get(change)
            item = {"surface": surface_id, "value": value, "change": change}
            if exception is None:
                violations.append(item)
            else:
                approved.append({**item, "exception": exception["id"]})
        for value in sorted(baseline_values - current_values):
            removals.append({"surface": surface_id, "value": value})
    return {
        "policy_id": baseline.get("policy_id"),
        "freeze_until_gate": baseline.get("freeze_until_gate"),
        "as_of": as_of.isoformat(),
        "passed": not violations,
        "violations": violations,
        "approved_exceptions": approved,
        "baseline_removals": removals,
        "inventory": current,
    }


def _print_human(result: dict[str, Any], warning: str | None = None) -> None:
    status = "PASS" if result["passed"] else "FAIL"
    print(f"[{status}] {result['policy_id']} (freeze through Gate {result['freeze_until_gate']})")
    if warning:
        print(f"  bootstrap warning: {warning}")
    for item in result["violations"]:
        print(f"  unauthorized addition: {item['change']}")
    for item in result["approved_exceptions"]:
        print(f"  approved exception {item['exception']}: {item['change']}")
    for item in result["baseline_removals"]:
        print(f"  baseline removal (non-blocking): {item['surface']}::{item['value']}")
    if result["passed"]:
        count = sum(len(values) for values in result["inventory"].values())
        print(f"  checked {len(result['inventory'])} surfaces / {count} inventory entries")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="repository root")
    parser.add_argument("--baseline", default=DEFAULT_BASELINE)
    parser.add_argument("--exceptions", default=DEFAULT_EXCEPTIONS)
    parser.add_argument(
        "--base-ref",
        help="trusted PR base Git ref; prevents the branch from rewriting its baseline",
    )
    parser.add_argument(
        "--bootstrap-if-missing",
        action="store_true",
        help="permit only the initial rollout when the trusted base has no F7 baseline",
    )
    parser.add_argument("--as-of", help="policy date override (YYYY-MM-DD; tests only)")
    parser.add_argument("--json", action="store_true", help="emit JSON result")
    parser.add_argument(
        "--inventory-only",
        action="store_true",
        help="print collected inventory without evaluating allowed values",
    )
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    try:
        baseline, bootstrap_warning = _baseline_for_run(
            root, args.baseline, args.base_ref, args.bootstrap_if_missing
        )
        current = collect_inventory(root, baseline)
        if args.inventory_only:
            print(json.dumps(current, ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        as_of = dt.date.fromisoformat(args.as_of) if args.as_of else dt.date.today()
        exceptions = _load_json(root / args.exceptions)
        result = evaluate(baseline, current, exceptions, as_of)
        if bootstrap_warning:
            result["bootstrap_warning"] = bootstrap_warning
    except (PolicyError, ValueError, KeyError, TypeError) as exc:
        if args.json:
            print(json.dumps({"passed": False, "policy_error": str(exc)}, ensure_ascii=False))
        else:
            print(f"[FAIL] capability freeze policy error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        _print_human(result, bootstrap_warning)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
