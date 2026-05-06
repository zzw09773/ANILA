from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture()
def memory_dir(tmp_path: Path) -> Path:
    d = tmp_path / "memory"
    d.mkdir()
    return d
