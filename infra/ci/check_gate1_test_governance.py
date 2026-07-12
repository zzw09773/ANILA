#!/usr/bin/env python3
"""Validate Gate 1 F1/F3 required-suite and quarantine governance."""

from __future__ import annotations

import argparse
import ast
import datetime as dt
import json
import re
import sys
from pathlib import Path


REQUIRED_SUITES = {
    "anila-contracts",
    "anila-security",
    "anila-core-unit",
    "anila-agent-unit",
    "ingestion-worker-unit",
    "csp-backend",
    "postgres-rls",
    "csp-governance-ui",
    "anila-shell",
    "anilalm",
    "contract-smoke",
    "capability-freeze",
    "deployment-posture",
    "gate2-pilot-policy",
    "studio-auth-revocation",
    "flux-agent-focused",
}
TEST_ROOTS = (
    "packages/anila-contracts/tests",
    "packages/anila-security/tests",
    "packages/anila-core/tests",
    "packages/anila-agent/tests",
    "services/ingestion-worker/tests",
    "services/csp/tests",
    "infra/policy/tests",
)
REQUIRED_WORKFLOW = ".github/workflows/gate1-ci.yml"
CLASSIFICATIONS = {"functional", "platform", "test-staleness"}
HISTORICAL_CSP_RESULT = {
    "failed": 38,
    "passed": 681,
    "skipped": 1,
    "errors": 2,
}
PLACEHOLDERS = {"", "tbd", "todo", "unknown", "none", "n/a", "owner"}
SKIP_APIS = {
    "pytest.skip",
    "pytest.importorskip",
    "pytest.mark.skip",
    "pytest.mark.skipif",
}


class GovernanceError(RuntimeError):
    pass


def _dotted_name(node: ast.AST) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def find_xfail_calls(root: Path) -> list[str]:
    hits: list[str] = []
    for relative_root in TEST_ROOTS:
        directory = root / relative_root
        if not directory.is_dir():
            raise GovernanceError(f"required test root missing: {relative_root}")
        for path in directory.rglob("*.py"):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except (SyntaxError, UnicodeDecodeError) as exc:
                raise GovernanceError(f"cannot inspect {path}: {exc}") from exc
            for node in ast.walk(tree):
                target = node.func if isinstance(node, ast.Call) else node
                if _dotted_name(target) in {"pytest.xfail", "pytest.mark.xfail"}:
                    hits.append(f"{path.relative_to(root).as_posix()}:{node.lineno}")
    return sorted(set(hits))


def collect_skip_calls(root: Path) -> dict[str, int]:
    calls: dict[str, int] = {}
    for relative_root in TEST_ROOTS:
        directory = root / relative_root
        if not directory.is_dir():
            raise GovernanceError(f"required test root missing: {relative_root}")
        for path in directory.rglob("*.py"):
            try:
                tree = ast.parse(
                    path.read_text(encoding="utf-8"), filename=str(path)
                )
            except (SyntaxError, UnicodeDecodeError) as exc:
                raise GovernanceError(f"cannot inspect {path}: {exc}") from exc
            relative = path.relative_to(root).as_posix()
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                kind = _dotted_name(node.func)
                if kind in SKIP_APIS:
                    key = f"{relative}::{kind}"
                    calls[key] = calls.get(key, 0) + 1
    return dict(sorted(calls.items()))


def _require_text(entry: dict, field: str, label: str) -> str:
    value = entry.get(field)
    if not isinstance(value, str) or value.strip().lower() in PLACEHOLDERS:
        raise GovernanceError(f"{label}: {field} must be explicit")
    return value.strip()


def _workflow_jobs(workflow_text: str) -> dict[str, str]:
    """Extract top-level job blocks without adding a YAML runtime dependency."""

    jobs_match = re.search(r"(?m)^jobs:\s*$", workflow_text)
    if jobs_match is None:
        raise GovernanceError("required workflow has no jobs mapping")
    jobs_text = workflow_text[jobs_match.end() :]
    matches = list(re.finditer(r"(?m)^  ([A-Za-z0-9_-]+):\s*$", jobs_text))
    if not matches:
        raise GovernanceError("required workflow has no job definitions")
    jobs: dict[str, str] = {}
    for index, match in enumerate(matches):
        job_id = match.group(1)
        end = matches[index + 1].start() if index + 1 < len(matches) else len(jobs_text)
        jobs[job_id] = jobs_text[match.start() : end]
    return jobs


def verify_required_skip_wiring(document: dict, workflow_text: str) -> None:
    """Prove every required skip guard has a concrete CI execution path."""

    jobs = _workflow_jobs(workflow_text)
    for entry in document["allowed_skips"]:
        if entry["required_execution"] is not True:
            continue
        key = f"{entry['path']}::{entry['kind']}"
        job_id = _require_text(entry, "ci_job", f"allowed skip {key}")
        fragment = _require_text(
            entry, "ci_command_fragment", f"allowed skip {key}"
        )
        job = jobs.get(job_id)
        if job is None:
            raise GovernanceError(
                f"allowed skip {key}: CI job {job_id!r} is not defined"
            )
        if fragment not in job:
            raise GovernanceError(
                f"allowed skip {key}: command fragment is not wired in job {job_id!r}"
            )


