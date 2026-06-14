"""每次 run 的隱藏相依（retriever、未來的 user/collection/token）。

透過 SDK 的 run-context（``RunContextWrapper``）注入工具，**不進入模型看得到的工具 schema**——
模型只看得到 query/k，看不到 retriever 或認證憑證。這是對全域 immutability 原則「刻意、
有範圍」的例外：run-context 本就設計為跨工具共享的可變請求狀態。

P0 僅含 retriever；user/collection/token 於 P1（平台契約）加入。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from anila_agent.retrieval.base import Retriever

if TYPE_CHECKING:
    from anila_agent.memory.runtime import MemdirRuntime


@dataclass
class AnilaRunContext:
    """單次 agent run 的請求範圍相依。"""

    retriever: Retriever
    memory: MemdirRuntime | None = None
