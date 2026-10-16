#!/usr/bin/env python3
# ruff: noqa: E402 -- Install the audit guard before other imports.
"""Bounded file-only backup verification and fresh-directory restore rehearsal."""
import os
import sys


def protected(path):
    parts = os.fsdecode(path).replace("\\", "/").casefold().split("/")
    return any(
        part.startswith(".env")
        or part in {"secrets", "keys", "credentials", "logs", ".git", ".ssh", ".aws", ".npmrc"}
        or part.endswith((".pem", ".key", ".log", ".p12", ".pfx"))
        for part in parts
    )


def guard(event, args):
    if event == "open" and args and isinstance(args[0], (str, bytes, os.PathLike)) and protected(args[0]):
        raise PermissionError("Protected file denied")
    if event.startswith("socket.") or event in {
        "subprocess.Popen", "os.system", "os.posix_spawn", "os.exec", "os.fork", "os.forkpty",
        "os.spawn", "pty.spawn", "ctypes.dlopen",
    }:
        raise PermissionError("Network and process creation are disabled")


sys.dont_write_bytecode = True
sys.addaudithook(guard)

import argparse
import hashlib
import json
import re
import stat
import unicodedata

VERSION = "1.0.0"
SELECTION_SCHEMA = "anila.file-recovery-selection/v1"
MANIFEST_SCHEMA = "anila.file-recovery-manifest/v1"
MAX_ENTRIES = 512
MAX_FILE = 64 * 1024 * 1024
MAX_TOTAL = 256 * 1024 * 1024
MAX_JSON = 1024 * 1024
BLOCK = 1024 * 1024
CLAIMS = {
    "scope": "file-bytes-only",
    "database_consistency_verified": False,
    "authorization_restored": False,
    "worker_readiness_verified": False,
    "deployment_ready": False,
}


class RecoveryError(ValueError):
    """An input rejection with a fixed, non-sensitive code."""


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        print(json.dumps({"ok": False, "type": "invalid_arguments"}))
        raise SystemExit(1)


def reject(code):
    raise RecoveryError(code)


def canonical(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def checksum(value):
    if type(value) is not str or re.fullmatch(r"[a-f0-9]{64}", value) is None:
        reject("invalid_checksum")
    return value


def relative(path):
    if type(path) is not str or len(path) > 1536 or unicodedata.normalize("NFC", path) != path:
        reject("invalid_relative_path")
    parts = path.split("/")
    if not 1 <= len(parts) <= 16:
        reject("invalid_relative_path")
    for part in parts:
        if (not 1 <= len(part) <= 120 or not part[0].isalnum()
                or not all(c.isalnum() or c in "-_." for c in part) or protected(part)):
            reject("invalid_relative_path")
    return parts


def absolute(path):
    if type(path) is not str or not path.startswith("/") or path == "/" or protected(path):
        reject("invalid_root")
    parts = path[1:].split("/")
    if any(part in {"", ".", ".."} or "\\" in part or "\x00" in part for part in parts):
        reject("invalid_root")
    return parts


def directory(path):
    parts = absolute(path)
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


class Root:
    def __init__(self, path=None, *, fd=None):
        self.fd = directory(path) if fd is None else fd

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        os.close(self.fd)

    def parent(self, path, *, create=False):
        parts = relative(path)
        fd = os.dup(self.fd)
        try:
            for part in parts[:-1]:
                if create:
                    try:
                        os.mkdir(part, 0o700, dir_fd=fd)
                    except FileExistsError:
                        pass
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            return fd, parts[-1]
        except BaseException:
            os.close(fd)
            raise

    def open_read(self, path, maximum):
        parent, name = self.parent(path)
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        finally:
            os.close(parent)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 0 <= info.st_size <= maximum:
                reject("unsafe_or_oversized_file")
            return os.fdopen(fd, "rb"), info
        except BaseException:
            os.close(fd)
            raise

    def open_write(self, path):
        parent, name = self.parent(path, create=True)
        try:
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        finally:
            os.close(parent)
        return os.fdopen(fd, "wb")

    def read(self, path, maximum=MAX_JSON):
        handle, before = self.open_read(path, maximum)
        with handle:
            data = handle.read(maximum + 1)
            stable(handle, before)
        if len(data) != before.st_size or len(data) > maximum:
            reject("input_changed")
        return data

    def write(self, path, data):
        with self.open_write(path) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())

    def child(self, name):
        if len(relative(name)) != 1:
            reject("invalid_child")
        os.mkdir(name, 0o700, dir_fd=self.fd)
        return Root(fd=os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self.fd))


