#!/usr/bin/env python3
# ruff: noqa: E402 -- Guard before importing the existing third-party validator.
"""Offline CycloneDX 1.6 schema validation using an already-installed jsonschema."""
import os
import sys


def _guard(event, args):
    if event == "socket.connect":
        raise PermissionError("Schema validation must remain offline")
    if event == "open" and args and isinstance(args[0], (str, bytes, os.PathLike)):
        parts = os.fsdecode(args[0]).replace("\\", "/").split("/")
        if ("secrets" in parts or parts[-1].startswith(".env")
                or parts[-1] == ".npmrc" or parts[-1].endswith((".pem", ".key"))):
            raise PermissionError("Protected file access denied")


sys.addaudithook(_guard)

import argparse
import hashlib
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bom", type=Path, required=True)
    parser.add_argument("--schema-dir", type=Path, required=True)
    parser.add_argument("--site-packages", type=Path, required=True,
                        help="Explicit existing jsonschema distribution root; no site/.pth execution")
    args = parser.parse_args()
    if os.getcwd() != "/tmp":
        raise SystemExit("Run offline schema validation from /tmp")
    root = args.site_packages.resolve(strict=True)
    if root.name not in {"site-packages", "dist-packages"}:
        raise SystemExit("Explicit distribution root must end in site-packages or dist-packages")
    sys.path.append(str(root))
    from jsonschema import Draft7Validator, FormatChecker
    from referencing import Registry, Resource

    schemas, evidence = {}, {}
    for name in ("bom-1.6.schema.json", "spdx.schema.json", "jsf-0.82.schema.json"):
        raw = (args.schema_dir / name).read_bytes()
        schema = json.loads(raw)
        schemas["http://cyclonedx.org/schema/" + name] = Resource.from_contents(schema)
        evidence[name] = hashlib.sha256(raw).hexdigest()
    primary = json.loads((args.schema_dir / "bom-1.6.schema.json").read_text())
    Draft7Validator.check_schema(primary)
    validator = Draft7Validator(primary, registry=Registry().with_resources(schemas.items()),
                               format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(json.loads(args.bom.read_text())), key=lambda error: str(error.path))
    if errors:
        for error in errors[:20]:
            # Do not echo untrusted manifest values or exception text.
            print(json.dumps({"instance_path": list(error.absolute_path), "schema_path": list(error.absolute_schema_path)}))
        raise SystemExit(f"Schema validation failed: {len(errors)} errors")
    print(json.dumps({"validation": "CycloneDX 1.6 official JSON schema with format checks",
                      "schema_sha256": evidence, "network": "blocked", "errors": 0}, sort_keys=True))


if __name__ == "__main__":
    main()
