"""aiosqlite-backed :class:`Session` adapter.

Default Session implementation for single-process anila-core deployments.
Multi-process / HA setups should swap in a Postgres- or Redis-backed
adapter implementing the :class:`Session` Protocol.

Schema (auto-created on first use):

.. code-block:: sql

    CREATE TABLE session_items (
        rowid INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        item_json TEXT NOT NULL
    );
    CREATE INDEX idx_session_items_sid ON session_items(session_id, rowid);

    CREATE TABLE session_interrupts (
        rowid INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        interrupt_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE (session_id, interrupt_id)
    );
    CREATE INDEX idx_session_interrupts_sid
        ON session_interrupts(session_id, rowid);

Items use ROWID-ordered storage so insertion order is preserved without
the caller needing to track sequence numbers.

Connection sharing: a module-level cache keyed by the resolved DB path
holds at most one ``aiosqlite.Connection`` per file. This avoids "database
is locked" errors when many ``SqliteSession`` instances target the same
file. Call :func:`close_all_connections` in test teardown if you need a
clean shutdown.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from ..models.message import AssistantMessage, Message, UserMessage
from .session import InterruptRecord

if TYPE_CHECKING:
    import aiosqlite


_SCHEMA = """
CREATE TABLE IF NOT EXISTS session_items (
    rowid INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    item_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_session_items_sid
    ON session_items(session_id, rowid);

CREATE TABLE IF NOT EXISTS session_interrupts (
    rowid INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    interrupt_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (session_id, interrupt_id)
);
CREATE INDEX IF NOT EXISTS idx_session_interrupts_sid
    ON session_interrupts(session_id, rowid);

-- Sprint 13 PR A2: per-session ownership mapping. The Router writes
-- (session_id, agent_id) every time it dispatches a query so the
-- ``POST /v1/sessions/{id}/answer`` resume path knows which agent to
-- forward to. Last-writer-wins; a session that crosses agents (rare
-- under normal use, but possible after explicit handoff) updates here.
CREATE TABLE IF NOT EXISTS session_owners (
    session_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


# Module-level connection cache: resolved path → aiosqlite.Connection.
# Guarded by a single lock; per-path lazy init avoids races on first use.
_conn_cache: dict[str, "aiosqlite.Connection"] = {}
_conn_cache_lock = asyncio.Lock()


async def _get_connection(db_path: str) -> "aiosqlite.Connection":
    cached = _conn_cache.get(db_path)
    if cached is not None:
        return cached
    async with _conn_cache_lock:
        cached = _conn_cache.get(db_path)
        if cached is not None:
            return cached
        import aiosqlite
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(db_path)
        # `executescript` runs the multi-statement schema in one call; commit
        # afterwards so the cache observers see a consistent DB.
        await conn.executescript(_SCHEMA)
        await conn.commit()
        _conn_cache[db_path] = conn
        return conn


async def close_all_connections() -> None:
    """Close every cached connection. Intended for test teardown."""
    async with _conn_cache_lock:
        for conn in _conn_cache.values():
            await conn.close()
        _conn_cache.clear()


def _serialize_item(item: Message) -> str:
    return item.model_dump_json()


def _deserialize_item(blob: str) -> Message:
    data = json.loads(blob)
    role = data.get("role")
    if role == "user":
        return UserMessage.model_validate(data)
    if role == "assistant":
        return AssistantMessage.model_validate(data)
    raise ValueError(f"Unknown message role: {role!r}")


class SqliteSession:
    """aiosqlite-backed Session.

    Args:
        db_path: filesystem path to the SQLite file. Created if missing.
            Use ``:memory:`` for an ephemeral DB (still cached per-process).
        session_id: opaque session identifier.
    """

    def __init__(self, db_path: str | Path, session_id: str) -> None:
        if str(db_path) == ":memory:":
            # Memory DBs are per-connection — give each instance a unique
            # cache key so they don't accidentally share state and so the
            # cache cleanup remains tractable.
            self._db_path = f":memory:#{id(self)}"
        else:
            self._db_path = str(Path(db_path).resolve())
        self.session_id = session_id

    async def _conn(self) -> "aiosqlite.Connection":
        if self._db_path.startswith(":memory:#"):
            cached = _conn_cache.get(self._db_path)
            if cached is None:
                async with _conn_cache_lock:
                    cached = _conn_cache.get(self._db_path)
                    if cached is None:
                        import aiosqlite
                        cached = await aiosqlite.connect(":memory:")
                        await cached.executescript(_SCHEMA)
                        await cached.commit()
                        _conn_cache[self._db_path] = cached
            return cached
        return await _get_connection(self._db_path)

    # ---- conversation history ----

    async def get_items(self, limit: int | None = None) -> list[Message]:
        conn = await self._conn()
        if limit is None:
            sql = (
                "SELECT item_json FROM session_items "
                "WHERE session_id = ? ORDER BY rowid"
            )
            cursor = await conn.execute(sql, (self.session_id,))
            try:
                rows = await cursor.fetchall()
            finally:
                await cursor.close()
            return [_deserialize_item(row[0]) for row in rows]
        if limit <= 0:
            return []
        sql = (
            "SELECT item_json FROM session_items "
            "WHERE session_id = ? ORDER BY rowid DESC LIMIT ?"
        )
        cursor = await conn.execute(sql, (self.session_id, limit))
        try:
            rows = await cursor.fetchall()
        finally:
            await cursor.close()
        # rows are newest-first; reverse to chronological for caller convenience.
        return [_deserialize_item(row[0]) for row in list(rows)[::-1]]

    async def add_items(self, items: list[Message]) -> None:
        if not items:
            return
        conn = await self._conn()
        sql = "INSERT INTO session_items (session_id, item_json) VALUES (?, ?)"
        params = [(self.session_id, _serialize_item(item)) for item in items]
        await conn.executemany(sql, params)
        await conn.commit()

    async def pop_item(self) -> Message | None:
        conn = await self._conn()
        sql = (
            "SELECT rowid, item_json FROM session_items "
            "WHERE session_id = ? ORDER BY rowid DESC LIMIT 1"
        )
        cursor = await conn.execute(sql, (self.session_id,))
        try:
            row = await cursor.fetchone()
        finally:
            await cursor.close()
        if row is None:
            return None
        rowid, blob = row
        await conn.execute("DELETE FROM session_items WHERE rowid = ?", (rowid,))
        await conn.commit()
        return _deserialize_item(blob)

    async def clear_session(self) -> None:
        conn = await self._conn()
        await conn.execute(
            "DELETE FROM session_items WHERE session_id = ?",
            (self.session_id,),
        )
        await conn.execute(
            "DELETE FROM session_interrupts WHERE session_id = ?",
            (self.session_id,),
        )
        await conn.commit()

    # ---- pending interrupts ----

    async def pending_interrupts(self) -> list[InterruptRecord]:
        conn = await self._conn()
        sql = (
            "SELECT interrupt_id, kind, payload_json, created_at "
            "FROM session_interrupts WHERE session_id = ? ORDER BY rowid"
        )
        cursor = await conn.execute(sql, (self.session_id,))
        try:
            rows = await cursor.fetchall()
        finally:
            await cursor.close()
        records: list[InterruptRecord] = []
        for interrupt_id, kind, payload_json, created_at_iso in rows:
            records.append(
                InterruptRecord(
                    id=interrupt_id,
                    kind=kind,
                    payload=json.loads(payload_json),
                    created_at=datetime.fromisoformat(created_at_iso),
                )
            )
        return records

    async def push_interrupt(self, record: InterruptRecord) -> None:
        conn = await self._conn()
        sql = (
            "INSERT INTO session_interrupts "
            "(session_id, interrupt_id, kind, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)"
        )
        await conn.execute(
            sql,
            (
                self.session_id,
                record.id,
                record.kind,
                json.dumps(record.payload, ensure_ascii=False),
                record.created_at.isoformat(),
            ),
        )
        await conn.commit()

    async def pop_interrupt(self, interrupt_id: str) -> InterruptRecord | None:
        conn = await self._conn()
        sql = (
            "SELECT kind, payload_json, created_at FROM session_interrupts "
            "WHERE session_id = ? AND interrupt_id = ?"
        )
        cursor = await conn.execute(sql, (self.session_id, interrupt_id))
        try:
            row = await cursor.fetchone()
        finally:
            await cursor.close()
        if row is None:
            return None
        kind, payload_json, created_at_iso = row
        await conn.execute(
            "DELETE FROM session_interrupts "
            "WHERE session_id = ? AND interrupt_id = ?",
            (self.session_id, interrupt_id),
        )
        await conn.commit()
        return InterruptRecord(
            id=interrupt_id,
            kind=kind,
            payload=json.loads(payload_json),
            created_at=datetime.fromisoformat(created_at_iso),
        )
