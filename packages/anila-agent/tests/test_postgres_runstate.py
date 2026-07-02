"""PostgresSession（fake pool）+ runstate 持久化助手。"""

from __future__ import annotations

import json

import pytest

from anila_agent.memory.session import PostgresSession
from anila_agent.runtime import runstate

pytestmark = pytest.mark.unit


# ---- PostgresSession via fake asyncpg pool ----

class _Conn:
    def __init__(self, store):
        self.store = store

    async def execute(self, sql, *args):
        if sql.lstrip().upper().startswith("DELETE"):
            self.store.clear()

    async def executemany(self, sql, rows):
        for _sid, js in rows:
            self.store.append(js)

    async def fetch(self, sql, *args):
        items = self.store[-args[1]:] if "ORDER BY idx DESC LIMIT" in sql else list(self.store)
        return [{"item": js} for js in items]

    async def fetchrow(self, sql, *args):
        if not self.store:
            return None
        return {"item": self.store.pop()}


class _Acquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *a):
        return False


class _Pool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _Acquire(self._conn)


def _session():
    store: list[str] = []
    s = PostgresSession("sid", "postgresql://x")
    s._pool = _Pool(_Conn(store))  # 預設 pool → 跳過 asyncpg 匯入/建表
    return s


async def test_add_then_get_roundtrip():
    s = _session()
    await s.add_items([{"role": "user", "content": "一"}, {"role": "assistant", "content": "二"}])
    items = await s.get_items()
    assert [i["content"] for i in items] == ["一", "二"]


async def test_get_items_limit_returns_last_in_order():
    s = _session()
    await s.add_items([{"role": "user", "content": str(i)} for i in range(5)])
    items = await s.get_items(limit=2)
    assert [i["content"] for i in items] == ["3", "4"]


async def test_pop_and_clear():
    s = _session()
    await s.add_items([{"role": "user", "content": "x"}])
    assert (await s.pop_item())["content"] == "x"
    await s.add_items([{"role": "user", "content": "y"}])
    await s.clear_session()
    assert await s.get_items() == []


def test_add_empty_is_noop():
    # add_items([]) 不應丟例外（提早 return）。
    import asyncio

    asyncio.run(_session().add_items([]))


# ---- runstate ----

def test_schema_version_recorded():
    assert isinstance(runstate.SCHEMA_VERSION, str) and runstate.SCHEMA_VERSION


def test_has_interruptions():
    class _R:
        interruptions = (object(),)

    class _Empty:
        interruptions = ()

    assert runstate.has_interruptions(_R()) is True
    assert runstate.has_interruptions(_Empty()) is False
    assert runstate.has_interruptions(object()) is False


def test_dump_state_delegates():
    class _State:
        def to_string(self):
            return '{"ok": 1}'

    assert json.loads(runstate.dump_state(_State())) == {"ok": 1}
