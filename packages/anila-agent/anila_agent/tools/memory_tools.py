"""長期記憶（memdir）工具：讓 agent 檢索使用者偏好、專案脈絡、過往指引。

read-only。recall 走混合管線（embed 粗篩 + LLM 精選，fail-closed）。未啟用 memdir
時回 []。新鮮度標記提醒過時記憶需先驗證。
"""

from __future__ import annotations

import time

from agents import RunContextWrapper, function_tool

from anila_agent.memory import freshness
from anila_agent.tools.context import AnilaRunContext

K_MIN = 1
K_MAX = 10


@function_tool
async def search_memory(
    ctx: RunContextWrapper[AnilaRunContext], query: str, k: int = 5
) -> list[dict]:
    """檢索長期記憶（使用者偏好、進行中專案、過往工作指引）。

    Args:
        query: 想喚起的記憶主題。
        k: 回傳筆數（夾在 1 到 10）。
    """
    mem = ctx.context.memory
    if mem is None:
        return []
    kk = max(K_MIN, min(int(k), K_MAX))
    now = time.time()
    out: list[dict] = []
    for m in await mem.recall_bodies(query, k=kk):
        path = mem.store._path(m.name)
        mtime = path.stat().st_mtime if path.is_file() else now
        out.append(
            {
                "name": m.name,
                "type": m.type,
                "body": m.body,
                "freshness": freshness.tag(m.name, mtime, now),
            }
        )
    return out
