"""PeriodicTrigger — 固定 interval 觸發 callback 的 trigger。

對應上游 antigravity 的 ``every(interval_seconds, callback)`` helper,但改為
class-based 以便 :class:`TriggerManager` 統一 tracing/lifecycle。

Use case 範例 (寫在 docstring,不在這裡跑 demo):

* **健康檢查**: ``PeriodicTrigger(60, check_vllm_health)`` — 每 60 秒探一次
  vLLM 服務狀態,異常時用 ``ctx.session.queue_message("...服務異常...")``
  把警示注入 agent,讓 agent 下一輪 turn 開始時看到。
* **定時提醒**: ``PeriodicTrigger(3600, daily_summary)`` — 每小時推一次摘要
  request 給 agent。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from anila_agent.triggers.base import Trigger

if TYPE_CHECKING:
    from anila_agent.core.hook_context import HookContext
    from anila_agent.triggers.base import TriggerManager

# callback 簽章:async (ctx) -> None。
PeriodicCallback = Callable[["HookContext"], Awaitable[None]]


class PeriodicTrigger(Trigger):
    """每 ``interval_seconds`` 秒呼叫 ``callback`` 一次的 trigger。

    第一次 fire 在 *第一個 interval 結束之後* (跟上游 antigravity ``every``
    一致),避免 trigger 一啟動就立刻 fire 而搶在 agent 還沒準備好之前。

    Example:
        ```python
        async def heartbeat(ctx):
            ctx.session.queue_message("[health-check] 1 minute tick")

        manager.register(PeriodicTrigger(60.0, heartbeat))
        ```

    Attributes:
        interval_seconds: 兩次 fire 之間的秒數,必須為正。
        callback: async callable,sig 為 ``async (ctx: HookContext) -> None``。
        custom_name: 可選 trigger 名稱;未給就用 ``"periodic_{interval}s"``。
    """

    def __init__(
        self,
        interval_seconds: float,
        callback: PeriodicCallback,
        *,
        name: str | None = None,
    ) -> None:
        """驗證 interval > 0 並儲存欄位。

        Args:
            interval_seconds: 兩次 fire 之間秒數,> 0 才合法。
            callback: async (ctx) -> None。
            name: 自訂 trigger 名;未給用 ``periodic_<interval>s``。

        Raises:
            ValueError: interval_seconds <= 0。
        """
        if interval_seconds <= 0:
            raise ValueError(
                f"interval_seconds must be positive, got {interval_seconds!r}"
            )
        self.interval_seconds = interval_seconds
        self.callback = callback
        self._name = name or f"periodic_{interval_seconds}s"

    @property
    def name(self) -> str:
        """trigger 名稱,例如 ``"periodic_60.0s"``。"""
        return self._name

    async def run(self, manager: TriggerManager) -> None:
        """long-lived loop:``sleep -> fire`` 反覆。

        sleep 後才 fire 而不是 fire 後才 sleep,跟上游 antigravity 行為一致。
        被 cancel 時 ``asyncio.sleep`` 會丟 CancelledError,base.supervise
        會把它 re-raise 讓 task 正常收尾。
        """
        while True:
            await asyncio.sleep(self.interval_seconds)
            await self.fire(manager)


__all__ = ["PeriodicCallback", "PeriodicTrigger"]
