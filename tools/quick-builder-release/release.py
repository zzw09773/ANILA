#!/usr/bin/env python3
# ruff: noqa: E402 -- Install deny rules before importing other modules.
"""Reproducible, source-only offline preparation inventory. Python 3.11 stdlib."""
import os
import sys


def protected(path):
    parts = os.fsdecode(path).replace("\\", "/").lower().split("/")
    return any(part in {"secrets", "keys", "credentials", "logs", ".git", ".ssh", ".aws"} or part.startswith(".env")
               or part == ".npmrc" or part.endswith((".pem", ".key", ".log", ".p12", ".pfx"))
               for part in parts)


def deny(event, args):
    if event == "open" and args and isinstance(args[0], (str, bytes, os.PathLike)) and protected(args[0]):
        raise PermissionError("Protected file denied")
    if event in {"subprocess.Popen", "os.system", "os.posix_spawn", "os.exec", "os.fork", "socket.connect", "socket.getaddrinfo"}:
        raise PermissionError("Commands and network are disabled")


sys.addaudithook(deny)

import argparse
import hashlib
import io
import json
from pathlib import PurePosixPath
import re
import stat
import tarfile

TOOL_VERSION = "1.0.0"
COVERAGE_BASELINE = "816f2b2b053023b9113892e4f7d595d2fc1ba18c"
# Frozen review-time comparison, not runtime Git discovery. Future changes
# require a new explicit review. Omitted files are never opened or archived.
OMITTED_FILES = (
    'apps/anila-shell/src/__tests__/followUpsUnavailable.test.jsx',
    'apps/anila-shell/src/__tests__/redactionChokePoint.test.jsx',
    'services/anila-studio/tests/test_fixed_text_normalization.py',
    'services/anila-studio/tests/test_no_anila_core_import.py',
    'services/anila-studio/tests/test_studio_batch3_layouts.py',
    'services/anila-studio/tests/test_studio_substance.py',
    'services/csp/tests/test_conversation_background_pool.py',
    'services/csp/tests/test_external_services.py',
    'services/csp/tests/test_follow_ups.py',
    'services/csp/tests/test_quick_builder_http_load_tool.py',
    'services/csp/tests/test_quick_stream_pool.py',
    'apps/anila-shell/src/__tests__/followUps.test.jsx',
    'apps/anila-shell/src/__tests__/helpers/fakeBackend.js',
    'apps/anila-shell/src/__tests__/normalizeAgents.test.js',
    'apps/anila-shell/src/__tests__/quickBuilderConversation.test.jsx',
    'apps/anila-shell/src/__tests__/shellNav.test.jsx',
    'apps/csp-governance-ui/tests/helpers/quickBuilderMcpMock.mjs',
    'apps/csp-governance-ui/tests/helpers/quickBuilderMock.mjs',
    'apps/csp-governance-ui/tests/ingestionFileAccept.test.mjs',
    'apps/csp-governance-ui/tests/quickBuilder.test.mjs',
    'apps/csp-governance-ui/tests/quickBuilderMcp.test.mjs',
    'packages/anila-core/tests/test_document_structure_review.py',
    'packages/anila-core/tests/test_ingestion_readiness.py',
    'packages/anila-core/tests/test_parse_remote_error_routing.py',
    'packages/anila-core/tests/test_pdf_ocr_trigger.py',
    'packages/anila-core/tests/test_pdf_page_markers.py',
    'packages/anila-core/tests/test_quick_builder_documents.py',
    'packages/anila-core/tests/test_source_boundaries_limits.py',
    'packages/anila-core/tests/test_zh_normalizer.py',
    'services/anila-studio/tests/test_quick_documents.py',
    'services/anila-studio/tests/test_report_renderer.py',
    'services/anila-studio/tests/test_report_runner.py',
    'services/anila-studio/tests/test_studio_batch1_fixes.py',
    'services/asr-gateway/tests/test_session.py',
    'services/csp/tests/test_conversation_compact.py',
    'services/csp/tests/test_quick_builder.py',
    'services/csp/tests/test_quick_builder_load_tool.py',
    'services/csp/tests/test_quick_builder_review.py',
    'services/csp/tests/test_quick_conversation_target.py',
    'services/csp/tests/test_quick_document.py',
    'services/csp/tests/test_quick_document_review.py',
    'services/csp/tests/test_quick_document_routing.py',
    'services/csp/tests/test_quick_execution_context.py',
    'services/csp/tests/test_quick_flow.py',
    'services/csp/tests/test_quick_hybrid.py',
    'services/csp/tests/test_quick_live_sources.py',
    'services/csp/tests/test_quick_mcp.py',
    'services/csp/tests/test_quick_model_stream.py',
    'services/csp/tests/test_quick_readiness.py',
    'services/csp/tests/test_quick_retrieval.py',
    'services/csp/tests/test_quick_retrieval_budget.py',
    'services/csp/tests/test_quick_stream_mask.py',
    'services/csp/tests/test_quick_stream_response.py',
    'services/csp/tests/test_quick_stream_terminal.py',
    'services/csp/tests/test_quick_test_authorization.py',
    'services/csp/tests/test_reasoning_open_tag.py',
    'services/csp/tests/test_zh_normalize_hook.py',
    'services/ingestion-worker/tests/test_docling_console.py',
    'services/ingestion-worker/tests/test_handlers_relation_progress.py',
    'services/ingestion-worker/tests/test_handlers_zero_chunk.py',
    'tools/sbom/test_build_repo_sbom.py',
)
MAX_SOURCE_FILE = 16 * 1024 * 1024
MAX_SOURCE_TOTAL = 64 * 1024 * 1024
MAX_MANIFEST = 1024 * 1024
MAX_ASSET_FILE = 32 * 1024**3
MAX_ASSET_TOTAL = 128 * 1024**3
BLOCK = 1024 * 1024
IMAGE_NAMES = ("pgvector", "pgbouncer", "csp-credential-dirs", "csp", "redis", "ingestion-worker",
               "router", "nginx", "pptx-renderer", "hr-lookup", "anila-studio", "anilalm", "anila-ui",
               "codeserver", "n8n", "asr-gateway")
