"""短期對話記憶（單一對話、append-only）。

- 預設 = SDK 原生 ``SQLiteSession``（本地檔、零相依、clone-and-run）。
- 平台選用 = ``PostgresSession``（自實作 4-method protocol，存平台 Postgres）。
  不用 OpenAI-managed session（會打 OpenAI）。
- ``SummarizingSession`` = 非-OpenAI 的 context 壓縮包裝（原生 compaction 綁
  OpenAI Responses，Gemma/gpt-oss 不通）。

注意：短期 Session 與長期 memdir（P3）是兩層，勿混為一談。
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from agents import SQLiteSession

if TYPE_CHECKING:
    from agents import Session

    from anila_agent.config import AppConfig

Summarizer = Callable[[list[dict]], Awaitable[str]]


def build_session(cfg: AppConfig, session_id: str) -> Session:
    """依設定建短期 session。預設 SQLite（ANILA_HOME/sessions.db）。"""
    backend = os.getenv("ANILA_SESSION_BACKEND", "sqlite").lower()
    if backend == "postgres":
        dsn = os.environ.get("ANILA_SESSION_DSN")
        if not dsn:
            raise ValueError("ANILA_SESSION_BACKEND=postgres 需設 ANILA_SESSION_DSN")
        return PostgresSession(session_id, dsn)
    cfg.home.mkdir(parents=True, exist_ok=True)
    return SQLiteSession(session_id, db_path=str(cfg.home / "sessions.db"))


class PostgresSession:
    """SDK Session protocol 的 Postgres 實作（asyncpg）。

    表：``anila_agent_messages(session_id text, idx bigserial, item jsonb)``。
    asyncpg 延遲匯入，首次使用時建表。
    """

    def __init__(self, session_id: str, dsn: str, *, table: str = "anila_agent_messages") -> None:
        self._session_id = session_id
        self._dsn = dsn
        self._table = table
        self._pool: Any = None

    async def _ensure(self) -> Any:
        if self._pool is None:
            import asyncpg

            self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=4)
            async with self._pool.acquire() as conn:
                await conn.execute(
                    f"CREATE TABLE IF NOT EXISTS {self._table} "
                    "(session_id text NOT NULL, idx bigserial PRIMARY KEY, item jsonb NOT NULL)"
                )
        return self._pool

    async def get_items(self, limit: int | None = None) -> list[dict]:
        import json

        pool = await self._ensure()
        async with pool.acquire() as conn:
            if limit is None:
                rows = await conn.fetch(
                    f"SELECT item FROM {self._table} WHERE session_id=$1 ORDER BY idx",
                    self._session_id,
                )
            else:
                # 取最後 limit 筆，再還原成時間遞增序。
                rows = await conn.fetch(
                    f"SELECT item FROM (SELECT item, idx FROM {self._table} "
                    "WHERE session_id=$1 ORDER BY idx DESC LIMIT $2) s ORDER BY idx",
                    self._session_id,
                    limit,
                )
        return [json.loads(r["item"]) if isinstance(r["item"], str) else r["item"] for r in rows]

    async def add_items(self, items: list[dict]) -> None:
        import json

        if not items:
            return
        pool = await self._ensure()
        async with pool.acquire() as conn:
            await conn.executemany(
                f"INSERT INTO {self._table} (session_id, item) VALUES ($1, $2)",
                [(self._session_id, json.dumps(it)) for it in items],
            )

    async def pop_item(self) -> dict | None:
        import json

        pool = await self._ensure()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"DELETE FROM {self._table} WHERE idx = "
                f"(SELECT idx FROM {self._table} WHERE session_id=$1 ORDER BY idx DESC LIMIT 1) "
                "RETURNING item",
                self._session_id,
            )
        if row is None:
            return None
        item = row["item"]
        return json.loads(item) if isinstance(item, str) else item

    async def clear_session(self) -> None:
        pool = await self._ensure()
        async with pool.acquire() as conn:
            await conn.execute(f"DELETE FROM {self._table} WHERE session_id=$1", self._session_id)


class SummarizingSession:
    """包裝任一 Session，超過上限時把較舊訊息壓縮成一則摘要（非-OpenAI）。

    壓縮在 ``add_items`` 後觸發：保留最後 ``keep_last`` 筆，其餘交給注入的
    ``summarizer`` 濃縮成單一 system 訊息。``summarizer`` 失敗時保守地略過壓縮
    （寧可不壓也不弄丟歷史）。
    """

    def __init__(
        self,
        inner: Session,
        summarizer: Summarizer,
        *,
        max_items: int = 40,
        keep_last: int = 20,
    ) -> None:
        # keep_last 須 >=1：keep_last=0 會踩 Python -0 切片陷阱（items[:-0] 為空、items[-0:] 為全部）。
        if not isinstance(keep_last, int) or keep_last < 1 or keep_last >= max_items:
            raise ValueError("keep_last 必須是 1 到 max_items-1 之間的整數")
        self._inner = inner
        self._summarizer = summarizer
        self._max_items = max_items
        self._keep_last = keep_last

    async def get_items(self, limit: int | None = None) -> list[dict]:
        return await self._inner.get_items(limit)

    async def add_items(self, items: list[dict]) -> None:
        await self._inner.add_items(items)
        await self._maybe_compact()

    async def pop_item(self) -> dict | None:
        return await self._inner.pop_item()

    async def clear_session(self) -> None:
        await self._inner.clear_session()

    async def _maybe_compact(self) -> None:
        items = await self._inner.get_items()
        if len(items) <= self._max_items:
            return
        head = items[: -self._keep_last]
        tail = items[-self._keep_last :]
        try:
            summary = await self._summarizer(head)
        except Exception:
            return
        await self._inner.clear_session()
        await self._inner.add_items(
            [{"role": "system", "content": f"[先前對話摘要]\n{summary}"}, *tail]
        )
