#!/usr/bin/env python3
# ruff: noqa: E402 -- Install the protected-file audit hook before other imports.
"""Deterministic, repository-only CycloneDX 1.6 inventory; Python 3.11+ stdlib."""
import os
import sys


def _deny_protected(event, args):
    if event == "open" and args and isinstance(args[0], (str, bytes, os.PathLike)):
        parts = os.fsdecode(args[0]).replace("\\", "/").split("/")
        if ("secrets" in parts or parts[-1].startswith(".env")
                or parts[-1] == ".npmrc" or parts[-1].endswith((".pem", ".key"))):
            raise PermissionError("Protected-file access denied by SBOM builder")


sys.addaudithook(_deny_protected)

import argparse
import base64
from collections import Counter
from email.parser import Parser
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tomllib
from urllib.parse import quote, urlsplit, urlunsplit
import uuid

BASELINE = "816f2b2b053023b9113892e4f7d595d2fc1ba18c"
# No filesystem discovery, include expansion, .npmrc, dotenv, or application import.
MANIFESTS = (
    "apps/anila-shell/package.json", "apps/anila-shell/package-lock.json",
    "apps/anilalm/package.json", "apps/anilalm/package-lock.json",
    "apps/csp-governance-ui/package.json", "apps/csp-governance-ui/package-lock.json",
    "infra/codeserver/microsoft-stubs/@microsoft/1ds-core-js/package.json",
    "infra/codeserver/microsoft-stubs/@microsoft/1ds-post-js/package.json",
    "infra/codeserver/microsoft-stubs/@microsoft/applicationinsights-web-snippet/package.json",
    "infra/codeserver/microsoft-stubs/@microsoft/mxc-sdk/package.json",
    "packages/anila-agent/pyproject.toml", "packages/anila-core/pyproject.toml",
    "packages/anila-core/uv.lock", "services/anila-core-router/requirements.txt",
    "services/anila-studio/pyproject.toml", "services/asr-gateway/pyproject.toml",
    "services/asr-gateway/uv.lock", "services/csp/requirements-dev.txt",
    "services/csp/requirements.txt", "services/hr-lookup/requirements.txt",
    "services/ingestion-worker/pyproject.toml", "services/pptx-renderer/package.json",
    "services/pptx-renderer/package-lock.json",
    "services/pptx-renderer/vendor/image-size/package.json",
    "packages/anila-agent-quickstart/requirements.in",
    "packages/anila-agent-quickstart/requirements.lock",
    "packages/anila-agent-quickstart/mlsteam/requirements-jupyter.in",
    "packages/anila-agent-quickstart/mlsteam/requirements-jupyter.lock",
)
_URL = re.compile(r"(?:git\+)?(?:https?|ssh|git|file)://[^\s\"<>]+")
_REQ = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]+\])?\s*(.*)$")


