"""暴露給 agent 的 RAG 工具（read-only）。

工具只接受 query/k；retriever 與認證從 ``AnilaRunContext`` 注入，模型看不到。
``search_documents`` 的 k 夾在 1..20（平台契約），避免單次拉爆 context。
"""

from __future__ import annotations

from agents import RunContextWrapper, function_tool

from anila_agent.tools.context import AnilaRunContext

K_MIN = 1
K_MAX = 20


def _clamp_k(k: int) -> int:
    return max(K_MIN, min(int(k), K_MAX))


@function_tool
async def search_documents(
    ctx: RunContextWrapper[AnilaRunContext], query: str, k: int = 5
) -> list[dict[str, object]]:
    """以語意檢索知識庫，回傳最相關的 k 筆片段（依相關度遞減）。

    Args:
        query: 查詢字串。
        k: 回傳筆數，會夾在 1 到 20 之間。
    """
    hits = await ctx.context.retriever.search(query, k=_clamp_k(k))
    return [
        {"id": d.id, "text": d.text, "score": d.score, "metadata": d.metadata}
        for d in hits
    ]


@function_tool
async def read_document(
    ctx: RunContextWrapper[AnilaRunContext], doc_id: str
) -> dict[str, object] | None:
    """以 id 取單一文件片段全文；找不到回 null。"""
    doc = await ctx.context.retriever.fetch(doc_id)
    if doc is None:
        return None
    return {"id": doc.id, "text": doc.text, "metadata": doc.metadata}


# P0 預設工具集（皆 read-only）。能力表（read_only/write/admin）於 P1 由 configs/tools.yaml 提供。
DEFAULT_TOOLS = [search_documents, read_document]
