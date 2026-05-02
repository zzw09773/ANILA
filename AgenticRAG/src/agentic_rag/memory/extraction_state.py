"""Persistent extraction cursor — survives SSE pod restarts.

The ``MemoryExtractor`` tracks ``last_message_uuid`` so post-turn
extraction only processes messages that arrived since the last run.
Without persistence the cursor is in-memory only — when the pod
restarts (GPU preemption, deploy rollover, OOM kill) every still-open
session would re-extract from the start of its message history,
producing duplicate memories and burning local-inference tokens for
no benefit.

This module is the durable backing: a small JSON file the cursor gets
flushed to after each successful extraction. On startup, the
``MemoryExtractor`` reads the file and resumes where it left off.

Design choices:
- One file per session (key = session_id) under ``state_dir/``.
  Avoids cross-session lock contention; matches AgenticRAG's
  per-request model.
- Stale-cursor reset (>24h by default) — if the cursor is older than
  ``stale_after_seconds`` it's treated as missing and extraction
  starts from scratch. Catches the case where a cursor pointed at a
  message that's since been compacted away.
- Best-effort writes: file I/O failures are logged, never raised.
  Extraction continues with the in-memory cursor; the persistence
  layer is a hint, not a hard guarantee.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


_DEFAULT_STALE_SECONDS = 24 * 60 * 60  # 24 hours


@dataclass(frozen=True)
class CursorRecord:
    """One persisted extraction cursor."""

    session_id: str
    last_message_uuid: str
    saved_at: float  # Unix epoch seconds

    def is_stale(self, *, now: float | None = None, stale_after_seconds: float | None = None) -> bool:
        cutoff = stale_after_seconds if stale_after_seconds is not None else _DEFAULT_STALE_SECONDS
        actual_now = now if now is not None else time.time()
        return (actual_now - self.saved_at) > cutoff


class CursorStore:
    """File-backed extraction cursor store.

    Per-session JSON files at ``state_dir/<session_id>.json`` shaped::

        {"session_id": "...", "last_message_uuid": "...", "saved_at": 1234567890.123}

    The store creates ``state_dir`` lazily on first write — no need for
    callers to ensure the directory exists. Reads of a non-existent
    file return ``None`` (not an error) so first-time sessions Just
    Work.
    """

    def __init__(
        self,
        state_dir: str | Path,
        *,
        stale_after_seconds: float | None = None,
    ) -> None:
        self._state_dir = Path(state_dir)
        self._stale_after = (
            stale_after_seconds
            if stale_after_seconds is not None
            else _DEFAULT_STALE_SECONDS
        )

    def get(self, session_id: str) -> Optional[CursorRecord]:
        """Return the cursor for ``session_id``, or ``None`` if missing / stale.

        Stale records are NOT auto-deleted — that would couple the read
        path to disk writes and complicate concurrent reads. Callers
        that want to clean up stale state can use ``delete()``.
        """
        path = self._path_for(session_id)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("cursor read failed for %s: %s", session_id, exc)
            return None

        try:
            record = CursorRecord(
                session_id=str(data["session_id"]),
                last_message_uuid=str(data["last_message_uuid"]),
                saved_at=float(data["saved_at"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("cursor file %s malformed: %s", path, exc)
            return None

        if record.is_stale(stale_after_seconds=self._stale_after):
            logger.info(
                "cursor for %s is stale (saved_at=%s); treating as missing",
                session_id,
                record.saved_at,
            )
            return None
        return record

    def set(self, session_id: str, last_message_uuid: str) -> None:
        """Persist the cursor for ``session_id``.

        Best-effort: write failures log a warning and return. The
        in-memory state in ``MemoryExtractor`` continues to track
        forward progress even if the disk copy gets out of date.
        """
        if not last_message_uuid:
            return
        record = CursorRecord(
            session_id=session_id,
            last_message_uuid=last_message_uuid,
            saved_at=time.time(),
        )
        path = self._path_for(session_id)
        try:
            self._state_dir.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(
                    {
                        "session_id": record.session_id,
                        "last_message_uuid": record.last_message_uuid,
                        "saved_at": record.saved_at,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            # Atomic rename so a half-written file never replaces a good one.
            os.replace(tmp, path)
        except OSError as exc:
            logger.warning("cursor write failed for %s: %s", session_id, exc)

    def delete(self, session_id: str) -> None:
        """Remove the persisted cursor (if any). No-op when missing."""
        path = self._path_for(session_id)
        try:
            path.unlink()
        except FileNotFoundError:
            return
        except OSError as exc:
            logger.warning("cursor delete failed for %s: %s", session_id, exc)

    def _path_for(self, session_id: str) -> Path:
        # Filesystem-safe slug — sessions named with ``/`` or ``..`` would
        # otherwise escape the state dir.
        safe = "".join(
            ch if ch.isalnum() or ch in "-_." else "_" for ch in session_id
        )
        if not safe:
            safe = "default"
        return self._state_dir / f"{safe}.json"


__all__ = ["CursorRecord", "CursorStore"]
