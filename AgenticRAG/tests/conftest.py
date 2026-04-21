"""Pytest config — make ``api`` importable without DB / LLM side effects.

`api.py` lives at the repo root (not in a ``src/`` layout), so tests need the
parent directory on ``sys.path``. We also set a ``CSP_SERVICE_TOKEN=""`` default
so the module's dev-mode fallback is taken and the fail-fast guard does not
trigger during import.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

os.environ.setdefault("CSP_SERVICE_TOKEN", "")
os.environ.setdefault("DATABASE_URL", "postgresql://noop@localhost:1/noop")
