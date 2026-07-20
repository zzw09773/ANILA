#!/usr/bin/env python3
"""CLI shim for the production backup implementation."""

from __future__ import annotations

import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "infra/deployment/backup"))

from production_backup import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