def stable(handle, before):
    after = os.fstat(handle.fileno())
    if any(getattr(before, field) != getattr(after, field) for field in (
        "st_dev", "st_ino", "st_size", "st_nlink", "st_mtime_ns", "st_ctime_ns",
    )):
        reject("input_changed")


def copy_checked(source, source_path, row, output=None, output_path=None):
    handle, before = source.open_read(source_path, min(MAX_FILE, row["size"]))
    target = None
    size, hashed = 0, hashlib.sha256()
    try:
        if before.st_size != row["size"]:
            reject("size_mismatch")
        if output is not None:
            target = output.open_write(output_path)
        with handle:
            while data := handle.read(BLOCK):
                size += len(data)
                if size > row["size"]:
                    reject("input_changed")
                hashed.update(data)
                if target is not None:
                    target.write(data)
            stable(handle, before)
        if size != row["size"] or hashed.hexdigest() != row["sha256"]:
            reject("content_mismatch")
        if target is not None:
            target.flush()
            os.fsync(target.fileno())
    finally:
        handle.close()
        if target is not None:
            target.close()


def no_duplicate_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            reject("duplicate_json_key")
        result[key] = value
    return result


def decode(data):
    if len(data) > MAX_JSON:
        reject("json_limit")
    try:
        return json.loads(data, object_pairs_hook=no_duplicate_pairs,
                          parse_constant=lambda _: reject("invalid_json_constant"))
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        reject("invalid_json")


def entries(value):
    if type(value) is not list or not 1 <= len(value) <= MAX_ENTRIES:
        reject("invalid_entries")
    paths, identities, hashes, total = set(), set(), {}, 0
    result = []
    for row in value:
        if type(row) is not dict or set(row) != {"category", "logical_id", "path", "size", "sha256"}:
            reject("invalid_entry")
        if row["category"] not in ("artifact", "ingestion"):
            reject("invalid_category")
        if (type(row["logical_id"]) is not str
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}", row["logical_id"]) is None):
            reject("invalid_logical_id")
        if len(relative(row["path"])) > 12 or len(row["path"]) > 1024:
            reject("invalid_relative_path")
        if type(row["size"]) is not int or not 0 <= row["size"] <= MAX_FILE:
            reject("invalid_size")
        checksum(row["sha256"])
        identity = (row["category"], row["logical_id"].casefold())
        path = row["path"].casefold()
        if identity in identities or path in paths:
            reject("duplicate_entry")
        for prior in paths:
            if path.startswith(prior + "/") or prior.startswith(path + "/"):
                reject("overlapping_path")
        if row["sha256"] in hashes and hashes[row["sha256"]] != row["size"]:
            reject("inconsistent_blob_size")
        identities.add(identity)
        paths.add(path)
        hashes[row["sha256"]] = row["size"]
        total += row["size"]
        if total > MAX_TOTAL:
            reject("total_limit")
        result.append(dict(row))
    return sorted(result, key=lambda row: (row["category"], row["logical_id"], row["path"]))


def selection_from(data):
    value = decode(data)
    if type(value) is not dict or set(value) != {"schema", "entries"} or value["schema"] != SELECTION_SCHEMA:
        reject("invalid_selection")
    return {"schema": SELECTION_SCHEMA, "entries": entries(value["entries"])}


def read_explicit(path):
    absolute(path)
    parent, name = os.path.split(path)
    with Root(parent) as root:
        return root.read(name)


def manifest_from(selection):
    return {
        "schema": MANIFEST_SCHEMA, "tool_version": VERSION,
        "selection_sha256": digest(canonical(selection)), "entries": selection["entries"], **CLAIMS,
    }


def new_output(path):
    absolute(path)
    parent, name = os.path.split(path)
    with Root(parent) as root:
        return root.child(name)


def disjoint(first, second):
    absolute(first)
    absolute(second)
    shared = os.path.commonpath((first, second))
    if shared in (first, second):
        reject("roots_overlap")


