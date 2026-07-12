#!/usr/bin/env python3
"""Create a redacted, reproducible Gate 0 deployment closeout bundle."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import ssl
import stat
import subprocess
import time
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[3]
AUDIT_JSON = ROOT / "docs/security/2026-07-10-card-material-audit.json"
INVENTORY = ROOT / "infra/deployment/intranet/platform-image-inventory.tsv"
POSTURE_ENV = (
    "ANILA_DEPLOYMENT_PROFILE", "ANILA_ENV", "ENABLE_CARD_LOGIN", "REQUIRE_CARD_LOGIN_ONLY",
    "ENABLE_PUBLIC_SHARE", "ENABLE_MEMORY", "ANILA_TRACE_ENDPOINT",
    "SITE_URL", "N8N_HOST", "N8N_EDITOR_BASE_URL", "N8N_WEBHOOK_URL",
    "GITLAB_HOST", "CODESERVER_HOST",
)


class CloseoutError(RuntimeError):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def run(command: list[str], *, check: bool = True, data: bytes | None = None):
    try:
        result = subprocess.run(
            command, cwd=ROOT, input=data, capture_output=True, check=False
        )
    except FileNotFoundError as exc:
        raise CloseoutError(f"required command not found: {command[0]}") from exc
    if check and result.returncode:
        error = result.stderr.decode("utf-8", errors="replace")[-2000:]
        raise CloseoutError(
            f"command failed ({result.returncode}): {' '.join(command)}\n{error}"
        )
    return result


def git_worktree_clean() -> bool:
    status = output(run(["git", "status", "--porcelain=v1", "--untracked-files=all"]))
    return not status.strip()


def output(result) -> str:
    return result.stdout.decode("utf-8", errors="replace")


def git_commit() -> str:
    return output(run(["git", "rev-parse", "HEAD"])).strip()


def external_directory(path: Path) -> Path:
    path = path.expanduser().resolve()
    repo = ROOT.resolve()
    if path == repo or repo in path.parents:
        raise CloseoutError(f"evidence must be outside the repository: {path}")
    path.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        path.chmod(0o700)
    return path


class EvidenceBundle:
    def __init__(self, directory: Path):
        self.directory = external_directory(directory)

    def write(self, name: str, payload: dict[str, Any]) -> None:
        path = self.directory / name
        body = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(body, encoding="utf-8")
        if os.name != "nt":
            temporary.chmod(0o600)
        temporary.replace(path)
        artifacts = []
        for artifact in sorted(self.directory.glob("*.json")):
            if artifact.name == "manifest.json":
                continue
            raw = artifact.read_bytes()
            artifacts.append({
                "file": artifact.name,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "bytes": len(raw),
            })
        manifest = {
            "schema_version": 1,
            "git_worktree_clean": git_worktree_clean(),
            "generated_at": now_iso(),
            "git_commit": git_commit(),
            "artifacts": artifacts,
        }
        manifest_path = self.directory / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if os.name != "nt":
            manifest_path.chmod(0o600)


def bundle_from(args) -> EvidenceBundle:
    if args.evidence_dir:
        return EvidenceBundle(Path(args.evidence_dir))
    state = os.environ.get("ANILA_STATE_DIR")
    if not state:
        raise CloseoutError("set ANILA_STATE_DIR or pass --evidence-dir")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return EvidenceBundle(Path(state) / "evidence/gate0-closeout" / stamp)


def compose_config() -> dict[str, Any]:
    return json.loads(output(run([
        "docker", "compose", "config", "--format", "json"
    ])))


def mount_projection(volume: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": volume.get("type"),
        "source": volume.get("source"),
        "target": volume.get("target"),
        "read_only": bool(volume.get("read_only", False)),
    }


def collect_profile(bundle: EvidenceBundle) -> dict[str, Any]:
    config = compose_config()
    services = config.get("services") or {}
    projection = {}
    for name, service in sorted(services.items()):
        environment = service.get("environment") or {}
        networks = service.get("networks") or {}
        projection[name] = {
            "image": service.get("image"),
            "user": service.get("user"),
            "profiles": service.get("profiles") or [],
            "ports": service.get("ports") or [],
            "networks": sorted(networks if isinstance(networks, list) else networks.keys()),
            "mounts": [mount_projection(v) for v in service.get("volumes") or []],
            "posture": {key: environment[key] for key in POSTURE_ENV if key in environment},
        }
    csp_env = (services.get("csp") or {}).get("environment") or {}
    router_env = (services.get("router") or {}).get("environment") or {}
    links = json.loads(csp_env.get("AUTO_REGISTER_LINKS", "[]"))
    links = [{"name": item.get("name"), "url": item.get("url")} for item in links]
    link_urls = {item["name"]: item["url"] for item in links}
    rw_repo = []
    for service_name, service in projection.items():
        for mount in service["mounts"]:
            if mount["type"] == "bind" and not mount["read_only"]:
                if Path(mount["source"]).resolve() == ROOT.resolve():
                    rw_repo.append({"service": service_name, **mount})
    gitlab_ports = (services.get("gitlab") or {}).get("ports") or []
    checks = {
        "codeserver_absent_from_default": "codeserver" not in services,
        "n8n_and_gitlab_present": {"n8n", "gitlab"}.issubset(services),
        "repo_root_has_no_rw_mount": not rw_repo,
        "memory_disabled": str(csp_env.get("ENABLE_MEMORY", "")).lower() == "false",
        "public_share_disabled": str(csp_env.get("ENABLE_PUBLIC_SHARE", "")).lower() == "false",
        "card_only": str(csp_env.get("REQUIRE_CARD_LOGIN_ONLY", "")).lower() == "true",
        "trace_endpoint_present": bool(router_env.get("ANILA_TRACE_ENDPOINT")),
        "independent_tool_links": all(str(link_urls.get(name, "")).startswith(prefix) for name, prefix in (
            ("n8n 工作流程", "https://n8n."), ("GitLab", "https://gitlab."),
            ("Code Server (按需)", "https://code."),
        )),
        "gitlab_ssh_interface_scoped": any(
            str(port.get("target")) == "22"
            and port.get("host_ip") not in (None, "", "0.0.0.0", "::")
            for port in gitlab_ports if isinstance(port, dict)
        ),
    }
    payload = {
        "schema_version": 1, "collected_at": now_iso(),
        "git_commit": git_commit(), "compose_project": config.get("name"),
        "services": projection, "registered_links": links,
        "rw_repo_mounts": rw_repo, "checks": checks,
        "passed": all(checks.values()),
        "redaction": "Expanded secrets and database URLs are omitted.",
    }
    bundle.write("formal-profile.json", payload)
    if not payload["passed"]:
        raise CloseoutError("formal profile checks failed: " + ", ".join(
            name for name, passed in checks.items() if not passed
        ))
    return payload


def mode_bits(path: Path) -> dict[str, Any]:
    info = path.stat()
    projection = {
        "path": str(path.resolve()), "mode": stat.filemode(info.st_mode),
        "mode_octal": oct(stat.S_IMODE(info.st_mode)),
        "uid": getattr(info, "st_uid", None), "gid": getattr(info, "st_gid", None),
        "is_symlink": path.is_symlink(),
    }
    if os.name == "nt":
        acl = output(run(["icacls", str(path.resolve())]))
        broad_names = (
            "BUILTIN\\Users", "NT AUTHORITY\\Authenticated Users", "Everyone",
        )
        projection["windows_acl"] = [line.strip() for line in acl.splitlines() if line.strip()]
        projection["broad_principals_present"] = any(
            principal in acl for principal in broad_names
        )
        projection["acl_restricted"] = not projection["broad_principals_present"]
    else:
        projection["acl_restricted"] = (
            stat.S_IMODE(info.st_mode) & (stat.S_IRWXG | stat.S_IRWXO)
        ) == 0
    return projection


def inspect_containers() -> list[dict[str, Any]]:
    ids = [line for line in output(run([
        "docker", "compose", "ps", "-a", "-q"
    ])).splitlines() if line]
    if not ids:
        raise CloseoutError("no compose containers exist")
    return json.loads(output(run(["docker", "inspect", *ids])))


def exec_csp(code: str, *, check: bool = True):
    return run(["docker", "compose", "exec", "-T", "csp", "python", "-c", code], check=check)


def collect_runtime(bundle: EvidenceBundle) -> dict[str, Any]:
    containers = []
    by_service = {}
    for item in inspect_containers():
        service = (item.get("Config", {}).get("Labels") or {}).get("com.docker.compose.service")
        mounts = [{
            "type": mount.get("Type"), "source": mount.get("Source"),
            "destination": mount.get("Destination"), "rw": bool(mount.get("RW")),
        } for mount in item.get("Mounts") or []]
        projected = {
            "name": item.get("Name", "").lstrip("/"), "service": service,
            "image": item.get("Config", {}).get("Image"), "image_id": item.get("Image"),
            "user": item.get("Config", {}).get("User"),
            "state": item.get("State", {}).get("Status"),
            "health": (item.get("State", {}).get("Health") or {}).get("Status"),
            "privileged": bool(item.get("HostConfig", {}).get("Privileged")),
            "readonly_rootfs": bool(item.get("HostConfig", {}).get("ReadonlyRootfs")),
            "cap_drop": item.get("HostConfig", {}).get("CapDrop") or [],
            "security_opt": item.get("HostConfig", {}).get("SecurityOpt") or [],
            "mounts": mounts,
        }
        containers.append(projected)
        if service:
            by_service[service] = projected

    csp_uid = int(output(run(["docker", "compose", "exec", "-T", "csp", "id", "-u"])).strip())
    csp_gid = int(output(run(["docker", "compose", "exec", "-T", "csp", "id", "-g"])).strip())
    exec_csp("from pathlib import Path; p=Path('/app/logs/.gate0-write-smoke'); p.write_text('ok'); p.unlink()")
    exec_csp("from pathlib import Path; p=Path('/var/anila/ingestion-uploads/.gate0-write-smoke'); p.write_text('ok'); p.unlink()")
    secret_write = exec_csp(
        "from pathlib import Path; Path('/app/secrets/.gate0-must-fail').write_text('bad')", check=False
    )

    csp_mounts = {m["destination"]: m for m in by_service.get("csp", {}).get("mounts", [])}
    nginx_mounts = {m["destination"]: m for m in by_service.get("nginx", {}).get("mounts", [])}
    rw_repo = []
    for container in containers:
        for mount in container["mounts"]:
            if mount["type"] == "bind" and mount["rw"] and Path(mount["source"]).resolve() == ROOT.resolve():
                rw_repo.append({"service": container["service"], **mount})
    acl = []
    for mount, leaves in (
        (csp_mounts.get("/app/secrets"), ("jwt-private.pem",)),
        (nginx_mounts.get("/etc/nginx/certs"), ("server.key",)),
    ):
        if not mount or not mount.get("source"):
            continue
        source = Path(mount["source"])
        if source.exists():
            acl.append(mode_bits(source))
        for leaf in leaves:
            candidate = source / leaf
            if candidate.exists():
                acl.append(mode_bits(candidate))

    expected = {"csp", "router", "ingestion-worker", "nginx", "n8n", "gitlab"}
    checks = {
        "required_services_exist": expected.issubset(by_service),
        "codeserver_absent": "codeserver" not in by_service,
        "csp_runs_non_root": csp_uid != 0,
        "csp_log_and_ingestion_mounts_writable": True,
        "csp_secret_mount_read_only": bool(csp_mounts.get("/app/secrets")) and not csp_mounts["/app/secrets"]["rw"],
        "csp_secret_write_rejected": secret_write.returncode != 0,
        "nginx_tls_mount_read_only": bool(nginx_mounts.get("/etc/nginx/certs")) and not nginx_mounts["/etc/nginx/certs"]["rw"],
        "host_private_material_acl_restricted": bool(acl) and all(
            item["acl_restricted"] for item in acl
        ),
        "repo_root_has_no_rw_mount": not rw_repo,
        "deployment_profile_declared": csp_env.get("ANILA_DEPLOYMENT_PROFILE")
        == "prod-intranet-card",
        "no_privileged_container": not any(item["privileged"] for item in containers),
    }
    payload = {
        "schema_version": 1, "collected_at": now_iso(), "git_commit": git_commit(),
        "csp_runtime": {"uid": csp_uid, "gid": csp_gid}, "containers": containers,
        "acl": acl, "rw_repo_mounts": rw_repo, "checks": checks,
        "passed": all(checks.values()),
    }
    bundle.write("runtime-mount-acl.json", payload)
    if not payload["passed"]:
        raise CloseoutError("runtime checks failed: " + ", ".join(
            name for name, passed in checks.items() if not passed
        ))
    return payload


def openssl(arguments: list[str], data: bytes | None = None) -> bytes:
    return run([os.environ.get("OPENSSL_BIN", "openssl"), *arguments], data=data).stdout


def private_key_spki(key_bytes: bytes) -> str:
    der = openssl(["pkey", "-pubout", "-outform", "DER"], key_bytes)
    return hashlib.sha256(der).hexdigest()


def cert_spki(cert: Path) -> str:
    return cert_spki_bytes(cert.read_bytes())


def cert_spki_bytes(cert_bytes: bytes) -> str:
    pem = openssl(["x509", "-noout", "-pubkey"], cert_bytes)
    der = openssl(["pkey", "-pubin", "-outform", "DER"], pem)
    return hashlib.sha256(der).hexdigest()


def cert_fingerprint(cert: Path) -> str:
    return cert_fingerprint_bytes(cert.read_bytes())


def cert_fingerprint_bytes(cert_bytes: bytes) -> str:
    raw = openssl(
        ["x509", "-noout", "-fingerprint", "-sha256"], cert_bytes
    )
    return raw.decode("ascii").strip().split("=", 1)[-1].replace(":", "").lower()


def tls_paths() -> tuple[Path, Path]:
    nginx = (compose_config().get("services") or {}).get("nginx") or {}
    mount = next((v for v in nginx.get("volumes") or []
                  if v.get("target") == "/etc/nginx/certs"), None)
    if not mount:
        raise CloseoutError("formal nginx TLS mount is missing")
    directory = Path(mount["source"])
    return directory / "server.crt", directory / "server.key"


def collect_tls(bundle: EvidenceBundle, label: str) -> dict[str, Any]:
    cert, key = tls_paths()
    if not cert.is_file() or cert.is_symlink() or not key.is_file() or key.is_symlink():
        raise CloseoutError("TLS cert/key must be regular non-symlink files")
    certificate_spki = cert_spki(cert)
    key_spki = private_key_spki(key.read_bytes())
    audit = json.loads(AUDIT_JSON.read_text(encoding="utf-8"))
    historical = []
    for finding in audit.get("history_findings", []):
        if finding.get("kind") != "serialized_private_key":
            continue
        object_id = finding["object_id"]
        historical.append({
            "path": finding.get("path"), "object_id": object_id,
            "redacted_value_sha256": finding.get("value_sha256"),
            "spki_sha256": private_key_spki(
                run(["git", "cat-file", "blob", object_id]).stdout
            ),
        })
    matched = [item for item in historical if item["spki_sha256"] == key_spki]
    checks = {
        "certificate_matches_private_key": certificate_spki == key_spki,
        "current_key_differs_from_all_historical_keys": not matched,
        "certificate_is_outside_repo": ROOT.resolve() not in cert.resolve().parents,
        "private_key_is_outside_repo": ROOT.resolve() not in key.resolve().parents,
    }
    payload = {
        "schema_version": 1, "collected_at": now_iso(), "git_commit": git_commit(),
        "phase": label,
        "certificate": {
            "path": str(cert.resolve()), "sha256_fingerprint": cert_fingerprint(cert),
            "spki_sha256": certificate_spki,
        },
        "private_key": {
            "path": str(key.resolve()), "spki_sha256": key_spki, "mode": mode_bits(key),
        },
        "historical_private_keys": historical, "matched_historical_keys": matched,
        "checks": checks, "passed": all(checks.values()),
        "note": "No private-key bytes are serialized in this evidence.",
    }
    bundle.write(f"tls-{label}.json", payload)
    if label != "before" and not payload["passed"]:
        raise CloseoutError("TLS checks failed: " + ", ".join(
            name for name, passed in checks.items() if not passed
        ))
    return payload


def collect_historical_tls_before(bundle: EvidenceBundle) -> dict[str, Any]:
    """Reconstruct redacted pre-rotation pairs from the audited Git objects."""

    audit = json.loads(AUDIT_JSON.read_text(encoding="utf-8"))
    keys: list[dict[str, str]] = []
    certificates: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for finding in audit.get("history_findings", []):
        kind = finding.get("kind")
        object_id = finding.get("object_id")
        if not object_id or (kind, object_id) in seen:
            continue
        seen.add((kind, object_id))
        raw = run(["git", "cat-file", "blob", object_id]).stdout
        if kind == "serialized_private_key":
            keys.append({
                "path": finding.get("path"),
                "object_id": object_id,
                "redacted_value_sha256": finding.get("value_sha256"),
                "spki_sha256": private_key_spki(raw),
            })
        elif kind in ("certificate_file", "pem_certificate"):
            certificates.append({
                "path": finding.get("path"),
                "object_id": object_id,
                "redacted_value_sha256": finding.get("value_sha256"),
                "sha256_fingerprint": cert_fingerprint_bytes(raw),
                "spki_sha256": cert_spki_bytes(raw),
            })
    pairs = [
        {"private_key": key, "certificate": cert}
        for key in keys
        for cert in certificates
        if key["spki_sha256"] == cert["spki_sha256"]
    ]
    checks = {
        "audited_historical_private_keys_present": bool(keys),
        "audited_historical_certificates_present": bool(certificates),
        "historical_cert_key_pairs_reconstructed": bool(pairs),
    }
    payload = {
        "schema_version": 1,
        "collected_at": now_iso(),
        "git_commit": git_commit(),
        "phase": "before-history",
        "source": str(AUDIT_JSON.relative_to(ROOT)),
        "historical_private_keys": keys,
        "historical_certificates": certificates,
        "historical_pairs": pairs,
        "checks": checks,
        "passed": all(checks.values()),
        "note": "Reconstructed from audited Git blobs; no private-key bytes are serialized.",
    }
    bundle.write("tls-before.json", payload)
    if not payload["passed"]:
        raise CloseoutError("historical TLS before evidence is incomplete")
    return payload


def tls_rotation_comparison(bundle: EvidenceBundle, before_file: Path, after_file: Path):
    before = json.loads(before_file.read_text(encoding="utf-8"))
    after = json.loads(after_file.read_text(encoding="utf-8"))
    historical_pairs = before.get("historical_pairs") or []
    if historical_pairs:
        before_key_spkis = sorted({
            pair["private_key"]["spki_sha256"] for pair in historical_pairs
        })
        before_cert_fingerprints = sorted({
            pair["certificate"]["sha256_fingerprint"] for pair in historical_pairs
        })
    else:
        before_key_spkis = [before["private_key"]["spki_sha256"]]
        before_cert_fingerprints = [before["certificate"]["sha256_fingerprint"]]
    checks = {
        "key_changed": after["private_key"]["spki_sha256"] not in before_key_spkis,
        "certificate_changed": (
            after["certificate"]["sha256_fingerprint"]
            not in before_cert_fingerprints
        ),
        "after_key_is_not_historical": after["checks"]["current_key_differs_from_all_historical_keys"],
        "after_pair_matches": after["checks"]["certificate_matches_private_key"],
    }
    payload = {
        "schema_version": 1, "collected_at": now_iso(), "git_commit": git_commit(),
        "before_evidence_sha256": hashlib.sha256(before_file.read_bytes()).hexdigest(),
        "after_evidence_sha256": hashlib.sha256(after_file.read_bytes()).hexdigest(),
        "before_key_spkis": before_key_spkis,
        "before_certificate_fingerprints": before_cert_fingerprints,
        "after_certificate_fingerprint": after["certificate"]["sha256_fingerprint"],
        "checks": checks, "passed": all(checks.values()),
    }
    bundle.write("tls-rotation-comparison.json", payload)
    if not payload["passed"]:
        raise CloseoutError("TLS rotation comparison failed")


def trace_ssl_context(base_url: str, ca_file: str | None, insecure_localhost: bool):
    parsed = urlparse(base_url)
    if parsed.scheme != "https":
        raise CloseoutError("trace readback requires an HTTPS base URL")
    if insecure_localhost:
        if parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise CloseoutError("--insecure-localhost is restricted to loopback")
        return ssl._create_unverified_context()
    return ssl.create_default_context(cafile=ca_file)


def read_trace(base_url: str, trace_id: str, token: str, context) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/traces/{trace_id}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20, context=context) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1000]
        raise CloseoutError(f"trace readback HTTP {exc.code}: {detail}") from exc


def collect_trace(
    bundle: EvidenceBundle, base_url: str, token_file: Path,
    ca_file: str | None, insecure_localhost: bool,
) -> dict[str, Any]:
    token = token_file.read_text(encoding="utf-8").strip()
    if not token:
        raise CloseoutError("bearer token file is empty")
    if os.name != "nt" and stat.S_IMODE(token_file.stat().st_mode) & 0o077:
        raise CloseoutError("bearer token file must be mode 0600 or stricter")
    trace_id = f"gate0-closeout-{secrets.token_hex(12)}"
    marker = secrets.token_hex(16)
    code = r'''import json, os
from anila_core.tracing import TraceExporter
exporter = TraceExporter(
    os.environ["ANILA_TRACE_ENDPOINT"],
    token_provider=lambda: os.environ["CSP_SERVICE_TOKEN"],
    start_worker=False,
)
exporter.enqueue(os.environ["GATE0_TRACE_ID"], {
    "span_id": "gate0-router-exporter",
    "span_type": "agent.run.finished",
    "name": "Gate 0 Full Trace closeout",
    "status": "ok",
    "attributes": {"gate0_evidence_marker": os.environ["GATE0_TRACE_MARKER"]},
})
exporter.flush()
print(json.dumps(exporter.stats(), sort_keys=True))'''
    result = run([
        "docker", "compose", "exec", "-T",
        "-e", f"GATE0_TRACE_ID={trace_id}",
        "-e", f"GATE0_TRACE_MARKER={marker}",
        "router", "python", "-c", code,
    ])
    lines = [line for line in output(result).splitlines() if line.strip()]
    stats = json.loads(lines[-1])
    if stats.get("sent") != 1 or stats.get("failed") != 0:
        raise CloseoutError(f"Router TraceExporter failed: {stats}")
    trace = read_trace(
        base_url, trace_id, token,
        trace_ssl_context(base_url, ca_file, insecure_localhost),
    )
    matches = [span for span in trace.get("spans") or []
               if (span.get("attributes") or {}).get("gate0_evidence_marker") == marker]
    payload = {
        "schema_version": 1, "collected_at": now_iso(), "git_commit": git_commit(),
        "trace_id": trace_id, "router_exporter_stats": stats,
        "readback": trace, "marker_verified": len(matches) == 1,
        "passed": stats.get("sent") == 1 and len(matches) == 1,
        "note": "The bearer token is never written to evidence.",
    }
    bundle.write("full-trace-roundtrip.json", payload)
    if not payload["passed"]:
        raise CloseoutError("Full Trace readback did not contain the emitted marker")
    return payload


def collect_deploy_verify(bundle: EvidenceBundle) -> dict[str, Any]:
    result = run(["bash", "infra/deployment/scripts/deploy-prod.sh", "verify"], check=False)
    payload = {
        "schema_version": 1, "collected_at": now_iso(), "git_commit": git_commit(),
        "command": "bash infra/deployment/scripts/deploy-prod.sh verify",
        "returncode": result.returncode,
        "stdout": output(result)[-20000:],
        "stderr": result.stderr.decode("utf-8", errors="replace")[-5000:],
        "passed": result.returncode == 0,
        "scope": [
            "TLS expiry/SAN/key pair", "CSP and Studio readiness",
            "n8n/GitLab image and migration posture", "independent tool origins",
            "n8n unauthenticated ingress denial", "main-origin legacy tool-path denial",
            "tool-host rejection on CSP-bearing port 4443",
        ],
    }
    bundle.write("formal-deploy-verify.json", payload)
    if not payload["passed"]:
        raise CloseoutError("deploy-prod.sh verify failed")
    return payload


def inventory_images() -> list[dict[str, str]]:
    rows = []
    for line in INVENTORY.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        service, image, bundle, activation, source = line.split("\t")
        if activation == "default":
            rows.append({
                "service": service, "image": image,
                "bundle": bundle, "source": source,
            })
    return rows



def wait_runtime(timeout_seconds: int = 900) -> dict[str, Any]:
    healthy = {
        "csp-db", "redis", "pptx-renderer", "csp", "router", "anilalm",
        "anila-ui", "anila-studio", "flux2-dev-agent", "nginx", "n8n", "gitlab",
    }
    running = {"ingestion-worker"}
    deadline = time.monotonic() + timeout_seconds
    last = {}
    while time.monotonic() < deadline:
        last = {}
        for item in inspect_containers():
            service = (item.get("Config", {}).get("Labels") or {}).get(
                "com.docker.compose.service"
            )
            if not service:
                continue
            state = item.get("State") or {}
            last[service] = {
                "state": state.get("Status"),
                "health": (state.get("Health") or {}).get("Status"),
            }
        terminal = {
            service: status for service, status in last.items()
            if status["state"] in {"exited", "dead", "removing"}
            or status["health"] == "unhealthy"
        }
        if terminal:
            raise CloseoutError(f"service entered a terminal state: {terminal}")
        healthy_ready = all(
            last.get(service, {}).get("state") == "running"
            and last.get(service, {}).get("health") == "healthy"
            for service in healthy
        )
        running_ready = all(
            last.get(service, {}).get("state") == "running" for service in running
        )
        if healthy_ready and running_ready:
            return last
        time.sleep(5)
    raise CloseoutError(
        f"stack did not become ready in {timeout_seconds}s: {last}"
    )


def collect_wait(bundle: EvidenceBundle) -> dict[str, Any]:
    status = wait_runtime()
    payload = {
        "schema_version": 1, "collected_at": now_iso(),
        "git_commit": git_commit(), "services": status, "passed": True,
    }
    bundle.write("runtime-readiness.json", payload)
    return payload

def collect_fresh_host(bundle: EvidenceBundle) -> dict[str, Any]:
    config = compose_config()
    project = config.get("name") or "anila-platform"
    invocation_overrides = {
        key: os.environ[key]
        for key in ("COMPOSE_FILE", "COMPOSE_PROFILES", "COMPOSE_PROJECT_NAME")
        if os.environ.get(key)
    }
    formal_invocation = project == "anila-platform" and not invocation_overrides

    containers = [line for line in output(run([
        "docker", "compose", "ps", "-a", "-q"
    ])).splitlines() if line]
    volumes = [line for line in output(run([
        "docker", "volume", "ls", "-q", "--filter",
        f"label=com.docker.compose.project={project}",
    ])).splitlines() if line]
    available, missing = [], []
    for row in inventory_images():
        result = run([
            "docker", "image", "inspect", row["image"], "--format", "{{.Id}}"
        ], check=False)
        (available if result.returncode == 0 else missing).append(
            {**row, "image_id": output(result).strip() if result.returncode == 0 else None}
        )
    preflight = {
        "schema_version": 1, "collected_at": now_iso(), "git_commit": git_commit(),
        "compose_project": project, "existing_project_containers": containers,
        "invocation_overrides": invocation_overrides,
        "formal_invocation": formal_invocation,
        "existing_project_volumes": volumes, "available_default_images": available,
        "missing_default_images": missing,
        "passed": formal_invocation and not containers and not volumes and not missing,
    }
    bundle.write("fresh-host-preflight.json", preflight)
    if not preflight["passed"]:
        raise CloseoutError("fresh-host precondition failed; inspect fresh-host-preflight.json")
    started = run([
        "docker", "compose", "up", "-d", "--no-build", "--pull", "never"
    ])
    final_status = wait_runtime()
    payload = {
        "schema_version": 1, "collected_at": now_iso(), "git_commit": git_commit(),
        "command": "docker compose up -d --no-build --pull never",
        "compose_stdout": output(started)[-12000:],
        "final_status": final_status, "passed": True,
    }
    bundle.write("fresh-host-startup.json", payload)
    if not payload["passed"]:
        raise CloseoutError("fresh-host stack did not become ready")
    return payload


def build_parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--evidence-dir", help="external output directory")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("profile")
    commands.add_parser("runtime")
    commands.add_parser("wait")
    commands.add_parser("verify")
    tls = commands.add_parser("tls")
    tls.add_argument("--label", choices=("before", "after", "current"), default="current")
    commands.add_parser("tls-history-before")
    comparison = commands.add_parser("tls-compare")
    comparison.add_argument("--before", required=True, type=Path)
    comparison.add_argument("--after", required=True, type=Path)
    trace = commands.add_parser("trace")
    trace.add_argument("--base-url", required=True)
    trace.add_argument("--token-file", required=True, type=Path)
    trace.add_argument("--ca-file")
    trace.add_argument("--insecure-localhost", action="store_true")
    fresh = commands.add_parser("fresh-host")
    fresh.add_argument("--confirm-empty-host", action="store_true")
    commands.add_parser("all")
    return root


def main() -> int:
    args = build_parser().parse_args()
    bundle = bundle_from(args)
    if args.command == "profile":
        collect_profile(bundle)
    elif args.command == "runtime":
        collect_runtime(bundle)
    elif args.command == "wait":
        collect_wait(bundle)
    elif args.command == "verify":
        collect_deploy_verify(bundle)
    elif args.command == "tls":
        collect_tls(bundle, args.label)
    elif args.command == "tls-history-before":
        collect_historical_tls_before(bundle)
    elif args.command == "tls-compare":
        tls_rotation_comparison(bundle, args.before, args.after)
    elif args.command == "trace":
        collect_trace(
            bundle, args.base_url, args.token_file,
            args.ca_file, args.insecure_localhost,
        )
    elif args.command == "fresh-host":
        if not args.confirm_empty_host:
            raise CloseoutError("fresh-host requires --confirm-empty-host")
        collect_fresh_host(bundle)
    elif args.command == "all":
        collect_profile(bundle)
        collect_runtime(bundle)
        collect_deploy_verify(bundle)
        collect_tls(bundle, "current")
    print(bundle.directory)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CloseoutError as exc:
        print(f"Gate 0 closeout failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
