"""Triggers 子系統 base 抽象 — `Trigger` ABC + `TriggerManager` 生命週期管理。

對應 enhancement roadmap **P1-4**:讓 anila-agent 在 session active 期間可以
被「外部事件」喚醒,從同步 request/response 進化到 always-on 自動化模式。

設計核心:

* **Trigger** 是一個 long-lived async loop,session start 時 spawn 成
  ``asyncio.Task``,session end 時 cancel。每個 trigger 自己管自己的 loop
  (poll / sleep / watch),由 :meth:`TriggerManager._run` 統一包 cancel +
  例外處理。
* **TriggerManager** 集中管理一組 trigger 的 start/stop,並對每次 trigger
  fire 開 P0-9 tracing span (`trigger.fire.<name>`),trigger 例外 catch
  + log warn 不會 break 其他 trigger。

跟 P1-3 HookContext 整合:`TriggerManager.__init__` 接受
``session: SessionContext``,trigger callback 收到 ``HookContext`` 後可以
``ctx.session.queue_message(...)`` 把訊息注入 agent message queue (見
`anila_agent.core.hook_context.SessionContext.queue_message`)。

跟上游 antigravity 的差異:

* 上游用「`Trigger` = async callable」純 functional 風格 + `TriggerRunner`
  分開;這裡用「`Trigger` ABC + 子類別」,每個 trigger 自帶 ``name`` /
  ``fire_callback`` 兩個責任分離的步驟,方便 manager 在 callback 外面加
  tracing / 例外吞掉,而不需要包整顆 loop。
* 不引 ``watchfiles`` dep — :class:`FileChangeTrigger` 用 std-lib
  ``pathlib`` + 輪詢 mtime 實作,效能可接受 (sub-agent 用途不會監數萬
  個檔案)。
"""

from __future__ import annotations

import abc
import asyncio
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from anila_agent.core.hook_context import HookContext, SessionContext
    from anila_agent.tracing import Tracer

logger = logging.getLogger(__name__)


class Trigger(abc.ABC):
    """所有 trigger 的抽象 base。

    子類別只需要實作兩件事:

    1. ``name`` (property) — 給 tracing span / log 用的識別字串。
    2. ``run(manager)`` (coroutine) — long-lived loop,內部自己決定何時
       呼叫 ``await self._fire(manager)`` 觸發一次 callback。

    為什麼把 ``run`` 跟 ``_fire`` 分開?讓「trigger 自己的觀測迴圈」(可能
    sleep / poll / await IO) 跟「callback 跑一次」徹底解耦 —— ``_fire``
    自帶 tracing span 與例外吞掉,子類別不必每次自己包 try/except。

    Attributes:
        callback: 真正要跑的 user 邏輯,async callable,sig 為
            ``(ctx: HookContext, *extra) -> Awaitable[None]``。extra 由
            子類別自行定義 (例如 ``FileChangeTrigger`` 會多傳 changed
            paths)。
    """

    callback: Any  # 子類別可換更精細型別 — base 用 Any 兼容。

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """trigger 名稱;用於 tracing span (`trigger.fire.<name>`) 與 log。"""

    @abc.abstractmethod
    async def run(self, manager: TriggerManager) -> None:
        """trigger 的 long-lived loop。

        Args:
            manager: 註冊本 trigger 的 :class:`TriggerManager`,可透過
                ``manager.session`` 拿到 session-level state,或透過
                ``manager.fire(self, *args)`` 觸發一次 callback。
        """

    async def fire(
        self,
        manager: TriggerManager,
        *callback_args: Any,
    ) -> None:
        """觸發一次 callback (給子類別在 ``run`` 內呼叫的 helper)。

        本 method 統一處理:

        1. 開 P0-9 tracing span (`trigger.fire.<name>`)。
        2. 把 ``HookContext.from_session(manager.session)`` 傳給 callback。
        3. catch callback 內任何 Exception (除 CancelledError) 後 log warn,
           讓本 trigger 的 loop 與其他 trigger 都不受影響。

        Args:
            manager: 觸發來源的 TriggerManager。
            callback_args: 額外傳給 callback 的位置參數;由子類別決定
                語義 (e.g. ``FileChangeTrigger`` 會傳 ``changed_paths``)。
        """
        await manager._fire_trigger(self, callback_args)


