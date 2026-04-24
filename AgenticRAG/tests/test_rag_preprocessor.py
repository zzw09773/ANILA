"""Tests for RagPreprocessor — context injection, filtering, fallback."""

from __future__ import annotations

import pytest

from agentic_rag.engine.rag_preprocessor import (
    RagPreprocessor,
    _extract_latest_query,
    _format_context,
    _inject_context,
)
from agentic_rag.models.message import AssistantMessage, UserMessage
from agentic_rag.models.storage import Citation
from agentic_rag.providers.embedding_mock import MockEmbeddingProvider


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_chunk(content: str, score: float = 0.9, source: str = "test.pdf") -> Citation:
    """Factory kept as ``make_chunk`` for call-site compat, but now returns a Citation."""
    return Citation(
        chunk_id=f"c-{content[:8]}",
        document_id="doc1",
        document_title="test.pdf",
        source_path=source,
        content=content,
        confidence=score,
        metadata={"source_path": source},
    )


class MockRetriever:
    def __init__(self, citations: list[Citation]) -> None:
        self._citations = citations

    async def search(
        self,
        query_embedding,
        user_id,
        project_id,
        top_k=5,
        min_score=0.0,
        include_parent_context=True,
    ):
        return [c for c in self._citations if c.confidence >= min_score][:top_k]


# ---------------------------------------------------------------------------
# _extract_latest_query
# ---------------------------------------------------------------------------

def test_extract_query_from_string_content():
    msgs = [UserMessage(content="What is RAG?")]
    assert _extract_latest_query(msgs) == "What is RAG?"


def test_extract_query_from_block_content():
    msgs = [UserMessage(content=[{"type": "text", "text": "Explain chunking"}])]
    assert _extract_latest_query(msgs) == "Explain chunking"


def test_extract_query_picks_last_user_message():
    msgs = [
        UserMessage(content="First"),
        AssistantMessage(content="Answer"),
        UserMessage(content="Second"),
    ]
    assert _extract_latest_query(msgs) == "Second"


def test_extract_query_empty_history():
    assert _extract_latest_query([]) == ""


# ---------------------------------------------------------------------------
# _format_context
# ---------------------------------------------------------------------------

def test_format_context_contains_source_and_score():
    chunks = [make_chunk("Important text", score=0.92, source="/docs/report.pdf")]
    text = _format_context(chunks)
    assert "Source 1" in text
    assert "0.92" in text
    assert "test.pdf" in text   # Citation.document_title is used in the trail
    assert "Important text" in text


def test_format_context_has_rag_markers():
    text = _format_context([make_chunk("x")])
    assert text.startswith("[RAG Context")
    assert text.endswith("[End RAG Context]")


# ---------------------------------------------------------------------------
# _inject_context
# ---------------------------------------------------------------------------

def test_inject_context_prepends_to_last_user_message():
    history = [UserMessage(content="My question")]
    augmented = _inject_context(history, "CONTEXT_BLOCK")
    assert len(augmented) == 1
    assert "CONTEXT_BLOCK" in augmented[0].content
    assert "My question" in augmented[0].content


def test_inject_context_block_format():
    history = [UserMessage(content=[{"type": "text", "text": "Q?"}])]
    augmented = _inject_context(history, "CTX")
    content = augmented[0].content
    assert isinstance(content, list)
    assert content[0]["text"].startswith("CTX")


def test_inject_context_no_user_message_prepends_synthetic():
    history = [AssistantMessage(content="Hi")]
    augmented = _inject_context(history, "CTX")
    assert len(augmented) == 2
    assert isinstance(augmented[0], UserMessage)


# ---------------------------------------------------------------------------
# RagPreprocessor integration
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_preprocessor_injects_context():
    embedder = MockEmbeddingProvider(dimension=4096)
    chunks = [make_chunk("RAG is great")]
    retriever = MockRetriever(chunks)
    preprocessor = RagPreprocessor(
        embedding_provider=embedder,
        retrieval_provider=retriever,
        user_id="u",
        project_id="p",
        top_k=5,
        min_score=0.0,
    )
    history = [UserMessage(content="Tell me about RAG")]
    augmented, context = await preprocessor.preprocess(history)
    assert context is not None
    assert "RAG is great" in context
    assert "RAG is great" in augmented[-1].content


@pytest.mark.asyncio
async def test_preprocessor_no_chunks_returns_original():
    embedder = MockEmbeddingProvider(dimension=4096)
    retriever = MockRetriever([])
    preprocessor = RagPreprocessor(
        embedding_provider=embedder,
        retrieval_provider=retriever,
        user_id="u",
        project_id="p",
    )
    history = [UserMessage(content="question")]
    augmented, context = await preprocessor.preprocess(history)
    assert context is None
    assert augmented is history  # unchanged reference


@pytest.mark.asyncio
async def test_preprocessor_min_score_filters_chunks():
    embedder = MockEmbeddingProvider(dimension=4096)
    chunks = [
        make_chunk("high score", score=0.95),
        make_chunk("low score", score=0.3),
    ]
    retriever = MockRetriever(chunks)
    preprocessor = RagPreprocessor(
        embedding_provider=embedder,
        retrieval_provider=retriever,
        user_id="u",
        project_id="p",
        min_score=0.8,
    )
    history = [UserMessage(content="question")]
    augmented, context = await preprocessor.preprocess(history)
    assert context is not None
    assert "high score" in context
    assert "low score" not in context


@pytest.mark.asyncio
async def test_preprocessor_empty_history_returns_unchanged():
    embedder = MockEmbeddingProvider()
    retriever = MockRetriever([])
    preprocessor = RagPreprocessor(
        embedding_provider=embedder,
        retrieval_provider=retriever,
        user_id="u",
        project_id="p",
    )
    augmented, context = await preprocessor.preprocess([])
    assert context is None
    assert augmented == []
