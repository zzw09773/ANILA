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

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from ..memory.short_term.sqlite import _get_connection


@dataclass(frozen=True)
class SessionOwnerRecord:
    agent_id: str | None
    owner_key_hash: str | None


def fingerprint_session_owner(caller_api_key: str) -> str:
    """Stable non-reversible fingerprint for the caller credential."""
    return hashlib.sha256(caller_api_key.encode("utf-8")).hexdigest()


async def set_session_owner(
    db_path: str,
    session_id: str,
    agent_id: str,
    *,
    owner_key_hash: str | None = None,
) -> None:
    """Pin or update the agent that owns ``session_id``.

    Idempotent — the table uses ``session_id`` as primary key so repeat
    calls for the same session simply refresh ``agent_id`` and
    ``updated_at``. ``owner_key_hash`` is set on first write and is not
    overwritten by later agent handoffs.
    """
    conn = await _get_connection(db_path)
    now = datetime.now(timezone.utc).isoformat()
    await conn.execute(
        """
        INSERT INTO session_owners (
            session_id, agent_id, owner_key_hash, updated_at
        )
        VALUES (?, ?, ?, ?)
        ON CONFLICT(session_id) DO UPDATE SET
            agent_id = excluded.agent_id,
            owner_key_hash = COALESCE(
                session_owners.owner_key_hash,
                excluded.owner_key_hash
            ),
            updated_at = excluded.updated_at
        """,
        (session_id, agent_id, owner_key_hash, now),
    )
    await conn.commit()


async def ensure_session_owner(
    db_path: str, session_id: str, owner_key_hash: str
) -> bool:
    """Bind a session to a caller fingerprint, or verify the existing bind."""
    record = await get_session_owner_record(db_path, session_id)
    if record is None:
        await set_session_owner(
            db_path, session_id, "", owner_key_hash=owner_key_hash
        )
        persisted = await get_session_owner_record(db_path, session_id)
        return persisted is not None and persisted.owner_key_hash == owner_key_hash
    if record.owner_key_hash is None:
        if record.agent_id:
            # Legacy rows created before owner binding cannot prove the
            # requester is the original dispatcher. Fail closed instead of
            # letting whoever knows the UUID claim the live agent session.
            return False
        await set_session_owner(
            db_path,
            session_id,
            record.agent_id or "",
            owner_key_hash=owner_key_hash,
        )
        persisted = await get_session_owner_record(db_path, session_id)
        return persisted is not None and persisted.owner_key_hash == owner_key_hash
    return record.owner_key_hash == owner_key_hash


async def get_session_owner_record(
    db_path: str, session_id: str
) -> Optional[SessionOwnerRecord]:
    """Return owning agent and caller fingerprint for ``session_id``."""
    conn = await _get_connection(db_path)
    cursor = await conn.execute(
        """
        SELECT agent_id, owner_key_hash
        FROM session_owners
        WHERE session_id = ?
        """,
        (session_id,),
    )
    try:
        row = await cursor.fetchone()
    finally:
        await cursor.close()
    if row is None:
        return None
    agent_id = str(row[0]) if row[0] else None
    owner_key_hash = str(row[1]) if row[1] else None
    return SessionOwnerRecord(agent_id=agent_id, owner_key_hash=owner_key_hash)


async def get_session_owner(
    db_path: str, session_id: str
) -> Optional[str]:
    """Return the ``agent_id`` that owns ``session_id``, or None."""
    record = await get_session_owner_record(db_path, session_id)
    if record is None:
        return None
    return record.agent_id
