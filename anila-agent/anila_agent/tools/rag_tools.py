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


@anila_tool(is_read_only=True, category="retrieval")
async def search_documents(query: str, k: int = 5) -> list[dict[str, Any]]:
    """Search the configured corpus.

    Args:
        query: Natural-language query.
        k: Maximum number of results (1–20). Defaults to 5.

    Returns:
        A list of {id, text, score, metadata} dicts ordered by descending relevance.
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


@anila_tool(is_read_only=True, category="retrieval")
async def read_document(doc_id: str) -> dict[str, Any] | None:
    """Fetch the full text of a document by ID.

    Use this after `search_documents` to get the unabridged content.
    Returns None when the ID does not exist.
    """
    doc = await _retriever.fetch(doc_id)
    if doc is None:
        return None
    return {"id": doc.id, "text": doc.text, "metadata": doc.metadata}