ASSETS = tuple(("image:" + name, "images/" + name + ".tar.gz") for name in IMAGE_NAMES) + (
    ("ocr:tesseract", "ocr/tesseract.tar.gz"), ("ocr:chi_tra", "ocr/chi_tra.traineddata"),
    ("ocr:eng", "ocr/eng.traineddata"), ("office:libreoffice", "office/libreoffice.tar.gz"),
    ("document:pandoc", "document/pandoc.tar.gz"), ("fonts", "fonts/fonts.tar.gz"),
    ("browser:chromium", "browser/chromium.tar.gz"), ("sbom:images", "sbom/images.cdx.json"),
    ("licenses", "licenses/third-party-notices.txt"),
)
SOURCE_FILES = tuple(sorted((
    'apps/anilalm/src/utils/ingestionFileAccept.ts',
    'infra/nginx/anila.conf',
    'services/anila-studio/README.en.md',
    'services/anila-studio/README.md',
    'services/anila-studio/app/schemas/infographic.py',
    'services/asr-gateway/README.md',
    'services/csp/app/services/auth_service.py',
    'services/csp/app/services/memory_service.py',
    'tools/loadtest/quick_builder_README.md',
    'tools/loadtest/quick_builder_http_README.md',
    'tools/loadtest/quick_builder_http_load.py',
    'tools/loadtest/quick_builder_load.py',
    'apps/anila-shell/src/app.jsx',
    'apps/anila-shell/src/chat.jsx',
    'apps/anila-shell/src/components/MessageList.jsx',
    'apps/anila-shell/src/components/SettingsModal.jsx',
    'apps/anila-shell/src/runtime/chatHelpers.js',
    'apps/anila-shell/src/runtime/conversations.js',
    'apps/anila-shell/src/runtime/followUps.js',
    'apps/anila-shell/src/runtime/serverMappers.js',
    'apps/anila-shell/src/runtime/useUiSettings.js',
    'apps/anila-shell/src/shellNav.jsx',
    'packages/anila-core/src/anila_core/text/domain_terms.py',
    'services/csp/app/services/follow_ups_policy.py',
    'services/csp/app/services/follow_ups_service.py',
    'services/csp/app/services/settings_registry.py',
    'services/anila-studio/app/services/studio_text_normalizer.py',
    'services/anila-studio/app/services/report_runner.py',
    'services/anila-studio/app/api/datatables.py',
    'services/anila-studio/app/api/infographics.py',
    'services/anila-studio/app/api/studio.py',
    'services/anila-studio/app/api/mindmaps.py',
    'services/asr-gateway/app/session.py',
    'services/asr-gateway/app/config.py',
    'services/asr-gateway/pyproject.toml',
    'services/asr-gateway/uv.lock',
    'apps/csp-governance-ui/src/api/client.js',
    'apps/csp-governance-ui/src/api/errors.js',
    'apps/csp-governance-ui/src/components/layout/AppHeader.vue',
    'apps/csp-governance-ui/src/components/layout/AppSidebar.vue',
    'apps/csp-governance-ui/src/router/index.js',
    'apps/csp-governance-ui/src/utils/ingestionFileAccept.js',
    'apps/csp-governance-ui/src/quickBuilder/builder.css',
    'apps/csp-governance-ui/src/quickBuilder/documentSettings.js',
    'apps/csp-governance-ui/src/quickBuilder/readiness.js',
    'apps/csp-governance-ui/src/quickBuilder/toolArguments.js',
    'apps/csp-governance-ui/src/quickBuilder/useDraftActor.js',
    'packages/anila-core/src/anila_core/ingestion/chunking_plugins/builtins.py',
    'packages/anila-core/src/anila_core/ingestion/chunking_plugins/structure.py',
    'packages/anila-core/src/anila_core/ingestion/docling_parser.py',
    'packages/anila-core/src/anila_core/ingestion/ocr.py',
    'packages/anila-core/src/anila_core/ingestion/parser_registry.py',
    'packages/anila-core/src/anila_core/ingestion/parsers.py',
    'packages/anila-core/src/anila_core/ingestion/presentation.py',
    'packages/anila-core/src/anila_core/ingestion/readiness.py',
    'packages/anila-core/src/anila_core/ingestion/source_code.py',
    'packages/anila-core/src/anila_core/ingestion/text_quality.py',
    'packages/anila-core/src/anila_core/text/__init__.py',
    'packages/anila-core/src/anila_core/text/zh_normalizer.py',
    'services/anila-studio/app/main.py',
    'services/anila-studio/app/schemas/quick_document.py',
    'services/anila-studio/app/services/report_renderer.py',
    'services/csp/app/api/agents/approval.py',
    'services/csp/app/api/agents/registration.py',
    'services/csp/app/api/conversations.py',
    'services/csp/app/api/proxy.py',
    'services/csp/app/api/router.py',
    'services/csp/app/api/users.py',
    'services/csp/app/models/__init__.py',
    'services/csp/app/services/api_key_service.py',
    'services/csp/app/services/collection_scope.py',
    'services/csp/app/services/conversation_service.py',
    'services/csp/app/services/internal_llm.py',
    'services/csp/app/services/zh_normalize_service.py',
    'services/ingestion-worker/src/ingestion_worker/handlers.py',
    "apps/anila-shell/package.json", "apps/anila-shell/package-lock.json",
    "apps/anilalm/package.json", "apps/anilalm/package-lock.json",
    "apps/csp-governance-ui/package.json", "apps/csp-governance-ui/package-lock.json",
    "apps/csp-governance-ui/src/api/quickBuilder.js",
    "apps/csp-governance-ui/src/views/QuickBuilderView.vue",
    "apps/csp-governance-ui/src/views/QuickBuilderMcpView.vue",
    "apps/csp-governance-ui/src/quickBuilder/NodeSettings.vue",
    "apps/csp-governance-ui/src/quickBuilder/ReadinessPanel.vue",
    "apps/csp-governance-ui/src/quickBuilder/ToolParameter.vue",
    "apps/csp-governance-ui/src/quickBuilder/graph.js",
    "apps/csp-governance-ui/src/quickBuilder/ragSettings.js",
    "apps/csp-governance-ui/src/quickBuilder/retrievalNotices.js",
    "docs/guides/quick-builder-acceptance.md", "docs/guides/quick-builder-creator.md",
    "docs/guides/quick-builder-reviewer.md", "docs/guides/quick-builder-mcp.md",
    "docs/guides/quick-builder-operations.md", "docs/guides/quick-builder-dependencies.md",
    "docs/guides/quick-builder-offline-delivery.md",
    "examples/quick-builder-mcp/README.md", "examples/quick-builder-mcp/server.py",
    "examples/quick-builder-mcp/postgresql-readonly.sql",
    "infra/compose/platform.yml", "infra/docker/csp.Dockerfile",
    "packages/anila-core/pyproject.toml", "packages/anila-core/uv.lock",
    "packages/anila-core/src/anila_core/ingestion/local_ocr.py",
    "packages/anila-core/src/anila_core/ingestion/office_conversion.py",
    "packages/anila-core/src/anila_core/ingestion/image_ocr.py",
    "packages/anila-core/src/anila_core/storage/adapters/pgvector_store.py",
    "scripts/release/images.tsv", "scripts/release/build-release.sh",
    "scripts/release/release-lib.sh", "scripts/release/anila-update.sh",
    "scripts/release/preflight.sh", "services/csp/requirements.txt", "services/csp/requirements-dev.txt",
    "services/csp/app/api/quick_builder.py", "services/csp/app/api/quick_mcp.py",
    "services/csp/app/api/quick_readiness.py", "services/csp/app/api/ingestion/search.py",
    "services/csp/app/models/quick_builder.py", "services/csp/app/models/quick_mcp.py",
    "services/csp/app/schemas/quick_builder.py", "services/csp/app/schemas/quick_mcp.py",
    "services/csp/app/services/quick_builder.py", "services/csp/app/services/quick_document.py",
    "services/csp/app/services/quick_execution.py", "services/csp/app/services/quick_flow.py",
    "services/csp/app/services/quick_mcp.py", "services/csp/app/services/quick_model_stream.py",
    "services/csp/app/services/quick_retrieval.py", "services/csp/app/services/quick_stream_mask.py",
    "services/csp/app/services/quick_stream_response.py", "services/csp/app/services/quick_templates.py",
    "services/csp/app/services/chunk_retrieval.py", "services/csp/app/services/reasoning_gate.py",
    "services/csp/app/services/proxy/service.py",
    "services/csp/migrations/versions/r1_0076_quick_builder.py",
    "services/ingestion-worker/pyproject.toml", "services/anila-studio/pyproject.toml",
    "services/anila-studio/app/api/quick_documents.py",
    "services/anila-studio/app/services/quick_document_render.py",
    "tools/sbom/build_repo_sbom.py", "tools/sbom/validate_cyclonedx.py", "tools/sbom/README.md",
    "tools/quick-builder-release/release.py", "tools/quick-builder-release/test_release.py",
    "tools/quick-builder-release/README.md",
)))


