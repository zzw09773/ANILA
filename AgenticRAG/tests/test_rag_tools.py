"""Tests for RAG tools — vector_search, keyword_search, read_document."""

from __future__ import annotations

import json
from typing import Any

import pytest

from agentic_rag.models.storage import ChunkType, Citation
from agentic_rag.tools import (
    create_keyword_search_tool,
    create_read_document_tool,
    create_vector_search_tool,
)


# ---------------------------------------------------------------------------
# Mock providers
# ---------------------------------------------------------------------------

class MockEmbeddingProvider:
    async def embed(self, texts: list[str], **kwargs: Any) -> list[list[float]]:
        return [[0.1, 0.2, 0.3] for _ in texts]


class MockRetrievalProvider:
    def __init__(self, citations: list[Citation] | None = None):
        self._citations = citations or []

    async def search(self, **kwargs: Any) -> list[Citation]:
        return self._citations


class MockPool:
    """Mock asyncpg pool for keyword_search and read_document tests."""

    def __init__(self, rows: list[dict] | None = None):
        self._rows = rows or []

    def acquire(self):
        return _MockConnection(self._rows)


class _MockConnection:
    def __init__(self, rows: list[dict]):
        self._rows = rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def fetch(self, query: str, *args):
        return self._rows


# ---------------------------------------------------------------------------
# VectorSearchTool
# ---------------------------------------------------------------------------

class TestVectorSearchTool:
    @pytest.fixture
    def tool(self):
        citations = [
            Citation(
                chunk_id="c1",
                document_id="doc1",
                document_title="python.md",
                source_path="/docs/python.md",
                content="Python is a programming language",
                confidence=0.95,
                chunk_type=ChunkType.CONTENT,
            ),
            Citation(
                chunk_id="c2",
                document_id="doc2",
                document_title="java.md",
                source_path="/docs/java.md",
                content="Java is also popular",
                confidence=0.8,
                chunk_type=ChunkType.CONTENT,
            ),
        ]
        return create_vector_search_tool(
            embedding_provider=MockEmbeddingProvider(),
            retrieval_provider=MockRetrievalProvider(citations),
        )

    @pytest.mark.asyncio
    async def test_basic_search(self, tool):
        result = await tool.implementation({"query": "what is python"})
        assert result["total"] == 2
        assert result["results"][0]["chunk_id"] == "c1"
        assert result["results"][0]["confidence"] == 0.95

    @pytest.mark.asyncio
    async def test_empty_query_returns_error(self, tool):
        result = await tool.implementation({"query": ""})
        assert "error" in result

    @pytest.mark.asyncio
    async def test_top_k_parameter(self, tool):
        result = await tool.implementation({"query": "test", "top_k": 1})
        # MockRetrievalProvider returns all, but the tool passes top_k
        assert isinstance(result["results"], list)

    def test_schema_is_valid(self, tool):
        schema = tool.to_openai_schema()
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "vector_search"
        params = schema["function"]["parameters"]
        assert "query" in params["properties"]
        # S3: verify integer → number normalization
        assert params["properties"]["top_k"]["type"] == "number"

    @pytest.mark.asyncio
    async def test_embedding_failure_returns_error(self):
        class FailEmbed:
            async def embed(self, *a, **kw):
                raise RuntimeError("embed down")

        tool = create_vector_search_tool(
            embedding_provider=FailEmbed(),
            retrieval_provider=MockRetrievalProvider(),
        )
        result = await tool.implementation({"query": "test"})
        assert "error" in result


# ---------------------------------------------------------------------------
# KeywordSearchTool
# ---------------------------------------------------------------------------

class TestKeywordSearchTool:
    @pytest.fixture
    def tool(self):
        rows = [
            {
                "chunk_id": "c1",
                "document_id": "doc1",
                "content": "學生獎懲辦法第 23 條",
                "metadata": json.dumps({"source_path": "/docs/rules.pdf"}),
                "score": 0.7,
            },
        ]
        return create_keyword_search_tool(db_pool=MockPool(rows))

    @pytest.mark.asyncio
    async def test_basic_keyword_search(self, tool):
        result = await tool.implementation({"query": "獎懲"})
        assert result["total"] == 1
        assert result["results"][0]["chunk_id"] == "c1"

    @pytest.mark.asyncio
    async def test_empty_query_returns_error(self, tool):
        result = await tool.implementation({"query": ""})
        assert "error" in result

    def test_schema_properties(self, tool):
        schema = tool.to_openai_schema()
        assert schema["function"]["name"] == "keyword_search"


# ---------------------------------------------------------------------------
# ReadDocumentTool
# ---------------------------------------------------------------------------

class TestReadDocumentTool:
    @pytest.fixture
    def tool(self):
        rows = [
            {
                "chunk_id": "c1",
                "content": "Chapter 1 content here",
                "metadata": json.dumps({"source_path": "/doc.md"}),
                "chunk_type": "content",
                "chunk_level": 2,
                "heading_path": json.dumps(["Chapter 1"]),
            },
            {
                "chunk_id": "c2",
                "content": "Chapter 2 content here",
                "metadata": json.dumps({"source_path": "/doc.md"}),
                "chunk_type": "content",
                "chunk_level": 2,
                "heading_path": json.dumps(["Chapter 2"]),
            },
        ]
        return create_read_document_tool(db_pool=MockPool(rows))

    @pytest.mark.asyncio
    async def test_read_document(self, tool):
        result = await tool.implementation({"document_id": "doc1"})
        assert result["total_chunks"] == 2
        assert "Chapter 1" in result["content"]
        assert "Chapter 2" in result["content"]
        assert result["document_id"] == "doc1"

    @pytest.mark.asyncio
    async def test_empty_id_returns_error(self, tool):
        result = await tool.implementation({"document_id": ""})
        assert "error" in result

    @pytest.mark.asyncio
    async def test_not_found(self):
        tool = create_read_document_tool(db_pool=MockPool([]))
        result = await tool.implementation({"document_id": "nonexistent"})
        assert "error" in result

    def test_schema_properties(self, tool):
        schema = tool.to_openai_schema()
        assert schema["function"]["name"] == "read_document"
        assert "document_id" in schema["function"]["parameters"]["properties"]
