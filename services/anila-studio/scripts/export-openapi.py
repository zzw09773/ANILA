#!/usr/bin/env python3
"""Export anila-studio's OpenAPI schema to a JSON file.

ANILALM's TypeScript codegen step consumes this. The export does not
require the service to be running — we import ``app`` and call
``app.openapi()`` directly. Lifespan handlers are not invoked, so JWKS /
Redis are not touched.

Usage:
    cd anila-studio
    .venv/bin/python scripts/export-openapi.py
    # writes openapi/studio.openapi.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    # Make sure we resolve the anila-studio package even when invoked
    # from the repo root.
    here = Path(__file__).resolve().parent.parent
    if str(here) not in sys.path:
        sys.path.insert(0, str(here))

    from app.main import app  # noqa: E402 — path mutated above

    schema = app.openapi()
    out_path = here / "openapi" / "studio.openapi.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(schema, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8")
    print(f"wrote {out_path} ({len(schema.get('paths', {}))} paths)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