def validate_registry(document: dict, *, as_of: dt.date) -> None:
    if document.get("schema_version") != 1:
        raise GovernanceError("registry schema_version must be 1")

    suites = document.get("required_suites")
    if not isinstance(suites, list):
        raise GovernanceError("required_suites must be a list")
    suite_ids = {entry.get("id") for entry in suites if isinstance(entry, dict)}
    if len(suite_ids) != len(suites):
        raise GovernanceError("required_suites contains duplicate or invalid ids")
    if suite_ids != REQUIRED_SUITES:
        raise GovernanceError(
            f"required suite set mismatch; missing={sorted(REQUIRED_SUITES-suite_ids)}, "
            f"unexpected={sorted(suite_ids-REQUIRED_SUITES)}"
        )
    for entry in suites:
        label = f"suite {entry.get('id')!r}"
        _require_text(entry, "command", label)
        _require_text(entry, "evidence", label)
        if entry.get("required") is not True or entry.get("status") != "passing":
            raise GovernanceError(f"{label}: required suites must be passing and required")

    historical = document.get("historical_csp_baseline")
    if not isinstance(historical, dict):
        raise GovernanceError("historical_csp_baseline must be an object")
    _require_text(historical, "commit", "historical CSP baseline")
    _require_text(historical, "command", "historical CSP baseline")
    _require_text(historical, "evidence", "historical CSP baseline")
    if historical.get("result") != HISTORICAL_CSP_RESULT:
        raise GovernanceError(
            "historical CSP result must preserve the reproduced "
            "38 failed / 681 passed / 1 skipped / 2 errors baseline"
        )
    historical_groups = historical.get("outcome_groups")
    if not isinstance(historical_groups, list) or not historical_groups:
        raise GovernanceError("historical CSP outcome_groups must be a non-empty list")
    historical_ids: set[str] = set()
    historical_cases: set[str] = set()
    historical_counts = {"failure": 0, "error": 0}
    for entry in historical_groups:
        if not isinstance(entry, dict):
            raise GovernanceError("historical CSP outcome groups must be objects")
        group_id = _require_text(entry, "id", "historical CSP outcome group")
        if group_id in historical_ids:
            raise GovernanceError(f"duplicate historical CSP group id: {group_id}")
        historical_ids.add(group_id)
        outcome = entry.get("outcome")
        if outcome not in historical_counts:
            raise GovernanceError(f"historical CSP group {group_id}: invalid outcome")
        if entry.get("classification") not in CLASSIFICATIONS:
            raise GovernanceError(
                f"historical CSP group {group_id}: invalid classification"
            )
        if entry.get("disposition") != "resolved":
            raise GovernanceError(
                f"historical CSP group {group_id}: baseline outcome is not resolved"
            )
        _require_text(entry, "cause", f"historical CSP group {group_id}")
        _require_text(entry, "resolution", f"historical CSP group {group_id}")
        cases = entry.get("cases")
        if not isinstance(cases, list) or not cases or not all(
            isinstance(value, str) and value.strip() for value in cases
        ):
            raise GovernanceError(
                f"historical CSP group {group_id}: cases must be a non-empty string list"
            )
        overlap = historical_cases.intersection(cases)
        if overlap:
            raise GovernanceError(
                f"historical CSP group {group_id}: duplicate cases {sorted(overlap)}"
            )
        historical_cases.update(cases)
        historical_counts[outcome] += len(cases)
    expected_outcome_counts = {
        "failure": HISTORICAL_CSP_RESULT["failed"],
        "error": HISTORICAL_CSP_RESULT["errors"],
    }
    if historical_counts != expected_outcome_counts:
        raise GovernanceError(
            "historical CSP outcome count mismatch; "
            f"expected={expected_outcome_counts}, actual={historical_counts}"
        )

    failures = document.get("observed_failures")
    if not isinstance(failures, list):
        raise GovernanceError("observed_failures must be a list")
    seen_ids: set[str] = set()
    seen_cases: set[str] = set()
    for entry in failures:
        if not isinstance(entry, dict):
            raise GovernanceError("failure entries must be objects")
        failure_id = _require_text(entry, "id", "failure")
        if failure_id in seen_ids:
            raise GovernanceError(f"duplicate failure id: {failure_id}")
        seen_ids.add(failure_id)
        if entry.get("classification") not in CLASSIFICATIONS:
            raise GovernanceError(f"failure {failure_id}: invalid classification")
        if entry.get("disposition") != "resolved":
            raise GovernanceError(f"failure {failure_id}: required baseline is not resolved")
        _require_text(entry, "resolution", f"failure {failure_id}")
        cases = entry.get("cases")
        if not isinstance(cases, list) or not cases or not all(isinstance(v, str) for v in cases):
            raise GovernanceError(f"failure {failure_id}: cases must be a non-empty string list")
        overlap = seen_cases.intersection(cases)
        if overlap:
            raise GovernanceError(f"failure {failure_id}: duplicate cases {sorted(overlap)}")
        seen_cases.update(cases)

    quarantines = document.get("quarantine")
    if not isinstance(quarantines, list):
        raise GovernanceError("quarantine must be a list")
    for entry in quarantines:
        if not isinstance(entry, dict):
            raise GovernanceError("quarantine entries must be objects")
        nodeid = _require_text(entry, "nodeid", "quarantine")
        if entry.get("required") is not False:
            raise GovernanceError(f"quarantine {nodeid}: must be non-required")
        if entry.get("classification") not in CLASSIFICATIONS:
            raise GovernanceError(f"quarantine {nodeid}: invalid classification")
        owner = _require_text(entry, "owner", f"quarantine {nodeid}")
        if len(owner) < 3:
            raise GovernanceError(f"quarantine {nodeid}: owner must be named")
        ticket = _require_text(entry, "ticket", f"quarantine {nodeid}")
        if not re.search(r"(?:https?://\S+|[A-Z][A-Z0-9_-]+-\d+|GH-\d+|#\d+)", ticket):
            raise GovernanceError(f"quarantine {nodeid}: ticket must be trackable")
        expiry_raw = _require_text(entry, "expires_on", f"quarantine {nodeid}")
        try:
            expiry = dt.date.fromisoformat(expiry_raw)
        except ValueError as exc:
            raise GovernanceError(f"quarantine {nodeid}: invalid expires_on") from exc
        if expiry < as_of:
            raise GovernanceError(f"quarantine {nodeid}: expired on {expiry}")
        _require_text(entry, "reason", f"quarantine {nodeid}")

    allowed_skips = document.get("allowed_skips")
    if not isinstance(allowed_skips, list):
        raise GovernanceError("allowed_skips must be a list")
    seen_skip_keys: set[str] = set()
    for entry in allowed_skips:
        if not isinstance(entry, dict):
            raise GovernanceError("allowed skip entries must be objects")
        path = _require_text(entry, "path", "allowed skip")
        kind = _require_text(entry, "kind", f"allowed skip {path}")
        if kind not in SKIP_APIS:
            raise GovernanceError(f"allowed skip {path}: unsupported kind {kind}")
        key = f"{path}::{kind}"
        if key in seen_skip_keys:
            raise GovernanceError(f"duplicate allowed skip: {key}")
        seen_skip_keys.add(key)
        if not isinstance(entry.get("count"), int) or entry["count"] < 1:
            raise GovernanceError(f"allowed skip {key}: count must be a positive integer")
        _require_text(entry, "reason", f"allowed skip {key}")
        if entry.get("required_execution") not in {True, False}:
            raise GovernanceError(f"allowed skip {key}: required_execution must be boolean")
        if entry["required_execution"] is True:
            _require_text(entry, "ci_job", f"allowed skip {key}")
            _require_text(entry, "ci_command_fragment", f"allowed skip {key}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".")
    parser.add_argument("--registry", default="infra/ci/gate1-test-baseline.json")
    parser.add_argument("--as-of", help="YYYY-MM-DD override for tests")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    try:
        document = json.loads((root / args.registry).read_text(encoding="utf-8"))
        as_of = dt.date.fromisoformat(args.as_of) if args.as_of else dt.date.today()
        validate_registry(document, as_of=as_of)
        workflow_text = (root / REQUIRED_WORKFLOW).read_text(encoding="utf-8")
        verify_required_skip_wiring(document, workflow_text)
        xfails = find_xfail_calls(root)
        if xfails:
            raise GovernanceError("permanent xfail is forbidden: " + ", ".join(xfails))
        actual_skips = collect_skip_calls(root)
        allowed_skips = {
            f"{entry['path']}::{entry['kind']}": entry["count"]
            for entry in document["allowed_skips"]
        }
        if actual_skips != allowed_skips:
            raise GovernanceError(
                "pytest skip baseline changed; "
                f"unregistered={sorted(set(actual_skips) - set(allowed_skips))}, "
                f"removed={sorted(set(allowed_skips) - set(actual_skips))}, "
                "count_changes="
                f"{sorted(k for k in set(actual_skips) & set(allowed_skips) if actual_skips[k] != allowed_skips[k])}"
            )
    except (OSError, json.JSONDecodeError, ValueError, GovernanceError) as exc:
        print(f"[FAIL] Gate 1 test governance: {exc}", file=sys.stderr)
        return 1
    print(
        "[PASS] Gate 1 test governance: "
        f"{len(document['required_suites'])} required suites, "
        "38 historical CSP failures + 2 errors classified, "
        f"{sum(len(x['cases']) for x in document['observed_failures'])} resolved cases, "
        f"{len(document['quarantine'])} quarantines, "
        f"{sum(collect_skip_calls(root).values())} registered skip callsites, 0 xfail"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
