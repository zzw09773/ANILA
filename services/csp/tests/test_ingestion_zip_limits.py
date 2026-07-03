"""Zip upload preflight limits.

Large zip members should be rejected from metadata before decompression so a
small archive cannot force the server to inflate attacker-controlled bytes
only to reject them afterward.
"""

from __future__ import annotations

import zipfile

from app.api.ingestion import documents


def _member(name: str, size: int) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name)
    info.file_size = size
    return info


def test_declared_zip_member_over_file_cap_is_skipped_before_read() -> None:
    result = documents._declared_zip_member_error(
        _member("huge.bin", documents._MAX_BYTES + 1),
        "huge.bin",
        cumulative_bytes=0,
    )
    assert result is not None
    assert result.status == "too_large"
    assert "exceeds" in (result.detail or "")


def test_declared_zip_member_over_total_cap_is_skipped_before_read() -> None:
    result = documents._declared_zip_member_error(
        _member("tail.bin", 10),
        "tail.bin",
        cumulative_bytes=documents._ZIP_MAX_TOTAL_BYTES - 5,
    )
    assert result is not None
    assert result.status == "skipped"
    assert "archive total" in (result.detail or "")
