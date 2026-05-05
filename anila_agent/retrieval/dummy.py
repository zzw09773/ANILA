"""In-memory starter retriever. Replace this with your real backend.

Scoring is naive: case-insensitive token-overlap count. The point is to make
the template runnable end-to-end without a vector store.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from anila_agent.models.schemas import Document
from anila_agent.retrieval.base import Retriever

_TOKEN_RE = re.compile(r"\w+", flags=re.UNICODE)


def _tokens(text: str) -> set[str]:
    return {m.group(0).lower() for m in _TOKEN_RE.finditer(text)}


class DummyRetriever(Retriever):
    """A list of documents, scored by token overlap with the query."""

    def __init__(self, docs: Iterable[Document | dict[str, Any]] = ()) -> None:
        self._docs: dict[str, Document] = {}
        for d in docs:
            doc = d if isinstance(d, Document) else Document.model_validate(d)
            self._docs[doc.id] = doc

    @property
    def name(self) -> str:
        return "dummy"

    @property
    def metadata(self) -> dict[str, Any]:
        return {"size": len(self._docs)}

    def add(self, doc: Document | dict[str, Any]) -> None:
        d = doc if isinstance(doc, Document) else Document.model_validate(doc)
        self._docs[d.id] = d

    async def search(self, query: str, k: int = 5) -> list[Document]:
        q_tokens = _tokens(query)
        if not q_tokens:
            return []
        scored: list[tuple[float, Document]] = []
        for doc in self._docs.values():
            doc_tokens = _tokens(doc.text)
            overlap = len(q_tokens & doc_tokens)
            if overlap == 0:
                continue
            score = overlap / max(len(q_tokens), 1)
            scored.append(
                (
                    score,
                    Document(id=doc.id, text=doc.text, score=score, metadata=doc.metadata),
                )
            )
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [doc for _, doc in scored[:k]]

    async def fetch(self, doc_id: str) -> Document | None:
        doc = self._docs.get(doc_id)
        if doc is None:
            return None
        return Document(id=doc.id, text=doc.text, metadata=doc.metadata)
