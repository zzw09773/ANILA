#!/usr/bin/env python3
# ruff: noqa: E402 -- Load the sibling audit guard before other imports.
"""Validate offline patch metadata without applying patches or approving sources."""
import os
import sys

sys.dont_write_bytecode = True
_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recovery.py")
_files = {"__name__": "recovery_support", "__file__": _path}
with open(_path, "rb") as _input:
    exec(compile(_input.read(), _path, "exec"), _files)

import json
import re
from urllib.parse import urlsplit

SCHEMA = "anila.offline-patch-plan/v1"
IMAGES = ("pgvector", "pgbouncer", "csp-credential-dirs", "csp", "redis", "ingestion-worker",
          "router", "nginx", "pptx-renderer", "hr-lookup", "anila-studio", "anilalm", "anila-ui",
          "codeserver", "n8n", "asr-gateway")
WORKER_CHECKS = ("parser-packages", "tesseract", "chi_tra", "eng", "libreoffice", "pandoc", "fonts",
                 "chromium", "offline-egress-policy")
reject = _files["reject"]
RecoveryError = _files["RecoveryError"]


def keys(value, expected):
    if type(value) is not dict or set(value) != set(expected):
        reject("plan_invalid_fields")


def text(value, maximum=160):
    if type(value) is not str or not 1 <= len(value) <= maximum or value.strip() != value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        reject("plan_invalid_text")


def optional_text(value, maximum=160):
    if value is not None:
        text(value, maximum)


def optional_hash(value):
    if value is not None:
        _files["checksum"](value)


def country(value):
    if value is not None and (type(value) is not str or re.fullmatch(r"[A-Z]{2}", value) is None):
        reject("plan_invalid_country_claim")


def known(value):
    return value is not None and value.casefold() not in {"unknown", "unverified", "pending", "n/a", "none", "tbd", "待核定", "未知"}


def source_url(value):
    if value is None:
        return
    text(value, 512)
    try:
        url = urlsplit(value)
        if (url.scheme != "https" or not url.hostname or url.username is not None or url.password is not None
                or url.query or url.fragment or url.port is not None or "\\" in value
                or any(c.isspace() for c in value)):
            reject("plan_unsafe_source_url")
    except ValueError:
        reject("plan_unsafe_source_url")


