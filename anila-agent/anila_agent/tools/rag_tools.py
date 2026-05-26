"""Built-in RAG tools wired to the configured Retriever.

The retriever is set once at startup via `set_retriever()`. The tools themselves
are module-level FunctionTool instances so they can be referenced by qualified
name in `configs/tools.yaml`.
"""

from __future__ import annotations

from typing import Any

from anila_agent.retrieval.base import Retriever
from anila_agent.retrieval.dummy import DummyRetriever
from anila_agent.tools.base import anila_tool

_retriever: Retriever = DummyRetriever()


def set_retriever(retriever: Retriever) -> None:
    """Install the active retriever. Call before invoking the agent."""
    global _retriever
    if not isinstance(retriever, Retriever):
        raise TypeError(
            f"retriever must implement the Retriever protocol, got {type(retriever).__name__}"
        )
    _retriever = retriever


def get_retriever() -> Retriever:
    return _retriever


@anila_tool(
    is_read_only=True,
    category="retrieval",
    cost_estimate="low",
)
async def search_documents(query: str, k: int = 5) -> list[dict[str, Any]]:
    """檢索已設定的語料庫。

    Args:
        query: 自然語言查詢字串。
        k: 最大回傳數(1–20),預設 5。

    Returns:
        依相關度遞減排序的 {id, text, score, metadata} dict list。
    """
    bounded_k = max(1, min(int(k), 20))
    docs = await _retriever.search(query, bounded_k)
    return [
        {
            "id": doc.id,
            "text": doc.text,
            "score": doc.score,
            "metadata": doc.metadata,
        }
        for doc in docs
    ]


@anila_tool(
    is_read_only=True,
    category="retrieval",
    cost_estimate="free",
)
async def read_document(doc_id: str) -> dict[str, Any] | None:
    """以 ID 取得單篇文件的完整內容。

    通常在 `search_documents` 之後使用,取得未截斷的全文。
    ID 不存在時回傳 None。
    """
    doc = await _retriever.fetch(doc_id)
    if doc is None:
        return None
    return {"id": doc.id, "text": doc.text, "metadata": doc.metadata}