class UnsafeInput(ValueError):
    pass


def relative(path):
    if (not isinstance(path, str) or not path or "\\" in path or protected(path)
            or any(ord(char) < 32 or ord(char) == 127 for char in path)):
        raise UnsafeInput("Forbidden path")
    parsed = PurePosixPath(path)
    if parsed.is_absolute() or str(parsed) != path or any(part in {"", ".", ".."} for part in path.split("/")):
        raise UnsafeInput("Noncanonical relative path")
    return parsed.parts


def directory(path):
    raw = os.fspath(path)
    if not os.path.isabs(raw) or protected(raw) or "\\" in raw:
        raise UnsafeInput("Root must be an unprotected absolute directory")
    parts = relative(raw[1:]) if raw != "/" else ()
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


class Root:
    def __init__(self, path):
        self.fd = directory(path)

    def close(self):
        os.close(self.fd)

    def open(self, path, *, maximum=MAX_SOURCE_FILE):
        parts = relative(path)
        fd = os.dup(self.fd)
        try:
            for part in parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            result = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        finally:
            os.close(fd)
        info = os.fstat(result)
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum or info.st_nlink != 1:
            os.close(result)
            raise UnsafeInput("Input must be a bounded regular file without hard links")
        return os.fdopen(result, "rb"), info.st_size

    @staticmethod
    def stable(handle, before):
        after = os.fstat(handle.fileno())
        fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_nlink")
        if any(getattr(before, name) != getattr(after, name) for name in fields):
            raise UnsafeInput("Input changed during read")

    def read(self, path, *, maximum=MAX_SOURCE_FILE):
        handle, size = self.open(path, maximum=maximum)
        with handle:
            before = os.fstat(handle.fileno())
            data = handle.read(maximum + 1)
            self.stable(handle, before)
        if len(data) != size or len(data) > maximum:
            raise UnsafeInput("Input changed or exceeded limit")
        return data

    def digest(self, path, *, maximum=MAX_ASSET_FILE):
        handle, expected = self.open(path, maximum=maximum)
        count, digest = 0, hashlib.sha256()
        with handle:
            before = os.fstat(handle.fileno())
            while piece := handle.read(BLOCK):
                count += len(piece)
                if count > maximum:
                    raise UnsafeInput("Input grew beyond size limit")
                digest.update(piece)
            self.stable(handle, before)
        if count != expected:
            raise UnsafeInput("Input changed during hashing")
        return count, digest.hexdigest()

    def write(self, path, data):
        # Only fixed output basenames. Refuse overwrites, symlinks and traversal.
        if path not in {"manifest.json", "SHA256SUMS", "source.tar"}:
            raise UnsafeInput("Output filename not allowed")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self.fd)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)


