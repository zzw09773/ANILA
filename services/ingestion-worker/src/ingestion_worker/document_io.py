"""Non-blocking boundary for document filesystem reads and parsing.

Parser implementations include CPU-heavy and blocking system-library work
(PDF, Office, image and legacy ``.doc`` extraction).  Arq runs multiple jobs on
one asyncio loop, so doing that work inline stalls heartbeats, cancellations
and unrelated jobs.  Keep the entire read + parse operation in one worker
thread; callers only receive the already-materialised parser result.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from ingestion_worker.parsers import extract_text


class DocumentParseTimeout(TimeoutError):
    """The side-effect-free parser thread exceeded its configured deadline."""


def _read_and_extract_sync(
    storage_path: str,
    filename: str,
    mime_type: str | None,
) -> tuple[str, dict[str, Any], Any]:
    blob = Path(storage_path).read_bytes()
    return extract_text(filename, blob, mime_type)


async def read_and_extract(
    storage_path: str,
    filename: str,
    mime_type: str | None,
    *,
    timeout_seconds: float,
) -> tuple[str, dict[str, Any], Any]:
    """Read and parse one document without blocking the Arq event loop.

    Python cannot forcibly stop a running worker thread. The thread boundary
    therefore contains only file read and pure parsing: it has no DB pool and
    cannot publish a late side effect after timeout or cancellation. Its result
    is accepted only while this coroutine still owns the await.
    """

    try:
        async with asyncio.timeout(timeout_seconds):
            return await asyncio.to_thread(
                _read_and_extract_sync,
                storage_path,
                filename,
                mime_type,
            )
    except TimeoutError as exc:
        raise DocumentParseTimeout(
            f"document parser exceeded {timeout_seconds:g} seconds"
        ) from exc


__all__ = ["DocumentParseTimeout", "read_and_extract"]