def validate(value):
    keys(value, {"schema", "base_revision", "target_revision", "source_bundle_sha256", "changes", "sources", "images", "worker", "acceptance"})
    if value["schema"] != SCHEMA:
        reject("plan_invalid_schema")
    for name in ("base_revision", "target_revision"):
        if type(value[name]) is not str or re.fullmatch(r"[a-f0-9]{40}", value[name]) is None:
            reject("plan_invalid_revision")
    if value["base_revision"] == value["target_revision"]:
        reject("plan_identical_revisions")
    optional_hash(value["source_bundle_sha256"])
    changes = value["changes"]
    if type(changes) is not list or not 1 <= len(changes) <= 512:
        reject("plan_invalid_changes")
    paths = set()
    for row in changes:
        keys(row, {"path", "before_sha256", "after_sha256"})
        _files["relative"](row["path"])
        optional_hash(row["before_sha256"])
        optional_hash(row["after_sha256"])
        if row["before_sha256"] == row["after_sha256"]:
            reject("plan_unchanged_or_empty_change")
        path = row["path"].casefold()
        if path in paths or any(path.startswith(prior + "/") or prior.startswith(path + "/") for prior in paths):
            reject("plan_duplicate_or_overlapping_change")
        paths.add(path)
    sources = value["sources"]
    if type(sources) is not list or not 1 <= len(sources) <= 512:
        reject("plan_invalid_sources")
    ids, gaps, source_records = set(), [], {}
    for row in sources:
        keys(row, {"id", "version", "artifact_sha256", "source_url", "publisher", "license_expression",
                   "author_country", "control_country", "approval"})
        if type(row["id"]) is not str or re.fullmatch(r"[a-z0-9][a-z0-9_.:-]{0,95}", row["id"]) is None or row["id"] in ids:
            reject("plan_invalid_source_id")
        ids.add(row["id"])
        source_records[row["id"]] = row
        for field in ("version", "publisher", "license_expression"):
            optional_text(row[field])
        optional_hash(row["artifact_sha256"])
        source_url(row["source_url"])
        country(row["author_country"])
        country(row["control_country"])
        approval = row["approval"]
        keys(approval, {"decision", "reviewer_id", "evidence_sha256"})
        if approval["decision"] not in ("pending", "approved", "rejected"):
            reject("plan_invalid_approval")
        optional_text(approval["reviewer_id"], 96)
        optional_hash(approval["evidence_sha256"])
        missing = [name for name in ("version", "artifact_sha256", "source_url", "publisher", "license_expression", "author_country", "control_country")
                   if not known(row[name])]
        if approval["decision"] != "approved":
            missing.append("approval.decision")
        for name in ("reviewer_id", "evidence_sha256"):
            if not known(approval[name]):
                missing.append("approval." + name)
        if missing:
            gaps.append({"id": row["id"], "missing": missing})
    images = value["images"]
    if type(images) is not list or len(images) != len(IMAGES):
        reject("plan_invalid_image_slots")
    seen_images, missing_images = set(), []
    for row in images:
        keys(row, {"id", "source_id", "archive_sha256", "image_digest", "sbom_sha256", "license_review_sha256"})
        if type(row["id"]) is not str or row["id"] not in IMAGES or row["id"] in seen_images:
            reject("plan_invalid_image_slots")
        seen_images.add(row["id"])
        if row["source_id"] != "image:" + row["id"] or row["source_id"] not in ids:
            reject("plan_missing_image_source_record")
        for name in ("archive_sha256", "sbom_sha256", "license_review_sha256"):
            optional_hash(row[name])
        source_hash = source_records[row["source_id"]]["artifact_sha256"]
        if row["archive_sha256"] is not None and source_hash is not None and row["archive_sha256"] != source_hash:
            reject("plan_image_source_hash_mismatch")
        if row["image_digest"] is not None and (type(row["image_digest"]) is not str
                or re.fullmatch(r"sha256:[a-f0-9]{64}", row["image_digest"]) is None):
            reject("plan_invalid_image_digest")
        missing = [name for name in ("archive_sha256", "image_digest", "sbom_sha256", "license_review_sha256") if row[name] is None]
        if missing:
            missing_images.append({"id": row["id"], "missing": missing})
    worker = value["worker"]
    keys(worker, {"scope", "image_digest", "checks"})
    if worker["scope"] != "target-worker-image":
        reject("plan_invalid_worker_scope")
    if worker["image_digest"] is not None and (type(worker["image_digest"]) is not str
            or re.fullmatch(r"sha256:[a-f0-9]{64}", worker["image_digest"]) is None):
        reject("plan_invalid_image_digest")
    worker_image = next(row for row in images if row["id"] == "ingestion-worker")
    if worker["image_digest"] != worker_image["image_digest"]:
        reject("plan_worker_image_mismatch")
    keys(worker["checks"], WORKER_CHECKS)
    missing_worker = []
    for name in WORKER_CHECKS:
        optional_hash(worker["checks"][name])
        if worker["checks"][name] is None:
            missing_worker.append(name)
    if worker["image_digest"] is None:
        missing_worker.append("image_digest")
    acceptance = value["acceptance"]
    keys(acceptance, {"source_verification_sha256", "file_restore_rehearsal_sha256", "regression_report_sha256", "rollback_review_sha256"})
    for item in acceptance.values():
        optional_hash(item)
    missing_acceptance = sorted(name for name, item in acceptance.items() if item is None)
    metadata_complete = (value["source_bundle_sha256"] is not None and not gaps and not missing_images
                         and not missing_worker and not missing_acceptance)
    return {
        "ok": True, "type": "offline_plan_structure_valid", "schema": SCHEMA,
        "metadata_scope": "declared-items-only",
        "plan_sha256": _files["digest"](_files["canonical"](value)),
        "metadata_complete": metadata_complete, "source_bundle_missing": value["source_bundle_sha256"] is None,
        "sources_missing": sorted(gaps, key=lambda row: row["id"]),
        "images_missing": sorted(missing_images, key=lambda row: row["id"]),
        "worker_checks_missing": sorted(missing_worker), "acceptance_missing": missing_acceptance,
        "image_slots": len(IMAGES), "change_count": len(changes),
        "evidence_references_verified": False, "source_approval_verified": False,
        "image_identity_verified": False, "worker_readiness_verified": False,
        "patch_apply_permitted": False, "deployment_ready": False,
    }


def main():
    parser = _files["SafeArgumentParser"](prog="delivery_plan.py", description="離線patch/來源核定清冊結構檢查；不套用patch、不認證核定或映像。")
    parser.add_argument("--plan", required=True)
    args = parser.parse_args()
    try:
        result = validate(_files["decode"](_files["read_explicit"](args.plan)))
    except RecoveryError as exc:
        print(json.dumps({"ok": False, "type": str(exc)}, ensure_ascii=False))
        return 1
    except (OSError, ValueError, TypeError, RecursionError, StopIteration):
        print(json.dumps({"ok": False, "type": "unsafe_or_unavailable_input"}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["metadata_complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
