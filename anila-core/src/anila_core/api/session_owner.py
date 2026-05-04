"""Sprint 13 PR A2 — per-session owning-agent persistence.

Every time the Router dispatches a query to an agent it pins the
``session_id → agent_id`` pair in a small SQLite table sitting next to
:class:`anila_core.memory.sqlite_session.SqliteSession`. The Router's
new ``POST /v1/sessions/{session_id}/answer`` resume endpoint reads
this mapping to know which agent to forward the user's answer to.

The table lives in the same SQLite file as ``session_items`` and
``session_interrupts`` so that operators have one durable artefact to
back up / clean up — see ``_SCHEMA`` in ``sqlite_session``.

Last-writer-wins on (session_id) — under normal multi-turn use the
same agent owns the session, but explicit handoffs may rewrite it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from ..memory.short_term.sqlite import _get_connection


async def set_session_owner(
    db_path: str, session_id: str, agent_id: str
) -> None:
    """Pin or update the agent that owns ``session_id``.

    Idempotent — the table uses ``session_id`` as primary key so repeat
    calls for the same session simply refresh ``agent_id`` and
    ``updated_at``.
    """
    conn = await _get_connection(db_path)
    now = datetime.now(timezone.utc).isoformat()
    await conn.execute(
        """
        INSERT INTO session_owners (session_id, agent_id, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(session_id) DO UPDATE SET
            agent_id = excluded.agent_id,
            updated_at = excluded.updated_at
        """,
        (session_id, agent_id, now),
    )
    await conn.commit()


async def get_session_owner(
    db_path: str, session_id: str
) -> Optional[str]:
    """Return the ``agent_id`` that owns ``session_id``, or None."""
    conn = await _get_connection(db_path)
    cursor = await conn.execute(
        "SELECT agent_id FROM session_owners WHERE session_id = ?",
        (session_id,),
    )
    try:
        row = await cursor.fetchone()
    finally:
        await cursor.close()
    if row is None:
        return None
    return str(row[0])
