"""RunHooks：稽核檢索呼叫 + per-run token 計量。掛在 ``Runner.run(hooks=...)``。

原生 spans/tracing 預設停用（air-gap，見 runtime.model），故以 RunHooks + Usage
做本地稽核/計量，不外連。
"""

from __future__ import annotations

import logging
from typing import Any

from agents import RunHooks

from anila_agent.observability.cost import estimate_cost

logger = logging.getLogger("anila.audit")


class AuditHooks(RunHooks):
    """記錄工具呼叫與每輪 token 用量（可附 user_id 做 per-user 計量）。"""

    def __init__(self, *, user_id: str | None = None) -> None:
        self._user_id = user_id

    async def on_tool_start(self, context: Any, agent: Any, tool: Any) -> None:
        logger.info("tool_start user=%s agent=%s tool=%s", self._user_id, agent.name, tool.name)

    async def on_tool_end(self, context: Any, agent: Any, tool: Any, result: Any) -> None:
        logger.info("tool_end user=%s tool=%s", self._user_id, tool.name)

    async def on_agent_end(self, context: Any, agent: Any, output: Any) -> None:
        usage = getattr(context, "usage", None)
        if usage is None:
            return
        model = getattr(agent.model, "model", str(agent.model))
        est = estimate_cost(model, usage)
        logger.info("agent_end user=%s usage=%s", self._user_id, est.render())
