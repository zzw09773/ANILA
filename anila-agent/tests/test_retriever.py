"""DummyRetriever scoring tests."""

from __future__ import annotations

import pytest

from anila_agent.models.schemas import Document
from anila_agent.retrieval.dummy import DummyRetriever


@pytest.mark.unit
async def test_search_returns_overlap_only() -> None:
    retriever = DummyRetriever(
        [
            Document(id="a", text="memory directory MEMORY.md index"),
            Document(id="b", text="vector store embeddings"),
        ]
    )
    results = await retriever.search("memory MEMORY", k=5)
    assert [d.id for d in results] == ["a"]


@pytest.mark.unit
async def test_search_orders_by_score() -> None:
    retriever = DummyRetriever(
        [
            Document(id="lots", text="alpha beta gamma delta"),
            Document(id="few", text="alpha"),
        ]
    )
    results = await retriever.search("alpha beta gamma delta", k=5)
    assert [d.id for d in results] == ["lots", "few"]


@pytest.mark.unit
async def test_fetch_round_trips() -> None:
    retriever = DummyRetriever([Document(id="x", text="payload")])
    doc = await retriever.fetch("x")
    assert doc is not None
    assert doc.text == "payload"
    assert await retriever.fetch("missing") is None


@pytest.mark.unit
async def test_search_caps_at_k() -> None:
    retriever = DummyRetriever(
        [Document(id=str(i), text="alpha beta") for i in range(20)]
    )
    results = await retriever.search("alpha", k=3)
    assert len(results) == 3
