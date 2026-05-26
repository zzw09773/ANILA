"""DbChangeTrigger — 定期 poll query function,result 變動時觸發 callback。

跟 :class:`PeriodicTrigger` 的差別:periodic 每 N 秒一定 fire,db_change 只在
``query_func()`` 回傳值真的「跟上次不同」時才 fire。對應 ANILA 平台 use case:

* **Studio job 完成自動續推**:
  ``DbChangeTrigger(query=fetch_job_status, callback=on_completion)`` —
  每 5 秒 poll ``studio_jobs.status``,from "running" → "done" 時 fire,
  callback 內 ``ctx.session.queue_message("[studio] job <id> done")``
  把訊息注入 agent 讓它接下個動作。
* **KB 變動 reindex notification**:
  ``DbChangeTrigger(query=count_kb_docs, callback=notify_reindex)`` —
  poll KB doc table count,有新增就 notify。

設計上 ``query_func`` 是任意 async callable,**回傳值必須 hashable** 才能用
``==`` 比對 (str / int / tuple / frozenset 等皆可)。callable 內部例外會被
吞掉並 log warn,讓 trigger 自身的觀測 loop 不會被 DB 暫斷拖死。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from anila_agent.triggers.base import Trigger

if TYPE_CHECKING:
    from anila_agent.core.hook_context import HookContext
    from anila_agent.triggers.base import TriggerManager

logger = logging.getLogger(__name__)

# query_func 簽章:async () -> any hashable。
DbQueryFunc = Callable[[], Awaitable[Any]]

# callback 簽章:async (ctx, previous, current) -> None。
DbChangeCallback = Callable[["HookContext", Any, Any], Awaitable[None]]

# sentinel:第一次 poll 之前無前值,跟 None 區分 (None 也是合法 query 結果)。
_UNSET = object()


class DbChangeTrigger(Trigger):
    """定期跑 query_func,結果跟上次比若不同就 fire callback。

    Attributes:
        query_func: async () -> any hashable,任何 IO 邏輯都行 (DB query /
            HTTP fetch / file read)。回傳值會被 ``==`` 比對。
        callback: async (ctx, previous, current) -> None。第一次 fire 時
            ``previous`` 為 None (而非 sentinel,讓 user 不必額外 import)。
        interval_seconds: poll 間隔,預設 5 秒。
        fire_on_initial: 第一次拿到 query 結果是否 fire callback,預設
            ``False`` (跟 antigravity ``every`` 一致,啟動後先觀測一次當 baseline)。
        custom_name: 可選 trigger 名。
    """

    def __init__(
        self,
        query_func: DbQueryFunc,
        callback: DbChangeCallback,
        *,
        interval_seconds: float = 5.0,
        fire_on_initial: bool = False,
        name: str | None = None,
    ) -> None:
        """驗證 interval > 0 並儲存欄位。

        Args:
            query_func: async () -> any hashable;每 interval 跑一次。
            callback: async (ctx, previous, current) -> None。
            interval_seconds: poll 間隔,> 0。
            fire_on_initial: 第一次 query 即 fire 一次 (previous=None)。
            name: 自訂 trigger 名;未給用 ``"db_change_<interval>s"``。

        Raises:
            ValueError: interval_seconds <= 0。
        """
        if interval_seconds <= 0:
            raise ValueError(
                f"interval_seconds must be positive, got {interval_seconds!r}"
            )
        self.query_func = query_func
        self.callback = callback
        self.interval_seconds = interval_seconds
        self.fire_on_initial = fire_on_initial
        self._name = name or f"db_change_{interval_seconds}s"

    @property
    def name(self) -> str:
        """trigger 名稱,例如 ``"db_change_5.0s"``。"""
        return self._name

    async def run(self, manager: TriggerManager) -> None:
        """long-lived loop:``sleep -> query -> compare -> (if changed) fire``。

        第一次跑 ``query_func`` 不 fire (預設) — 以該值為 baseline,
        後續跟它比。``query_func`` 例外被 catch + log warn,當次 cycle
        略過 (保留上次 baseline)。
        """
        previous: Any = _UNSET
        while True:
            await asyncio.sleep(self.interval_seconds)
            try:
                current = await self.query_func()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "DbChangeTrigger '%s' query_func raised; skipping this cycle",
                    self._name,
                    exc_info=True,
                )
                continue

            if previous is _UNSET:
                # baseline:第一次拿到 — 視 fire_on_initial 決定是否 fire。
                if self.fire_on_initial:
                    await self.fire(manager, None, current)
                previous = current
                continue

            if current != previous:
                await self.fire(manager, previous, current)
                previous = current


__all__ = ["DbChangeCallback", "DbChangeTrigger", "DbQueryFunc"]