def canonical(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def row(root, ident, path, maximum):
    if root is None:
        return {"id": ident, "path": path, "status": "missing", "size": None, "sha256": None}
    try:
        size, digest = root.digest(path, maximum=maximum)
    except FileNotFoundError:
        return {"id": ident, "path": path, "status": "missing", "size": None, "sha256": None}
    return {"id": ident, "path": path, "status": "present", "size": size, "sha256": digest}


def declared_images(repo):
    try:
        data = repo.read("scripts/release/images.tsv", maximum=65536).decode("utf-8")
    except FileNotFoundError:
        return []
    return parse_images(data)


def parse_images(data):
    result = []
    for line in data.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        fields = line.split()
        if (not 4 <= len(fields) <= 5 or fields[2] not in IMAGE_NAMES
                or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,80}", fields[0])
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,200}(?:@sha256:[a-f0-9]{64})?", fields[1])):
            raise UnsafeInput("Unsupported image declaration; update fixed allowlist after review")
        result.append({"service": fields[0], "declared_reference": fields[1],
                       "artifact": "images/" + fields[2] + ".tar.gz",
                       "observed_image_id": None, "identity_verified": False})
    return sorted(result, key=lambda item: item["service"])


def make_manifest(repo, assets=None, *, archive=False, repo_sbom=None, revision=None):
    if revision is not None and not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", revision):
        raise UnsafeInput("Revision must be an explicit full commit hash")
    files, source_total = [], 0
    for path in SOURCE_FILES:
        item = row(repo, path, path, min(MAX_SOURCE_FILE, MAX_SOURCE_TOTAL - source_total))
        source_total += item["size"] or 0
        files.append(item)
    if sum(item["size"] or 0 for item in files) > MAX_SOURCE_TOTAL:
        raise UnsafeInput("Source selection exceeds total size limit")
    artifacts, asset_total = [], 0
    for ident, path in ASSETS:
        item = row(assets, ident, path, min(MAX_ASSET_FILE, MAX_ASSET_TOTAL - asset_total))
        asset_total += item["size"] or 0
        artifacts.append(item)
    if repo_sbom is not None:
        parent, basename = os.path.split(repo_sbom)
        if not basename.endswith(".cdx.json"):
            raise UnsafeInput("Explicit repo SBOM must end in .cdx.json")
        supplied = Root(parent)
        try:
            observed = row(supplied, "sbom:repo", basename, min(MAX_SOURCE_FILE, MAX_ASSET_TOTAL - asset_total))
            observed["path"] = "sbom/repo.cdx.json"
            if observed["status"] == "present":
                data = json.loads(supplied.read(basename))
                if not isinstance(data, dict) or data.get("bomFormat") != "CycloneDX":
                    raise UnsafeInput("Explicit repo SBOM is not a CycloneDX document")
        finally:
            supplied.close()
    else:
        observed = row(assets, "sbom:repo", "sbom/repo.cdx.json", min(MAX_SOURCE_FILE, MAX_ASSET_TOTAL - asset_total))
    artifacts.append(observed)
    if sum(item["size"] or 0 for item in artifacts) > MAX_ASSET_TOTAL:
        raise UnsafeInput("Assets exceed total size limit")
    complete = all(item["status"] == "present" for item in files)
    return {"schema": "anila.quick-builder.offline-preparation/v1", "tool_version": TOOL_VERSION,
            "package_kind": "source-only-preparation", "revision_claim": {"value": revision, "verified": False},
            "coverage_baseline": COVERAGE_BASELINE, "coverage_inventory_verified": False,
            "omitted_changed_paths": [{"path": path, "reason": "test/evidence outside source selection"} for path in OMITTED_FILES],
            "selection_complete": complete, "deployment_ready": False,
            "delivery_ready": False, "deployment_validated": False, "source_revision_verified": False,
            "archive_included": archive,
            "asset_files_complete": all(item["status"] == "present" for item in artifacts),
            "files": files, "assets": sorted(artifacts, key=lambda item: item["id"]),
            "declared_images": declared_images(repo),
            "limitations": ["Selected source only; not a complete build context or installable release.",
                            "Asset presence and checksums do not verify image identity, licenses, SBOM completeness or NF-11.",
                            "No Docker, daemon inspection, downloads, installation or deployment was performed."]}