def canonical(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def safe_url(value):
    """Never emit userinfo, query, fragment, controls, or malformed URL text."""
    if not isinstance(value, str) or any(ord(c) < 32 for c in value):
        return "[redacted-url]"
    raw = value.removeprefix("git+")
    try:
        parts = urlsplit(raw)
        host, port = parts.hostname, parts.port
    except ValueError:
        return "[redacted-url]"
    if parts.scheme not in {"http", "https", "git", "ssh", "file"}:
        return "[redacted-url]"
    authority = f"[{host}]" if host and ":" in host else host or ""
    if port is not None:
        authority += f":{port}"
    return urlunsplit((parts.scheme, authority, parts.path, "", ""))


def safe_text(value):
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return _URL.sub(lambda match: safe_url(match.group()), value)


def safe_tree(value):
    """Apply URL redaction to every exported string, including notices/summary."""
    if isinstance(value, str):
        return safe_text(value)
    if isinstance(value, list):
        return [safe_tree(item) for item in value]
    if isinstance(value, dict):
        return {safe_text(key): safe_tree(item) for key, item in value.items()}
    return value


def prop(name, value):
    return {"name": "anila:" + name, "value": safe_text(value)}


def ref_for(key):
    return "urn:anila:repo-sbom:" + hashlib.sha256("\0".join(key).encode()).hexdigest()[:32]


def requirement(raw):
    match = _REQ.match(raw.strip())
    if not match:
        return None
    name, constraint = match.groups()
    exact = re.match(r"^==([^,;\s*]+)(?:\s*;.*)?$", constraint)
    return canonical(name), constraint, exact.group(1) if exact else None


def _license(value):
    if isinstance(value, dict):
        # A path points at evidence not read by this whitelist-only builder.
        value = value.get("text")
    return value if isinstance(value, str) and value.strip() else None


def _author(value):
    if isinstance(value, dict):
        return value.get("name") or "unknown"
    if isinstance(value, list):
        return "; ".join(_author(item) for item in value) or "unknown"
    return value or "unknown"


class Inventory:
    def __init__(self):
        self.records = {}
        self.edges = {}
        self.files = []
        self.notices = []

    def add(self, ecosystem, path, locator, name, observation, *, version=None,
            constraint="", relationship="unknown", role="runtime", license=None,
            author=None, urls=(), hashes=(), extra=None):
        key = (ecosystem, path, locator, name, observation)
        record = dict(key=key, ref=ref_for(key), ecosystem=ecosystem, path=path,
                      locator=locator, name=name, observation=observation,
                      version=version, constraint=constraint, relationship=relationship,
                      role=role, license=license, author=author or "unknown",
                      urls=sorted(set(safe_url(u) for u in urls if isinstance(u, str))),
                      hashes=list(hashes), extra=extra or {})
        self.records[key] = record
        self.edges.setdefault(record["ref"], set())
        return record

    def edge(self, source, target):
        self.edges[source["ref"]].add(target["ref"])

    def parse(self, path, raw):
        self.files.append({"path": path, "sha256": hashlib.sha256(raw).hexdigest()})
        text = raw.decode("utf-8")
        if path.endswith("package.json"):
            self.npm_manifest(path, json.loads(text))
        elif path.endswith("package-lock.json"):
            self.npm_lock(path, json.loads(text))
        elif path.endswith("pyproject.toml"):
            self.python_manifest(path, tomllib.loads(text))
        elif path.endswith("uv.lock"):
            self.uv_lock(path, tomllib.loads(text))
        else:
            self.requirements(path, text)

    def npm_manifest(self, path, data):
        project = self.add("npm", path, "$project", data.get("name", path), "project",
                           version=data.get("version"), relationship="project",
                           license=_license(data.get("license")), author=_author(data.get("author")),
                           extra={"local-manifest": True})
        for field in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
            for name, constraint in sorted(data.get(field, {}).items()):
                child = self.add("npm", path, field + "/" + name, name, "declared",
                                 version=constraint if re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", constraint) else None,
                                 constraint=constraint, relationship="direct", role=field)
                self.edge(project, child)

    def npm_lock(self, path, data):
        packages = data.get("packages")
        if not isinstance(packages, dict):
            self.notices.append({"path": path, "type": "unsupported_npm_lock_without_packages"})
            return
        root = packages.get("", {})
        direct = set().union(*(set(root.get(field, {})) for field in
                              ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies")))
        entries = {}
        for locator, package in sorted(packages.items()):
            name = package.get("name") or (locator.rsplit("node_modules/", 1)[-1] if locator else data.get("name", path))
            sri = []
            for token in package.get("integrity", "").split():
                algorithm, _, encoded = token.partition("-")
                if algorithm in {"sha1", "sha256", "sha384", "sha512"}:
                    try:
                        sri.append({"alg": "SHA-" + algorithm[3:], "content": base64.b64decode(encoded, validate=True).hex()})
                    except ValueError:
                        self.notices.append({"path": path, "type": "invalid_integrity", "locator": locator})
            entries[locator] = self.add(
                "npm", path, locator or "$project", name, "locked",
                version=package.get("version"),
                relationship="project" if not locator else "direct" if locator == "node_modules/" + name and name in direct else "transitive",
                role="dev" if package.get("dev") else "runtime",
                license=_license(package.get("license")), urls=[package["resolved"]] if package.get("resolved") else [],
                hashes=sri, extra={"optional": bool(package.get("optional")), "link": bool(package.get("link"))},
            )
        for locator, package in packages.items():
            if package.get("link"):
                target = package.get("resolved")
                if target in entries:
                    self.edge(entries[locator], entries[target])
                else:
                    self.notices.append({"path": path, "type": "unresolved_local_link", "locator": locator})
            for field in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
                for name in package.get(field, {}):
                    search = locator
                    while True:
                        candidate = (search + "/" if search else "") + "node_modules/" + name
                        if candidate in entries:
                            self.edge(entries[locator], entries[candidate])
                            break
                        if not search:
                            self.notices.append({"path": path, "type": "unresolved_lock_edge", "from": locator, "dependency": name})
                            break
                        search = search.rsplit("/node_modules/", 1)[0] if "/node_modules/" in search else ""

    def python_manifest(self, path, data):
        project = data.get("project", {})
        root = self.add("pypi", path, "$project", project.get("name", path), "project",
                        version=project.get("version"), relationship="project",
                        license=_license(project.get("license")), author=_author(project.get("authors")),
                        extra={"local-manifest": True,
                               "license-file-not-read": project.get("license", {}).get("file", "")
                               if isinstance(project.get("license"), dict) else ""})
        groups = {"dependencies": project.get("dependencies", [])}
        groups.update({"optional/" + group: values for group, values in project.get("optional-dependencies", {}).items()})
        groups["build-system"] = data.get("build-system", {}).get("requires", [])
        for group, values in groups.items():
            for index, raw in enumerate(values):
                parsed = requirement(raw)
                if not parsed:
                    self.notices.append({"path": path, "type": "unparsed_requirement", "group": group, "index": index})
                    continue
                name, constraint, exact = parsed
                child = self.add("pypi", path, f"{group}/{name}", name, "declared",
                                 version=exact, constraint=constraint, relationship="direct", role=group)
                self.edge(root, child)

    def uv_lock(self, path, data):
        packages = data.get("package", [])
        roots = [p for p in packages if "editable" in p.get("source", {}) or "virtual" in p.get("source", {})]
        direct = set()
        for root in roots:
            groups = [root.get("dependencies", [])] + list(root.get("optional-dependencies", {}).values()) + list(root.get("dev-dependencies", {}).values())
            direct.update(canonical(dep["name"]) for group in groups for dep in group)
        entries = []
        counts = Counter(canonical(package["name"]) for package in packages)
        for package in packages:
            name = canonical(package["name"])
            artifacts = [package["sdist"]] if package.get("sdist") else []
            artifacts += package.get("wheels", [])
            locator = name if counts[name] == 1 else name + "@" + package.get("version", "unknown") + ":" + hashlib.sha256(json.dumps(package.get("source", {}), sort_keys=True).encode()).hexdigest()[:12]
            record = self.add("pypi", path, locator, name, "locked",
                              version=package.get("version"), relationship="project" if package in roots else "direct" if name in direct else "transitive",
                              urls=[artifact["url"] for artifact in artifacts if artifact.get("url")],
                              extra={"source": package.get("source", {}), "artifacts": artifacts,
                                     "markers": package.get("resolution-markers", [])})
            entries.append((package, record))
        for package, record in entries:
            groups = [package.get("dependencies", [])] + list(package.get("optional-dependencies", {}).values()) + list(package.get("dev-dependencies", {}).values())
            for group in groups:
                for dependency in group:
                    matches = [other for candidate, other in entries if canonical(candidate["name"]) == canonical(dependency["name"])
                               and (not dependency.get("version") or candidate.get("version") == dependency["version"])]
                    if len(matches) == 1:
                        self.edge(record, matches[0])
                    else:
                        self.notices.append({"path": path, "type": "ambiguous_uv_edge", "from": package["name"], "dependency": dependency["name"]})

    def requirements(self, path, text):
        root = self.add("pypi", path, "$project", path, "project", relationship="project")
        logical, pending = [], ""
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            pending += " " + stripped.rstrip("\\").strip()
            if stripped.endswith("\\"):
                continue
            logical.append(pending.strip())
            pending = ""
        if pending:
            logical.append(pending.strip())
        for index, line in enumerate(logical):
            if line.startswith(("-r ", "-c ", "-e ", "--")):
                self.notices.append({"path": path, "type": "directive_not_followed", "line": index + 1})
                continue
            statement = re.split(r"\s+--hash=|\s+#", line)[0]
            parsed = requirement(statement)
            if not parsed:
                self.notices.append({"path": path, "type": "unparsed_requirement", "line": index + 1})
                continue
            name, constraint, exact = parsed
            locked = path.endswith(".lock")
            direct_names = {r["name"] for r in self.records.values() if r["path"] == path.removesuffix(".lock") + ".in" and r["observation"] == "declared"}
            relationship = "direct" if not locked or name in direct_names else "transitive" if direct_names else "unknown"
            child = self.add("pypi", path, name, name, "locked" if locked else "declared",
                     version=exact, constraint=constraint, relationship=relationship,
                     hashes=[{"alg": "SHA-256", "content": value} for value in re.findall(r"--hash=sha256:([a-fA-F0-9]{64})", line)],
                     extra={"hash-pinned-declaration": bool("--hash=" in line), "markers-evaluated": False})
            self.edge(root, child)

    def installed_metadata(self, roots):
        names = {canonical(record["name"]) for record in self.records.values() if record["ecosystem"] == "pypi"}
        for raw_root in sorted(roots):
            root = Path(raw_root).resolve(strict=True)
            if root.name not in {"site-packages", "dist-packages"}:
                raise ValueError("Installed metadata roots must end in site-packages or dist-packages")
            for path in sorted(root.glob("*.dist-info/METADATA")):
                if path.is_symlink() or path.parent.is_symlink():
                    continue
                raw = path.read_bytes()
                message = Parser().parsestr(raw.decode("utf-8", errors="replace"))
                name = canonical(message.get("Name", ""))
                if name not in names:
                    continue
                self.add("pypi", str(path), "$metadata", name, "installed",
                         version=message.get("Version"), license=message.get("License-Expression") or message.get("License"),
                         author=message.get("Author") or message.get("Author-email"),
                         urls=[message["Home-page"]] if message.get("Home-page") else [],
                         extra={"metadata-sha256": hashlib.sha256(raw).hexdigest(),
                                "project-urls-declared": message.get_all("Project-URL", []),
                                "requires-dist-declared": message.get_all("Requires-Dist", []),
                                "not-a-lock-observation": True})


def load_repository(root, baseline=None):
    inventory = Inventory()
    if baseline:
        check = subprocess.run(["git", "cat-file", "-e", f"{baseline}^{{commit}}"],
                               cwd=root, capture_output=True, check=False)
        if check.returncode:
            raise ValueError("Baseline must resolve to an existing Git commit")
    for relative in MANIFESTS:
        path = root / relative
        if baseline:
            process = subprocess.run(["git", "show", f"{baseline}:{relative}"], cwd=root,
                                     capture_output=True, check=False)
            if process.returncode:
                inventory.notices.append({"path": relative, "type": "absent_at_baseline"})
                continue
            raw = process.stdout
        else:
            if not path.is_file():
                inventory.notices.append({"path": relative, "type": "missing_whitelisted_manifest"})
                continue
            if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
                raise ValueError("Whitelisted manifests must be regular files within the repository")
            raw = path.read_bytes()
        inventory.parse(relative, raw)
    return inventory


def change(record, previous):
    if record["observation"] == "installed":
        return "not-compared-installed-observation"
    if previous is None:
        return "added"
    if (record["version"], record["constraint"]) != (previous["version"], previous["constraint"]):
        return "version-or-constraint-changed"
    # Evidence-only locator and scope stay separate from version comparison.
    return "baseline-existing"


def component(record, previous):
    properties = [prop(key, record[key]) for key in
                  ("path", "locator", "observation", "relationship", "role", "constraint")]
    properties += [prop("baseline-status", change(record, previous)),
                   prop("source-assurance", "manifest/lock/metadata declaration; not origin or control verification"),
                   prop("source-status", "declared-unverified" if record["urls"] else "unknown"),
                   prop("developer-declared", record["author"]),
                   prop("developer-status", "unknown" if record["author"] == "unknown" else "declared-unverified"),
                   prop("version-status", "recorded-exact" if record["version"] else "unknown-exact-version"),
                   prop("country-control-status", "unknown"),
                   prop("license-status", "declared-unverified" if record["license"] else "unknown"),
                   prop("scope-not-production", "Repository package observation, not OS/image/remote-service SBOM")]
    properties += [prop(key, value) for key, value in record["extra"].items()]
    if previous and change(record, previous) != "baseline-existing":
        properties.append(prop("baseline-version-or-constraint", [previous["version"], previous["constraint"]]))
    result = {"type": "application" if record["relationship"] == "project" else "library",
              "bom-ref": record["ref"], "name": safe_text(record["name"]),
              "properties": sorted(properties, key=lambda value: (value["name"], value["value"]))}
    if record["version"]:
        result["version"] = safe_text(str(record["version"]))
    if record["relationship"] != "project":
        result["purl"] = "pkg:" + record["ecosystem"] + "/" + quote(safe_text(record["name"]), safe="/")
        if record["version"]:
            result["purl"] += "@" + quote(safe_text(str(record["version"])), safe="")
    if record["license"]:
        # A name preserves the declaration without pretending it is a validated SPDX expression.
        result["licenses"] = [{"license": {"name": safe_text(record["license"])}}]
    refs = [{"type": "distribution", "url": url} for url in record["urls"] if url.startswith(("https://", "http://"))]
    if refs:
        result["externalReferences"] = refs
    if record["hashes"]:
        result["hashes"] = sorted(record["hashes"], key=lambda item: (item["alg"], item["content"]))
    return result


def build(root, baseline=BASELINE, installed_roots=()):
    current, base = load_repository(root), load_repository(root, baseline)
    head_result = subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=root,
                                 capture_output=True, text=True, check=False)
    source_head = head_result.stdout.strip() if head_result.returncode == 0 else "unknown"
    current.installed_metadata(installed_roots)
    components = [component(record, base.records.get(key)) for key, record in sorted(current.records.items())]
    removed = [record for key, record in sorted(base.records.items()) if key not in current.records]
    statuses = Counter(change(record, base.records.get(key)) for key, record in current.records.items())
    root_ref = "urn:anila:repo-sbom:repository"
    bom = {"$schema": "http://cyclonedx.org/schema/bom-1.6.schema.json", "bomFormat": "CycloneDX", "specVersion": "1.6", "version": 1,
           "metadata": {"component": {"type": "application", "bom-ref": root_ref, "name": "ANILA repository package inventory"},
                        "properties": [prop("baseline-commit", baseline), prop("source-head", source_head),
                                       prop("manifest-files", sorted(current.files, key=lambda item: item["path"])),
                                       prop("excluded", "OS/image/binary/browser/font/model/remote-service inventory and full provenance/control review"),
                                       prop("determinism", "No wall-clock timestamp, runtime import, network, resolver, installer, or recursive manifest discovery"),
                                       prop("markers", "All recorded optional/dev/platform lock entries retained; environment markers not evaluated")]},
           "components": components,
           "dependencies": [{"ref": root_ref, "dependsOn": sorted(record["ref"] for record in current.records.values() if record["relationship"] == "project")},
                            *[{"ref": ref, "dependsOn": sorted(targets)} for ref, targets in sorted(current.edges.items())]],
           "properties": [prop("comparison-counts", dict(sorted(statuses.items()))), prop("removed-observations", [{"name": r["name"], "path": r["path"], "version": r["version"], "observation": r["observation"]} for r in removed]),
                          prop("notices", sorted(current.notices, key=lambda item: json.dumps(item, sort_keys=True))),
                          prop("compliance", "NF-11 not passed. Zero new declarations does not certify existing dependencies."),
                          prop("opencc-provenance", "opencc-python-reimplemented 0.1.7 reuses BYVoid/OpenCC dictionary/configuration data; https://github.com/yichen0831/opencc-python/blob/master/NOTICE.txt"),
                          prop("opencc-reachability", "quick_model_stream→reasoning_gate→anila_core.text.__init__→zh_normalizer→OpenCC; Shell conversation_service→zh_normalize_service→normalize_report→OpenCC(s2twp); Studio main→reports→report_runner→studio_text_normalizer startup import; pure quick renderer bypasses normalizer")]}
    bom = safe_tree(bom)
    payload = json.dumps(bom, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    bom["serialNumber"] = "urn:uuid:" + str(uuid.uuid5(uuid.NAMESPACE_URL, hashlib.sha256(payload.encode()).hexdigest()))
    summary = {"manifest_count": len(current.files), "component_count": len(components), "comparison": dict(sorted(statuses.items())),
               "observations": dict(sorted(Counter(r["observation"] for r in current.records.values()).items())),
               "removed_count": len(removed), "notices": current.notices, "baseline": baseline}
    return bom, safe_tree(summary)


def validate_structure(bom):
    """Local invariants, not a full external JSON Schema validation claim."""
    assert bom["bomFormat"] == "CycloneDX" and bom["specVersion"] == "1.6"
    refs = [bom["metadata"]["component"]["bom-ref"], *[c["bom-ref"] for c in bom["components"]]]
    assert len(refs) == len(set(refs))
    for edge in bom["dependencies"]:
        assert edge["ref"] in refs and all(target in refs for target in edge["dependsOn"])
    for comp in bom["components"]:
        fields = {p["name"]: p["value"] for p in comp["properties"]}
        assert fields["anila:observation"] in {"project", "declared", "locked", "installed"}
        assert fields["anila:country-control-status"] == "unknown"
        for reference in comp.get("externalReferences", []):
            parts = urlsplit(reference["url"])
            assert parts.username is None and parts.password is None and not parts.query and not parts.fragment


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--baseline", default=BASELINE)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--installed-root", action="append", default=[], help="Optional explicit Python distribution metadata root; not a lock")
    args = parser.parse_args()
    bom, result = build(args.repo, args.baseline, args.installed_root)
    validate_structure(bom)
    args.output.write_text(json.dumps(bom, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    args.summary.write_text("# Repository 套件 SBOM 範圍摘要\n\n"
                            + "此為明確白名單內的 repository package inventory；不是 OS／映像／遠端服務完整 SBOM，也不是 NF-11 通過證明。\n\n"
                            + "```json\n" + json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n```\n\n"
                            + "declared 是 manifest 宣告；locked 是 lock 記錄；installed 僅為顯式指定本機 metadata 觀察。"
                            + "精確 pin 的 requirements.txt 仍屬 declared，不能冒充 lock。環境 marker、optional/dev 與所有平台項目未經安裝解析。\n\n"
                            + "component_count 是按來源檔案與定位區分的觀測紀錄數；同名不同版本及不同宣告位置保留，並非去重後的實體安裝套件數。license-file 路徑不是已讀取的授權內容。\n\n"
                            + "國別／控制關係均未知；版本、授權與作者來源自述未替代完整來源審查。OpenCC 真實使用鏈與上游詞典來源保留於 BOM properties。"
                            + "來源 URL 已移除 credential、query 與 fragment。內建檢核只驗必要結構與參照完整性，未宣稱外部 JSON Schema 完整驗證。\n")
    print(json.dumps({key: value for key, value in result.items() if key != "notices"}, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
