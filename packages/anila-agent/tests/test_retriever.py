"""Retriever Protocol 一致性 + Document 形狀（平台契約）。"""

from __future__ import annotations

import pytest

from anila_agent.retrieval.base import Retriever
from anila_agent.retrieval.dummy import DummyRetriever
from anila_agent.retrieval.schemas import Document

pytestmark = pytest.mark.unit


def test_dummy_satisfies_protocol():
    assert isinstance(DummyRetriever(), Retriever)


def test_document_shape_defaults():
    d = Document(id="x", text="t")
    assert d.id == "x" and d.text == "t"
    assert d.score is None
    assert d.metadata == {}


async def test_search_returns_sorted_documents():
    r = DummyRetriever([("a", "alpha beta gamma"), ("b", "beta gamma"), ("c", "delta")])
    hits = await r.search("beta gamma", k=5)
    assert hits and all(isinstance(h, Document) for h in hits)
    assert len(hits) <= 5
    scores = [h.score for h in hits]
    assert scores == sorted(scores, reverse=True)  # 遞減排序


async def test_search_respects_k():
    r = DummyRetriever([("a", "x y"), ("b", "x z"), ("c", "x w")])
    assert len(await r.search("x", k=2)) <= 2


async def test_empty_query_returns_empty():
    assert await DummyRetriever().search("") == []


async def test_fetch_hit_and_miss():
    r = DummyRetriever([("a", "alpha")])
    got = await r.fetch("a")
    assert got is not None and got.text == "alpha"
    assert await r.fetch("missing") is None
