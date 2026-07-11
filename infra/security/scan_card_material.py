#!/usr/bin/env python3
"""Scan the worktree and every reachable Git ref for card/identity material.

The report never emits the matched value.  It records only a SHA-256 prefix,
length, location, and finding type so the output itself does not become a new
copy of personal data or credential material.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

try:
    from cryptography import x509
except ImportError:  # Fail closed: allowlisted CA material is not trusted blindly.
    x509 = None


MAX_TEXT_BYTES = 20 * 1024 * 1024
SYNTHETIC_EMPLOYEE_IDS = {"990000001", "990000002"}
PUBLIC_TRUST_ANCHOR_PATHS = {
    "services/csp/app/services/cspki_ca_bundle.pem",
}
CERTIFICATE_FILE_SUFFIXES = (".cer", ".crt", ".der", ".pem")
PEM_CERTIFICATE_BLOCK = re.compile(
    rb"-----BEGIN CERTIFICATE-----\s+[A-Za-z0-9+/=\r\n]{128,}"
    rb"-----END CERTIFICATE-----"
)
REMOTE_VERIFICATION_FAILURE_EXIT = 2


@dataclass(frozen=True)
class Pattern:
    kind: str
    severity: str
    regex: re.Pattern[str]
    group: int = 1


PATTERNS = (
    Pattern(
        "serialized_private_key",
        "critical",
        re.compile(
            r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
            r"\s+([A-Za-z0-9+/=\r\n]{128,})"
            r"-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
        ),
    ),
    Pattern(
        "institutional_email",
        "high",
        re.compile(r"\b([A-Z0-9._%+-]+@ncsist\.org\.tw)\b", re.IGNORECASE),
    ),
    Pattern(
        "employee_id",
        "high",
        re.compile(
            r"(?:employee[_ -]?id|subjectID|serialNumber|CARD_INITIAL_OWNERS|"
            r"X-ANILA-User-Id|user_identity|user_id)[^\r\n]{0,80}?"
            r"\b([0-9]{6,9})\b",
            re.IGNORECASE,
        ),
    ),
    Pattern(
        "employee_id_in_home_path",
        "high",
        re.compile(r"/home/(?:aia/)?c([0-9]{6,9})(?:/|\b)", re.IGNORECASE),
    ),
    Pattern(
        "card_serial",
        "high",
        re.compile(r"\b(CS[0-9]{8,})\b", re.IGNORECASE),
    ),
    Pattern(
        "certificate_subject_person_name",
        "high",
        re.compile(
            r"(?:subjectCN|CN)\s*[\"']?\s*[:=]\s*[\"']?"
            r"([\u4e00-\u9fff]{2,4})(?=[,\s\"'])",
            re.IGNORECASE,
        ),
    ),
    Pattern(
        "embedded_cms_signature",
        "high",
        re.compile(
            r"(?:signature|signature_b64)\s*[\"']?\s*[:=]\s*[\"']"
            r"([A-Za-z0-9+/]{256,}={0,2})",
            re.IGNORECASE,
        ),
    ),
    Pattern(
        "embedded_certificate",
        "high",
        re.compile(
            r"(?:certb64|certificate_b64|certificate)\s*[\"']?\s*[:=]\s*[\"']"
            r"([A-Za-z0-9+/]{256,}={0,2})",
            re.IGNORECASE,
        ),
    ),
)


def git(root: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    return subprocess.check_output(
        ["git", *args], cwd=root, input=input_bytes, stderr=subprocess.PIPE
    )


def redacted_value(value: str | bytes) -> dict[str, str | int]:
    raw = value.encode("utf-8") if isinstance(value, str) else value
    return {
        "value_sha256": hashlib.sha256(raw).hexdigest()[:16],
        "value_length": len(raw),
    }


def finding(
    *,
    scope: str,
    path: str,
    kind: str,
    severity: str,
    value: str | bytes,
    line: int | None = None,
    object_id: str | None = None,
) -> dict:
    item = {
        "scope": scope,
        "path": path,
        "line": line,
        "kind": kind,
        "severity": severity,
        **redacted_value(value),
    }
    if object_id:
        item["object_id"] = object_id
    return item


def _load_certificates(data: bytes) -> list:
    """Parse a PEM bundle or one DER certificate; invalid input is untrusted."""
    if x509 is None:
        return []
    try:
        pem_blocks = PEM_CERTIFICATE_BLOCK.findall(data)
        if pem_blocks:
            return [x509.load_pem_x509_certificate(block) for block in pem_blocks]
        return [x509.load_der_x509_certificate(data)]
    except (TypeError, ValueError):
        return []


def _is_public_ca_bundle(data: bytes) -> bool:
    """True only when every parsed certificate explicitly declares CA=true."""
    certificates = _load_certificates(data)
    if not certificates:
        return False
    try:
        return all(
            certificate.extensions.get_extension_for_class(
                x509.BasicConstraints
            ).value.ca
            for certificate in certificates
        )
    except (AttributeError, x509.ExtensionNotFound):
        return False


def _certificate_material_kind(path: str, data: bytes) -> str | None:
    """Conservatively identify certificate containers before binary skipping."""
    lower_path = path.lower()
    if lower_path.endswith(CERTIFICATE_FILE_SUFFIXES):
        return "certificate_file"
    if PEM_CERTIFICATE_BLOCK.search(data):
        return "pem_certificate"
    # DER X.509 starts with an ASN.1 SEQUENCE. Parsing avoids labeling every
    # arbitrary NUL-containing binary as a certificate while still catching a
    # renamed DER blob with no certificate extension.
    if len(data) <= MAX_TEXT_BYTES and data.startswith(b"\x30"):
        if _load_certificates(data):
            return "der_certificate"
    return None


def scan_bytes(
    *, scope: str, path: str, data: bytes, object_id: str | None = None
) -> tuple[list[dict], list[dict], str | None]:
    findings: list[dict] = []
    allowed: list[dict] = []
    lower_path = path.lower()

    key_container_path = lower_path.endswith((".p12", ".pfx", ".key"))
    certificate_kind = _certificate_material_kind(path, data)

    if certificate_kind:
        if (
            path in PUBLIC_TRUST_ANCHOR_PATHS
            and len(data) <= MAX_TEXT_BYTES
            and _is_public_ca_bundle(data)
        ):
            allowed.append(
                {
                    "scope": scope,
                    "path": path,
                    "line": None,
                    "kind": "public_ca_trust_anchor",
                    **redacted_value(data),
                    **({"object_id": object_id} if object_id else {}),
                }
            )
        else:
            findings.append(
                finding(
                    scope=scope,
                    path=path,
                    kind=certificate_kind,
                    severity="high",
                    value=data,
                    object_id=object_id,
                )
            )

    if len(data) > MAX_TEXT_BYTES:
        if key_container_path:
            findings.append(
                finding(
                    scope=scope,
                    path=path,
                    kind="key_container_file",
                    severity="critical",
                    value=data,
                    object_id=object_id,
                )
            )
        return findings, allowed, "oversize"
    if b"\x00" in data[:8192]:
        if key_container_path:
            findings.append(
                finding(
                    scope=scope,
                    path=path,
                    kind="key_container_file",
                    severity="critical",
                    value=data,
                    object_id=object_id,
                )
            )
        return findings, allowed, "binary"

    text = data.decode("utf-8", errors="replace")
    for spec in PATTERNS:
        for match in spec.regex.finditer(text):
            value = match.group(spec.group)
            if spec.kind == "employee_id" and value in SYNTHETIC_EMPLOYEE_IDS:
                continue
            line = text.count("\n", 0, match.start(spec.group)) + 1
            findings.append(
                finding(
                    scope=scope,
                    path=path,
                    line=line,
                    kind=spec.kind,
                    severity=spec.severity,
                    value=value,
                    object_id=object_id,
                )
            )
    if key_container_path and not any(
        item["kind"] == "serialized_private_key" for item in findings
    ):
        findings.append(
            finding(
                scope=scope,
                path=path,
                kind="key_container_file",
                severity="critical",
                value=data,
                object_id=object_id,
            )
        )
    return findings, allowed, None


def worktree_paths(root: Path) -> Iterable[str]:
    seen: set[str] = set()
    commands = (
        ("ls-files", "-z", "--cached", "--others", "--exclude-standard"),
        # gitignore is not a security boundary. Explicitly inspect ignored key
        # and certificate containers without walking node_modules/venvs.
        (
            "ls-files",
            "-z",
            "--others",
            "--ignored",
            "--exclude-standard",
            "--",
            "*.key",
            "*.pem",
            "*.pfx",
            "*.p12",
            "*.cer",
            "*.der",
            "*.crt",
        ),
    )
    excluded_parts = {".git", ".venv", "venv", "node_modules", ".pytest_cache"}
    for command in commands:
        raw = git(root, *command)
        for entry in raw.split(b"\0"):
            if not entry:
                continue
            rel = entry.decode("utf-8", errors="surrogateescape").replace("\\", "/")
            if any(part in excluded_parts for part in Path(rel).parts):
                continue
            if rel not in seen:
                seen.add(rel)
                yield rel


def scan_worktree(root: Path) -> tuple[list[dict], list[dict], dict[str, int]]:
    findings: list[dict] = []
    allowed: list[dict] = []
    stats = {"files": 0, "binary_skipped": 0, "oversize_skipped": 0}
    for rel in worktree_paths(root):
        path = root / rel
        if not path.is_file():
            continue
        stats["files"] += 1
        file_findings, file_allowed, skipped = scan_bytes(
            scope="current", path=rel, data=path.read_bytes()
        )
        findings.extend(file_findings)
        allowed.extend(file_allowed)
        if skipped:
            stats[f"{skipped}_skipped"] += 1
    return findings, allowed, stats


def reachable_objects(root: Path) -> tuple[dict[str, str], list[str]]:
    objects: dict[str, str] = {}
    for line in git(root, "rev-list", "--objects", "--all").decode().splitlines():
        object_id, _, path = line.partition(" ")
        objects.setdefault(object_id, path.replace("\\", "/"))
    refs = (
        git(
            root,
            "for-each-ref",
            "--format=%(refname)",
            "refs/heads",
            "refs/remotes",
            "refs/tags",
        )
        .decode()
        .splitlines()
    )
    return objects, sorted(set(refs))


def scan_history(
    root: Path,
) -> tuple[list[dict], list[dict], dict[str, int], list[str]]:
    objects, refs = reachable_objects(root)
    findings: list[dict] = []
    allowed: list[dict] = []
    stats = {
        "objects": len(objects),
        "blobs": 0,
        "binary_skipped": 0,
        "oversize_skipped": 0,
    }

    process = subprocess.Popen(
        ["git", "cat-file", "--batch"],
        cwd=root,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdin is not None and process.stdout is not None
    try:
        for object_id, path in objects.items():
            process.stdin.write(object_id.encode("ascii") + b"\n")
            process.stdin.flush()
            header = process.stdout.readline().decode("ascii", errors="replace").strip()
            parts = header.split()
            if len(parts) != 3 or parts[1] == "missing":
                continue
            _, object_type, size_text = parts
            size = int(size_text)
            data = process.stdout.read(size)
            process.stdout.read(1)  # protocol newline
            if object_type != "blob":
                continue
            stats["blobs"] += 1
            blob_path = path or "<unknown-path>"
            blob_findings, blob_allowed, skipped = scan_bytes(
                scope="history",
                path=blob_path,
                data=data,
                object_id=object_id,
            )
            findings.extend(blob_findings)
            allowed.extend(blob_allowed)
            if skipped:
                stats[f"{skipped}_skipped"] += 1
    finally:
        process.stdin.close()
        process.wait(timeout=30)

    affected = {item["object_id"] for item in findings if "object_id" in item}
    refs_by_object: dict[str, list[str]] = {object_id: [] for object_id in affected}
    for ref in refs:
        reachable = {
            line.split(" ", 1)[0]
            for line in git(root, "rev-list", "--objects", ref).decode().splitlines()
        }
        for object_id in affected & reachable:
            refs_by_object[object_id].append(ref)
    for item in findings:
        item["reachable_from"] = refs_by_object.get(item.get("object_id", ""), [])
    return findings, allowed, stats, refs


def verify_remote_refs(root: Path, remote: str) -> dict:
    """Read-only comparison; does not fetch, update refs, or alter the worktree."""
    try:
        remote_lines = git(root, "ls-remote", "--heads", "--tags", remote).decode().splitlines()
    except subprocess.CalledProcessError as exc:
        return {"remote": remote, "status": "unavailable", "error_code": exc.returncode}

    local_lines = (
        git(
            root,
            "for-each-ref",
            "--format=%(objectname)%09%(refname)",
            f"refs/remotes/{remote}",
            "refs/tags",
        )
        .decode()
        .splitlines()
    )
    local = {ref: object_id for object_id, ref in (line.split("\t", 1) for line in local_lines)}
    missing: list[str] = []
    drifted: list[str] = []
    compared = 0
    for line in remote_lines:
        object_id, ref = line.split("\t", 1)
        if ref.endswith("^{}"):
            continue
        compared += 1
        local_ref = (
            f"refs/remotes/{remote}/{ref.removeprefix('refs/heads/')}"
            if ref.startswith("refs/heads/")
            else ref
        )
        if local_ref not in local:
            missing.append(ref)
        elif local[local_ref] != object_id:
            drifted.append(ref)
    return {
        "remote": remote,
        "status": "synchronized" if not missing and not drifted else "out_of_sync",
        "refs_compared": compared,
        "missing_local_tracking_refs": missing,
        "drifted_local_tracking_refs": drifted,
    }


def determine_exit_code(
    *,
    blocking: list[dict],
    no_fail_current: bool,
    remote_verification: dict | None,
) -> int:
    """Return a fail-closed process status for findings and remote coverage."""
    if remote_verification is not None:
        if remote_verification.get("status") != "synchronized":
            return REMOTE_VERIFICATION_FAILURE_EXIT
    if blocking and not no_fail_current:
        return 1
    return 0


def deduplicate(items: list[dict]) -> list[dict]:
    unique: dict[tuple, dict] = {}
    for item in items:
        key = (
            item.get("scope"),
            item.get("path"),
            item.get("line"),
            item.get("kind"),
            item.get("value_sha256"),
            item.get("object_id"),
        )
        unique[key] = item
    return sorted(
        unique.values(),
        key=lambda x: (
            x.get("scope", ""),
            x.get("severity", ""),
            x.get("path", ""),
            x.get("line") or 0,
            x.get("kind", ""),
        ),
    )


def markdown(report: dict) -> str:
    summary = report["summary"]
    lines = [
        "# Card and identity material audit",
        "",
        f"Generated: `{report['generated_at']}`",
        "",
        "This report is redacted by construction: matched values are represented only "
        "by SHA-256 prefixes and byte lengths.",
        "",
        "## Summary",
        "",
        f"- Current-tree blocking candidates: **{summary['current_blocking']}**",
        f"- Current-tree total candidates: **{summary['current_total']}**",
        f"- Reachable-history candidates: **{summary['history_total']}**",
        f"- Reachable-history unique redacted values: **{summary['history_unique_values']}**",
        f"- Reachable-history affected blobs: **{summary['history_affected_blobs']}**",
        f"- Reachable-history critical blobs: **{summary['history_critical_blobs']}**",
        f"- Local/remote/tag refs scanned: **{summary['refs_scanned']}**",
        "",
        "## Current tree",
        "",
    ]
    if report.get("remote_verification"):
        remote = report["remote_verification"]
        lines[15:15] = [
            f"- Live remote comparison (`{remote['remote']}`): **{remote['status']}** "
            f"({remote.get('refs_compared', 0)} refs)",
        ]
    if report["current_findings"]:
        lines += [
            "| Severity | Kind | Path | Line | Value hash | Length |",
            "|---|---|---|---:|---|---:|",
        ]
        for item in report["current_findings"]:
            lines.append(
                f"| {item['severity']} | {item['kind']} | `{item['path']}` | "
                f"{item.get('line') or ''} | `{item['value_sha256']}` | "
                f"{item['value_length']} |"
            )
    else:
        lines.append("No candidate personal card fixture or serialized private key was found.")

    lines += [
        "",
        "## Allowed public trust material",
        "",
        "The production CSPKI bundle contains public CA certificates only. It remains "
        "the production trust anchor and is not a private-key fixture.",
        "",
        "| Scope | Kind | Path | Value hash |",
        "|---|---|---|---|",
    ]
    for item in report["allowed_material"]:
        lines.append(
            f"| {item['scope']} | {item['kind']} | `{item['path']}` | "
            f"`{item['value_sha256']}` |"
        )

    lines += [
        "",
        "## Reachable Git history",
        "",
        "History findings are evidence only. This scanner does not rewrite objects, "
        "delete refs, force-push, revoke cards, or rotate keys.",
        "",
        "| Severity | Kind | Path | Blob | Refs | Value hash |",
        "|---|---|---|---|---:|---|",
    ]
    for item in report["history_findings"][:250]:
        lines.append(
            f"| {item['severity']} | {item['kind']} | `{item['path']}` | "
            f"`{item['object_id'][:12]}` | {len(item.get('reachable_from', []))} | "
            f"`{item['value_sha256']}` |"
        )
    if len(report["history_findings"]) > 250:
        lines.append(
            f"\nOnly the first 250 rows are rendered here; the JSON report contains all "
            f"{len(report['history_findings'])} candidates."
        )

    lines += [
        "",
        "### Refs scanned",
        "",
        *[f"- `{ref}`" for ref in report["refs"]],
        "",
        "## Separately authorized incident / rewrite steps",
        "",
        "1. Security/data owners validate each redacted candidate and open an incident "
        "record before destructive cleanup.",
        "2. If any private key or active credential is confirmed, revoke/rotate it first; "
        "history cleanup does not make a leaked credential safe.",
        "3. Obtain explicit approval for a coordinated `git filter-repo` rewrite of every "
        "affected branch and tag, followed by protected force-pushes.",
        "4. Expire or replace old clones, forks, mirrors, CI caches, release archives, and "
        "backups that retain the old object IDs; notify all developers to reclone.",
        "5. Re-run this scanner against the rewritten authoritative remote and preserve the "
        "redacted before/after reports as incident evidence.",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--scope", choices=("current", "history", "all"), default="all")
    parser.add_argument("--json", type=Path)
    parser.add_argument("--markdown", type=Path)
    parser.add_argument(
        "--verify-remote",
        metavar="REMOTE",
        help="Read-only ls-remote comparison with local tracking refs (for example origin).",
    )
    parser.add_argument(
        "--no-fail-current",
        action="store_true",
        help="Report current high/critical findings without returning exit code 1.",
    )
    args = parser.parse_args(argv)
    root = Path(git(args.repo.resolve(), "rev-parse", "--show-toplevel").decode().strip())

    current_findings: list[dict] = []
    history_findings: list[dict] = []
    allowed: list[dict] = []
    refs: list[str] = []
    scan_stats: dict[str, dict] = {}

    if args.scope in ("current", "all"):
        current_findings, current_allowed, stats = scan_worktree(root)
        allowed.extend(current_allowed)
        scan_stats["current"] = stats
    if args.scope in ("history", "all"):
        history_findings, history_allowed, stats, refs = scan_history(root)
        allowed.extend(history_allowed)
        scan_stats["history"] = stats

    current_findings = deduplicate(current_findings)
    history_findings = deduplicate(history_findings)
    allowed = deduplicate(allowed)
    blocking = [
        item
        for item in current_findings
        if item["severity"] in {"critical", "high"}
    ]
    report = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repository": str(root),
        "summary": {
            "current_blocking": len(blocking),
            "current_total": len(current_findings),
            "history_total": len(history_findings),
            "history_unique_values": len(
                {
                    (item["kind"], item["value_sha256"])
                    for item in history_findings
                }
            ),
            "history_affected_blobs": len(
                {item["object_id"] for item in history_findings}
            ),
            "history_critical_blobs": len(
                {
                    item["object_id"]
                    for item in history_findings
                    if item["severity"] == "critical"
                }
            ),
            "refs_scanned": len(refs),
        },
        "scan_stats": scan_stats,
        "refs": refs,
        "current_findings": current_findings,
        "history_findings": history_findings,
        "allowed_material": allowed,
    }
    if args.verify_remote:
        report["remote_verification"] = verify_remote_refs(root, args.verify_remote)

    rendered_json = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(rendered_json, encoding="utf-8")
    if args.markdown:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(markdown(report), encoding="utf-8")
    if not args.json and not args.markdown:
        sys.stdout.write(rendered_json)

    return determine_exit_code(
        blocking=blocking,
        no_fail_current=args.no_fail_current,
        remote_verification=report.get("remote_verification"),
    )


if __name__ == "__main__":
    raise SystemExit(main())
