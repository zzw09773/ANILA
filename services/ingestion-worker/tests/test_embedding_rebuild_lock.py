"""The rebuild lock is not taken on the transaction-pooled connection."""

from __future__ import annotations

import pytest

from ingestion_worker.embedding_rebuild import direct_lock_dsn, rebuild_embeddings
from ingestion_worker import embedding_rebuild as rebuild_mod


def test_pooler_host_is_rewritten_to_the_database():
    assert (
        direct_lock_dsn("postgresql://csp_app:s3cret@pgbouncer:5432/csp")
        == "postgresql://csp_app:s3cret@csp-db:5432/csp"
    )
    same = "postgresql://csp_app:s3cret@csp-db:5432/csp"
    assert direct_lock_dsn(same) == same


@pytest.mark.asyncio
async def test_lock_stays_on_the_direct_connection(monkeypatch):
    events: list[tuple[str, str]] = []

    class Conn:
        def __init__(self, name: str) -> None:
            self.name = name

        async def fetchval(self, sql, *args):
            events.append((self.name, sql))
            return True

        async def execute(self, sql, *args):
            events.append((self.name, sql))

        async def close(self):
            events.append((self.name, "close"))

    class Pool:
        def acquire(self):
            return self

        async def __aenter__(self):
            return Conn("pool")

        async def __aexit__(self, *exc):
            return False

    async def connect(dsn):
        events.append(("connect", dsn))
        return Conn("direct")

    async def _pass(ctx, conn, started):
        events.append(("pass", conn.name))
        return {"status": "idle"}

    monkeypatch.setattr(rebuild_mod.asyncpg, "connect", connect)
    monkeypatch.setattr(
        rebuild_mod.settings,
        "database_url",
        "postgresql://u:p@pgbouncer:5432/csp",
    )
    monkeypatch.setattr(rebuild_mod, "_pass", _pass)

    result = await rebuild_embeddings({"pool": Pool()})
    assert result == {"status": "idle"}
    assert "csp-db" in events[0][1]
    assert ("direct", "SELECT pg_try_advisory_lock($1)") in events
    assert ("pass", "pool") in events
    assert ("direct", "SELECT pg_advisory_unlock($1)") in events
    assert not any(name == "pool" and "advisory" in sql for name, sql in events)
