"""Short-term memory — a thin wrapper over openai-agents `SQLiteSession`.

Per-session conversation history. The session_id is the dedup key; identical IDs
load the same history. Storage path comes from `configs/memory.yaml`.
"""

from __future__ import annotations

from pathlib import Path

from agents.memory.sqlite_session import SQLiteSession


def open_session(session_id: str, db_path: str | Path) -> SQLiteSession:
    """Create or reopen a SQLite-backed session.

    Args:
        session_id: Unique-per-conversation key. Reusing an ID resumes that conversation.
        db_path: Path to the SQLite file. Parent directories are created on demand.
    """
    if not session_id:
        raise ValueError("session_id must be a non-empty string")
    p = Path(db_path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    return SQLiteSession(session_id=session_id, db_path=str(p))
