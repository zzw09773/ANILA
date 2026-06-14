"""事件觸發（對應 Antigravity 的 triggers）。

註冊 ``(event, action, when?)``，``dispatch(event, payload)`` 時跑所有匹配的 action。
純 in-process、無排程器。常見事件：``turn_end``（端末自動抽取記憶）、``tool_error`` 等。
單一 action 失敗不影響其他（隔離），以免一個壞 trigger 拖垮整輪。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("anila.triggers")

Action = Callable[[Any], Awaitable[None]]
Predicate = Callable[[Any], bool]


@dataclass
class Trigger:
    name: str
    event: str
    action: Action
    when: Predicate | None = None

    def applies(self, event: str, payload: Any) -> bool:
        return self.event == event and (self.when is None or bool(self.when(payload)))


class TriggerRunner:
    """註冊與派發 trigger。"""

    def __init__(self) -> None:
        self._triggers: list[Trigger] = []

    def register(self, trigger: Trigger) -> None:
        self._triggers.append(trigger)

    async def dispatch(self, event: str, payload: Any = None) -> int:
        """跑所有匹配此事件的 action；回傳成功執行的數量。失敗隔離。"""
        ran = 0
        for trigger in self._triggers:
            if not trigger.applies(event, payload):
                continue
            try:
                await trigger.action(payload)
                ran += 1
            except Exception:
                logger.exception("trigger %s 在事件 %s 失敗", trigger.name, event)
        return ran
