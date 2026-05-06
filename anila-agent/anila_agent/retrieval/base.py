"""Retriever interface. Coworkers fill this in for their corpus.

Two methods only — search and fetch. Anything richer (reranking, hybrid search,
filters) lives in the implementation, not in the protocol, so the agent code
stays the same regardless of backend.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from anila_agent.models.schemas import Document

__all__ = ["Document", "Retriever"]


@runtime_checkable
class Retriever(Protocol):
    """Implement this and pass an instance to `RAGToolset(retriever)`.

    Contract:
        search(query, k) → list of Documents ordered by descending relevance.
                           At most k results. May return fewer; never None.
        fetch(doc_id)    → the full Document for an ID returned by search,
                           or None if it does not exist.

    Async/sync: implementations may be async; the tool wrappers await everything.
    """

    async def search(self, query: str, k: int = 5) -> list[Document]: ...

    async def fetch(self, doc_id: str) -> Document | None: ...

    @property
    def name(self) -> str: ...

    @property
    def metadata(self) -> dict[str, Any]:  # pragma: no cover - simple default
        return {}