def backup(source_path, selection_path, output_path):
    disjoint(source_path, output_path)
    disjoint(selection_path, output_path)
    selection = selection_from(read_explicit(selection_path))
    manifest = manifest_from(selection)
    encoded = canonical(manifest)
    if len(encoded) > MAX_JSON:
        reject("json_limit")
    with Root(source_path) as source, new_output(output_path) as output:
        copied = set()
        for row in manifest["entries"]:
            blob = "blobs/" + row["sha256"]
            # Every source is checked, including files that share the same bytes.
            copy_checked(source, row["path"], row, output if blob not in copied else None, blob)
            copied.add(blob)
        for row in manifest["entries"]:
            copy_checked(output, "blobs/" + row["sha256"], row)
        output.write("manifest.json", encoded)
        output.write("COMPLETE.json", canonical({"schema": "anila.file-backup-complete/v1", "manifest_sha256": digest(encoded)}))
        os.fsync(output.fd)
    return {"ok": True, "type": "backup_verified", "manifest_sha256": digest(encoded),
            "file_count": len(manifest["entries"]), "total_bytes": sum(row["size"] for row in manifest["entries"]), **CLAIMS}


def verify_root(root, expected):
    checksum(expected)
    encoded = root.read("manifest.json")
    if digest(encoded) != expected:
        reject("manifest_pin_mismatch")
    value = decode(encoded)
    if type(value) is not dict or set(value) != {"schema", "tool_version", "selection_sha256", "entries", *CLAIMS}:
        reject("invalid_manifest")
    selection = {"schema": SELECTION_SCHEMA, "entries": entries(value["entries"])}
    if (value != manifest_from(selection) or canonical(value) != encoded
            or any(value[key] is not False for key in CLAIMS if key != "scope")):
        reject("invalid_manifest")
    complete = root.read("COMPLETE.json", maximum=512)
    if complete != canonical({"schema": "anila.file-backup-complete/v1", "manifest_sha256": expected}):
        reject("incomplete_backup")
    checked = set()
    for row in value["entries"]:
        if row["sha256"] not in checked:
            copy_checked(root, "blobs/" + row["sha256"], row)
            checked.add(row["sha256"])
    return value


def verify(bundle_path, expected):
    with Root(bundle_path) as root:
        value = verify_root(root, expected)
    return {"ok": True, "type": "bundle_verified", "manifest_sha256": expected,
            "file_count": len(value["entries"]), "total_bytes": sum(row["size"] for row in value["entries"]), **CLAIMS}


def rehearse(bundle_path, expected, scratch_path):
    disjoint(bundle_path, scratch_path)
    with Root(bundle_path) as bundle, Root(scratch_path) as scratch:
        manifest = verify_root(bundle, expected)
        # Refuse a scratch directory containing any earlier data or rehearsal.
        if os.listdir(scratch.fd):
            reject("scratch_not_empty")
        with scratch.child("rehearsal-" + expected) as output:
            for row in manifest["entries"]:
                path = "data/" + row["category"] + "/" + row["logical_id"] + "/" + row["path"]
                copy_checked(bundle, "blobs/" + row["sha256"], row, output, path)
            # Read each restored file back before marking the rehearsal complete.
            for row in manifest["entries"]:
                path = "data/" + row["category"] + "/" + row["logical_id"] + "/" + row["path"]
                copy_checked(output, path, row)
            report = {"ok": True, "type": "file_restore_rehearsal_verified", "manifest_sha256": expected,
                      "file_count": len(manifest["entries"]), "total_bytes": sum(row["size"] for row in manifest["entries"]), **CLAIMS}
            output.write("REHEARSAL.json", canonical(report))
            os.fsync(output.fd)
    return report


def main():
    parser = SafeArgumentParser(prog="recovery.py", description="離線檔案備份校驗與新目錄還原演練；不還原資料庫或權限。")
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("backup")
    build.add_argument("--source-root", required=True)
    build.add_argument("--selection", required=True)
    build.add_argument("--out", required=True)
    check = commands.add_parser("verify")
    drill = commands.add_parser("rehearse")
    for command in (check, drill):
        command.add_argument("--bundle", required=True)
        command.add_argument("--manifest-sha256", required=True)
    drill.add_argument("--scratch", required=True)
    args = parser.parse_args()
    try:
        if args.command == "backup":
            result = backup(args.source_root, args.selection, args.out)
        elif args.command == "verify":
            result = verify(args.bundle, args.manifest_sha256)
        else:
            result = rehearse(args.bundle, args.manifest_sha256, args.scratch)
    except RecoveryError as exc:
        print(json.dumps({"ok": False, "type": str(exc)}, ensure_ascii=False))
        return 1
    except (OSError, ValueError, TypeError, RecursionError):
        print(json.dumps({"ok": False, "type": "unsafe_or_unavailable_input"}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
