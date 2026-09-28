"""Two workers must not both ALTER the same missing column."""

from __future__ import annotations

import asyncio
import sqlite3

import aiosqlite

from anila_core.memory.short_term.sqlite import _ensure_schema_migrations


def test_two_connections_add_owner_key_hash_once(tmp_path):
    path = tmp_path / "sessions.db"
    raw = sqlite3.connect(path)
    raw.execute(
        """
        CREATE TABLE session_owners (
            session_id TEXT PRIMARY KEY,
            agent_id TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    raw.commit()
    raw.close()

    async def migrate():
        db = await aiosqlite.connect(path)
        await db.execute("PRAGMA busy_timeout=5000")
        await _ensure_schema_migrations(db)
        await db.close()

    async def both():
        await asyncio.gather(migrate(), migrate())

    asyncio.run(both())
    seen = [
        row[1]
        for row in sqlite3.connect(path).execute("PRAGMA table_info(session_owners)")
    ]
    assert seen.count("owner_key_hash") == 1