def archive_bytes(repo, manifest, encoded):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for path, data in [("manifest.json", encoded)] + [
            ("source/" + item["path"], repo.read(item["path"])) for item in manifest["files"] if item["status"] == "present"
        ]:
            if path != "manifest.json":
                expected = next(item for item in manifest["files"] if "source/" + item["path"] == path)
                if len(data) != expected["size"] or sha(data) != expected["sha256"]:
                    raise UnsafeInput("Source changed after manifest hashing")
            info = tarfile.TarInfo(path)
            info.size, info.mode, info.mtime = len(data), 0o644, 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            archive.addfile(info, io.BytesIO(data))
    return output.getvalue()


def build(repo_path, output_path, *, assets_path=None, archive=False, repo_sbom=None, revision=None):
    if os.path.commonpath((repo_path, output_path)) == os.path.normpath(repo_path):
        raise UnsafeInput("Output must be outside the source root")
    repo, assets, output = Root(repo_path), None, None
    try:
        assets = Root(assets_path) if assets_path else None
        output = Root(output_path)
        manifest = make_manifest(repo, assets, archive=archive, repo_sbom=repo_sbom, revision=revision)
        encoded = canonical(manifest)
        tar = archive_bytes(repo, manifest, encoded) if archive else None
        output.write("manifest.json", encoded)
        checksums = [("manifest.json", sha(encoded))]
        if tar is not None:
            output.write("source.tar", tar)
            checksums.append(("source.tar", sha(tar)))
        output.write("SHA256SUMS", "".join(digest + "  " + name + "\n" for name, digest in checksums).encode())
        return manifest
    finally:
        repo.close()
        if assets:
            assets.close()
        if output:
            output.close()


