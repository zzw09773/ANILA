"""Router workers share one session file. WAL plus a busy timeout lets them."""

import pytest

from anila_core.memory.short_term.sqlite import SqliteSession, close_all_connections


@pytest.mark.asyncio
async def test_file_session_uses_wal_and_busy_timeout(tmp_path):
    path = tmp_path / "sessions.db"
    session = SqliteSession(path, "sid")
    conn = await session._conn()
    try:
        mode = await (await conn.execute("PRAGMA journal_mode")).fetchone()
        timeout = await (await conn.execute("PRAGMA busy_timeout")).fetchone()
        assert str(mode[0]).lower() == "wal"
        assert int(timeout[0]) >= 5000
    finally:
        await close_all_connections()