class TriggerManager:
    """管理一組 :class:`Trigger` 的生命週期 — start_all / stop_all。

    與 P1-3 SessionContext 整合:傳入 ``session``,trigger callback 收到的
    :class:`HookContext` 就是從這個 session 派生出來的,可以用
    ``ctx.session.queue_message(...)`` 注入 message。

    與 P0-9 tracing 整合:每次 trigger fire 都會開
    `trigger.fire.<trigger.name>` span (透過 ``tracer.start_span``),
    若 ``tracer`` 為 None 則直接跑 callback (用於單元測試或不需 tracing
    的場景)。

    典型使用流程:

    ```python
    session = SessionContext(session_id="...")
    manager = TriggerManager(session=session, tracer=tracer)
    manager.register(PeriodicTrigger(interval_seconds=60, callback=heartbeat))
    manager.register(FileChangeTrigger(paths=[...], callback=on_change))
    await manager.start_all()
    try:
        ...  # agent 跑 turn 期間 trigger 在背景 fire
    finally:
        await manager.stop_all()
    ```

    Attributes:
        session: P1-3 SessionContext,trigger callback 用它注入 message。
        tracer: 可選 P0-9 Tracer;若給定則每次 fire 開 span。
    """

    def __init__(
        self,
        *,
        session: SessionContext,
        tracer: Tracer | None = None,
    ) -> None:
        """初始化空 trigger list。

        Args:
            session: P1-3 SessionContext,trigger 跨多 turn 共用。
            tracer: 可選 P0-9 Tracer;若給定則每次 trigger fire 開 span。
        """
        self.session = session
        self.tracer = tracer
        self._triggers: list[Trigger] = []
        self._tasks: list[asyncio.Task[None]] = []

    # ---- 註冊 ----------------------------------------------------------

    def register(self, trigger: Trigger) -> None:
        """新增一個 trigger;必須在 :meth:`start_all` 前呼叫。

        Args:
            trigger: 要加入管理的 Trigger 實例。

        Raises:
            RuntimeError: 已 ``start_all`` 之後再 register 視為錯誤。
        """
        if self._tasks:
            raise RuntimeError(
                "Cannot register triggers after start_all(); "
                "call stop_all() first or register before starting."
            )
        self._triggers.append(trigger)

    @property
    def triggers(self) -> tuple[Trigger, ...]:
        """以 immutable tuple 回傳已註冊的 trigger (避免外部 mutate)。"""
        return tuple(self._triggers)

    @property
    def is_running(self) -> bool:
        """是否有任何 trigger task 還在跑。"""
        return any(not task.done() for task in self._tasks)

    # ---- 生命週期 ------------------------------------------------------

    async def start_all(self) -> None:
        """把所有已註冊 trigger 各自 spawn 成 ``asyncio.Task``。

        每個 trigger 跑在自己的 task 內,task name 為
        ``"trigger:<trigger.name>"`` 方便 debugger 識別。task 之間獨立,
        某一個 trigger 例外不會影響其他 trigger。

        Raises:
            RuntimeError: 已經 start 過但未 stop。
        """
        if self._tasks:
            raise RuntimeError("TriggerManager already started; call stop_all() first.")
        for trigger in self._triggers:
            task = asyncio.create_task(
                self._supervise(trigger),
                name=f"trigger:{trigger.name}",
            )
            self._tasks.append(task)

    async def stop_all(self) -> None:
        """cancel 所有 trigger task 並等候它們收尾。

        重複呼叫安全;stop 完可以再 :meth:`start_all`。
        """
        if not self._tasks:
            return
        for task in self._tasks:
            if not task.done():
                task.cancel()
        # gather 用 return_exceptions=True 把 CancelledError 吃掉,避免
        # asyncio 報 "Task was destroyed but it is pending"。
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def __aenter__(self) -> TriggerManager:
        """以 async context manager 使用:進場 start_all。"""
        await self.start_all()
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        """離場 stop_all (任何例外都先確保 trigger 收乾淨)。"""
        await self.stop_all()

    # ---- 內部 ----------------------------------------------------------

    async def _supervise(self, trigger: Trigger) -> None:
        """包裝 ``trigger.run`` 加 cancel 容忍 + 例外不影響 manager。

        CancelledError 一定要 re-raise (asyncio 規約),其他 Exception
        catch + log warn。
        """
        try:
            await trigger.run(self)
        except asyncio.CancelledError:
            logger.debug("trigger '%s' cancelled", trigger.name)
            raise
        except Exception:
            logger.warning(
                "trigger '%s' loop raised unhandled exception; stopping this trigger only",
                trigger.name,
                exc_info=True,
            )

    async def _fire_trigger(
        self,
        trigger: Trigger,
        callback_args: tuple[Any, ...],
    ) -> None:
        """執行一次 trigger callback,自動開 tracing span + 吞例外。

        Args:
            trigger: 觸發的 trigger 實例 (用來取 ``name``)。
            callback_args: 額外位置參數,append 在 ``ctx`` 後面傳給 callback。
        """
        # 延後 import 避免 hook_context ↔ triggers 循環。
        from anila_agent.core.hook_context import HookContext

        ctx = HookContext.from_session(self.session)
        span_name = f"trigger.fire.{trigger.name}"

        if self.tracer is None:
            await self._invoke_callback(trigger, ctx, callback_args)
            return

        with self.tracer.start_span(span_name, attributes={"trigger.name": trigger.name}):
            await self._invoke_callback(trigger, ctx, callback_args)

    async def _invoke_callback(
        self,
        trigger: Trigger,
        ctx: HookContext,
        callback_args: tuple[Any, ...],
    ) -> None:
        """跑 callback 並吞例外。CancelledError 例外照樣 re-raise。"""
        try:
            await trigger.callback(ctx, *callback_args)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "trigger '%s' callback raised; suppressed to keep manager alive",
                trigger.name,
                exc_info=True,
            )


__all__ = ["Trigger", "TriggerManager"]