def validate_manifest(manifest):
    if (type(manifest) is not dict or manifest.get("schema") != "anila.quick-builder.offline-preparation/v1"
            or manifest.get("package_kind") != "source-only-preparation" or manifest.get("delivery_ready") is not False
            or manifest.get("deployment_validated") is not False or manifest.get("deployment_ready") is not False
            or manifest.get("source_revision_verified") is not False or manifest.get("tool_version") != TOOL_VERSION
            or type(manifest.get("archive_included")) is not bool):
        raise UnsafeInput("Invalid preparation manifest")
    if (manifest.get("coverage_baseline") != COVERAGE_BASELINE or manifest.get("coverage_inventory_verified") is not False
            or manifest.get("omitted_changed_paths") != [{"path": path, "reason": "test/evidence outside source selection"} for path in OMITTED_FILES]):
        raise UnsafeInput("Invalid frozen coverage inventory")
    for field, expected in (("files", {(p, p) for p in SOURCE_FILES}),
                            ("assets", set(ASSETS) | {("sbom:repo", "sbom/repo.cdx.json")})):
        rows = manifest.get(field)
        if type(rows) is not list or len(rows) != len(expected):
            raise UnsafeInput("Manifest entries do not match fixed allowlist")
        seen = set()
        for item in rows:
            if (type(item) is not dict or set(item) != {"id", "path", "status", "size", "sha256"}
                    or type(item.get("id")) is not str or type(item.get("status")) is not str):
                raise UnsafeInput("Invalid inventory entry")
            relative(item.get("path"))
            pair = (item.get("id"), item["path"])
            if pair not in expected or pair in seen or item.get("status") not in {"present", "missing"}:
                raise UnsafeInput("Unapproved or duplicate inventory entry")
            seen.add(pair)
            if item["status"] == "present":
                maximum = MAX_SOURCE_FILE if field == "files" or item["id"] == "sbom:repo" else MAX_ASSET_FILE
                if (type(item.get("size")) is not int or not 0 <= item["size"] <= maximum
                        or not isinstance(item.get("sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", item["sha256"])):
                    raise UnsafeInput("Invalid size or checksum")
            elif item.get("size") is not None or item.get("sha256") is not None:
                raise UnsafeInput("Missing item must not claim a checksum")
    if [item["path"] for item in manifest["files"]] != list(SOURCE_FILES):
        raise UnsafeInput("Source inventory ordering differs from the fixed selection")
    if (sum(item["size"] or 0 for item in manifest["files"]) > MAX_SOURCE_TOTAL
            or sum(item["size"] or 0 for item in manifest["assets"]) > MAX_ASSET_TOTAL):
        raise UnsafeInput("Inventory exceeds total size limits")
    claim = manifest.get("revision_claim")
    if not isinstance(claim, dict) or set(claim) != {"value", "verified"} or claim["verified"] is not False:
        raise UnsafeInput("Revision claim is not explicitly unverified")
    revision = claim["value"]
    if revision is not None and (not isinstance(revision, str) or not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", revision)):
        raise UnsafeInput("Invalid source revision claim")
    images = manifest.get("declared_images")
    if not isinstance(images, list) or len(images) > 50:
        raise UnsafeInput("Invalid image inventory")
    services = set()
    for image in images:
        if (not isinstance(image, dict) or set(image) != {"service", "artifact", "declared_reference", "observed_image_id", "identity_verified"}
                or image["observed_image_id"] is not None or image["identity_verified"] is not False
                or not isinstance(image["artifact"], str)
                or image["artifact"] not in {"images/" + name + ".tar.gz" for name in IMAGE_NAMES}
                or not isinstance(image["service"], str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,80}", image["service"])
                or image["service"] in services):
            raise UnsafeInput("Unverified or invalid image identity claim")
        services.add(image["service"])
    if (type(manifest.get("selection_complete")) is not bool or type(manifest.get("asset_files_complete")) is not bool
            or manifest.get("selection_complete") != all(item["status"] == "present" for item in manifest["files"])
            or manifest.get("asset_files_complete") != all(item["status"] == "present" for item in manifest["assets"])):
        raise UnsafeInput("False completeness claim")


def verify(bundle_path, *, repo_path=None, assets_path=None, repo_sbom=None):
    bundle, repo = Root(bundle_path), None
    try:
        encoded = bundle.read("manifest.json", maximum=MAX_MANIFEST)
        manifest = json.loads(encoded)
        validate_manifest(manifest)
        if canonical(manifest) != encoded:
            raise UnsafeInput("Manifest must use canonical JSON")
        expected = {"manifest.json": sha(encoded)}
        tar = None
        if manifest["archive_included"]:
            tar = bundle.read("source.tar", maximum=MAX_SOURCE_TOTAL + 2 * MAX_MANIFEST)
            expected["source.tar"] = sha(tar)
        lines = bundle.read("SHA256SUMS", maximum=1024).decode().splitlines()
        wanted = [digest + "  " + name for name, digest in expected.items()]
        if lines != wanted:
            raise UnsafeInput("Bundle checksum mismatch")
        source_images = None
        selected = {"source/" + item["path"]: item for item in manifest["files"] if item["status"] == "present"}
        if manifest["archive_included"]:
            member_order = ["manifest.json", *selected]
            rebuilt = io.BytesIO()
            with tarfile.open(fileobj=io.BytesIO(tar), mode="r:") as archive, tarfile.open(
                fileobj=rebuilt, mode="w", format=tarfile.USTAR_FORMAT,
            ) as canonical_tar:
                seen, total = set(), 0
                for member in archive:
                    relative(member.name)
                    if (member.name not in selected and member.name != "manifest.json") or member.name in seen:
                        raise UnsafeInput("Unapproved archive member")
                    if len(seen) >= len(member_order) or member.name != member_order[len(seen)]:
                        raise UnsafeInput("Archive member ordering differs from the fixed selection")
                    if not member.isreg() or member.linkname or member.pax_headers:
                        raise UnsafeInput("Only regular source members are allowed")
                    if (member.uid != 0 or member.gid != 0 or member.mtime != 0 or member.mode != 0o644
                            or member.uname or member.gname):
                        raise UnsafeInput("Nondeterministic archive attributes")
                    maximum = MAX_MANIFEST if member.name == "manifest.json" else selected[member.name]["size"]
                    if member.size != maximum and member.name != "manifest.json":
                        raise UnsafeInput("Archive size mismatch")
                    if member.size > maximum:
                        raise UnsafeInput("Archive member too large")
                    total += member.size
                    if total > MAX_SOURCE_TOTAL + MAX_MANIFEST:
                        raise UnsafeInput("Archive exceeds total size limit")
                    raw = archive.extractfile(member).read(maximum + 1)
                    if member.name == "manifest.json":
                        if raw != encoded:
                            raise UnsafeInput("Archive manifest mismatch")
                    elif sha(raw) != selected[member.name]["sha256"]:
                        raise UnsafeInput("Archive source checksum mismatch")
                    if member.name == "source/scripts/release/images.tsv":
                        source_images = parse_images(raw.decode("utf-8"))
                    info = tarfile.TarInfo(member.name)
                    info.size, info.mode, info.mtime = len(raw), 0o644, 0
                    info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    canonical_tar.addfile(info, io.BytesIO(raw))
                    seen.add(member.name)
                if seen != set(selected) | {"manifest.json"}:
                    raise UnsafeInput("Missing archive members")
            if rebuilt.getvalue() != tar:
                raise UnsafeInput("Archive contains noncanonical headers, padding or trailing data")
        else:
            if repo_path is None:
                raise UnsafeInput("Manifest-only verification requires --repo")
            repo = Root(repo_path)
            for item in selected.values():
                size, digest = repo.digest(item["path"], maximum=MAX_SOURCE_FILE)
                if size != item["size"] or digest != item["sha256"]:
                    raise UnsafeInput("Source checksum mismatch")
            if "source/scripts/release/images.tsv" in selected:
                source_images = declared_images(repo)
        if manifest["declared_images"] != (source_images or []):
            raise UnsafeInput("Image declarations do not match the checksummed source")
        if assets_path is not None or repo_sbom is not None:
            assets = Root(assets_path) if assets_path else None
            try:
                for item in manifest["assets"]:
                    if item["status"] != "present":
                        continue
                    explicit = item["id"] == "sbom:repo" and repo_sbom is not None
                    supplied = Root(os.path.dirname(repo_sbom)) if explicit else assets
                    if supplied is None:
                        raise UnsafeInput("Present asset requires its explicit verification root")
                    try:
                        path = os.path.basename(repo_sbom) if explicit else item["path"]
                        size, digest = supplied.digest(path, maximum=MAX_SOURCE_FILE if item["id"] == "sbom:repo" else MAX_ASSET_FILE)
                        if size != item["size"] or digest != item["sha256"]:
                            raise UnsafeInput("Asset checksum mismatch")
                    finally:
                        if explicit:
                            supplied.close()
            finally:
                if assets:
                    assets.close()
        return manifest
    finally:
        bundle.close()
        if repo:
            repo.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("build")
    create.add_argument("--repo", required=True)
    create.add_argument("--out", required=True, help="Existing empty output directory; no overwrites")
    create.add_argument("--assets-root", help="Only fixed ASSETS paths are inspected")
    create.add_argument("--repo-sbom", help="Explicit pre-existing .cdx.json; no SBOM generation")
    create.add_argument("--revision", help="Explicit claimed full source commit; not verified by this tool")
    create.add_argument("--tar", action="store_true", help="Include only the selected source tar")
    check = commands.add_parser("verify")
    check.add_argument("--bundle", required=True)
    check.add_argument("--repo", help="Required only when no source.tar exists")
    check.add_argument("--assets-root", help="Also verify every present external asset checksum")
    check.add_argument("--repo-sbom", help="Explicit SBOM used at build time, to verify its checksum")
    args = parser.parse_args()
    try:
        if args.command == "build":
            manifest = build(args.repo, args.out, assets_path=args.assets_root, archive=args.tar, repo_sbom=args.repo_sbom, revision=args.revision)
        else:
            manifest = verify(args.bundle, repo_path=args.repo, assets_path=args.assets_root, repo_sbom=args.repo_sbom)
    except (OSError, ValueError, tarfile.TarError, RecursionError):
        print("Release preparation rejected: unsafe, changed, oversized or invalid input.", file=sys.stderr)
        return 2
    print(json.dumps({"result": "prepared" if args.command == "build" else "checksums-consistent",
                      "package_kind": manifest["package_kind"], "selection_complete": manifest["selection_complete"],
                      "deployment_ready": False, "delivery_ready": False,
                      "present_asset_checksums_verified": args.command == "verify" and bool(args.assets_root or args.repo_sbom),
                      "missing_source_count": sum(item["status"] == "missing" for item in manifest["files"]),
                      "missing_assets": [item["id"] for item in manifest["assets"] if item["status"] == "missing"]},
                     ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
