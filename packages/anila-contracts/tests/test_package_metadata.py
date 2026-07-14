from __future__ import annotations

from pathlib import Path

import anila_contracts


def test_typed_package_marker_is_present() -> None:
    marker = Path(anila_contracts.__file__).with_name("py.typed")
    assert marker.is_file()
