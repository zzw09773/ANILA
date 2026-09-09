#!/usr/bin/env python3
"""Fail the CSP image build if vendored Swagger UI bytes drift."""
from __future__ import annotations

import hashlib
import pathlib
import sys

EXPECTED = {
    "app/static/swagger-ui-bundle.js": "c50b94bbc4f02394326fb7aed1f4fb693b3677f4b3d3344e0d6131808cbf281f",
    "app/static/swagger-ui.css": "8f33d996025317049d4a9864f421eab2b2a247872f388026fa94c654913259e7",
}


def main() -> int:
    failed = False
    for path, exp in EXPECTED.items():
        p = pathlib.Path(path)
        if not p.is_file():
            print(f"FATAL: {path} missing", file=sys.stderr)
            failed = True
            continue
        got = hashlib.sha256(p.read_bytes()).hexdigest()
        if got != exp:
            print(f"FATAL: {path} hash {got} != {exp}", file=sys.stderr)
            failed = True
    if failed:
        print("Swagger UI 靜態檔與 swagger-ui-dist@5.18.2 的雜湊對不上。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
