"""Tests for the /agentic-chat endpoint — tool-driven RAG."""

from __future__ import annotations

import json
from typing import AsyncIterator

import pytest
from httpx import AsyncClient, ASGITransport

from anila_core.api.server import create_app
from anila_core.models.message import StreamDelta, ToolCallDelta, Usage
from anila_core.providers.base import ProviderRequest
from anila_core.router.tool_router import ToolRegistry


class FakeProvider:
    """Provider that simulates a model calling vector_search then answering."""

    def __init__(self, responses: list[list[StreamDelta]] | None = None):
        self._responses = responses or [self._default_response()]
        self._call_count = 0

    @staticmethod
    def _default_response() -> list[StreamDelta]:
        return [
            StreamDelta(type="text", text="No search needed. Hello!"),
            StreamDelta(type="stop", finish_reason="stop", usage=Usage(input_tokens=10, output_tokens=5)),
        ]

    async def stream_completion(self, request: ProviderRequest) -> AsyncIterator[StreamDelta]:
        idx = min(self._call_count, len(self._responses) - 1)
        self._call_count += 1
        for delta in self._responses[idx]:
            yield delta


class MockPool:
    """Minimal mock for db_pool.acquire()."""
    def acquire(self):
        return _MockConn()

class _MockConn:
    async def __aenter__(self):
        return self
    async def __aexit__(self, *a):
        pass
    async def fetch(self, *a):
        return []


class MockEmbedding:
    async def embed(self, texts, **kw):
        return [[0.1] * 3 for _ in texts]


class MockRetrieval:
    async def search(self, **kw):
        return []


@pytest.fixture
def app_no_tools():
    """App with no RAG dependencies — agentic-chat should still work."""
    provider = FakeProvider()
    return create_app(provider=provider, tool_registry=ToolRegistry())


@pytest.fixture
def app_with_rag():
    """App with embedding + retrieval + db_pool — full agentic tools."""
    provider = FakeProvider()
    return create_app(
        provider=provider,
        tool_registry=ToolRegistry(),
        embedding_provider=MockEmbedding(),
        retrieval_provider=MockRetrieval(),
        db_pool=MockPool(),
    )


@pytest.mark.asyncio
async def test_agentic_chat_returns_sse(app_with_rag):
    """POST /agentic-chat should return SSE stream."""
    transport = ASGITransport(app=app_with_rag)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/agentic-chat", json={
            "session_id": "test-session",
            "user_message": "什麼是 RAG？",
        })
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]

        # Parse SSE events
        events = [
            line for line in resp.text.split("\n")
            if line.startswith("data: ")
        ]
        assert len(events) >= 2  # at least message + done

        # Check we got a message delta and stream_done
        types = []
        for line in events:
            data = json.loads(line[6:])
            types.append(data["type"])
        assert "message_delta" in types
        assert "stream_done" in types


@pytest.mark.asyncio
async def test_agentic_chat_no_providers(app_no_tools):
    """Without providers, /agentic-chat still works (just no RAG tools)."""
    transport = ASGITransport(app=app_no_tools)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/agentic-chat", json={
            "session_id": "test-session",
            "user_message": "Hello",
        })
        assert resp.status_code == 200


@pytest.mark.asyncio
async def test_agentic_chat_custom_system_prompt(app_with_rag):
    """Custom system_prompt in request should override default."""
    transport = ASGITransport(app=app_with_rag)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/agentic-chat", json={
            "session_id": "test-session",
            "user_message": "Hi",
            "system_prompt": "You are a pirate.",
        })
        assert resp.status_code == 200


@pytest.mark.asyncio
async def test_agentic_chat_with_tool_calling():
    """Model calls vector_search tool, gets results, then answers."""
    # Turn 1: model calls vector_search
    turn1 = [
        StreamDelta(
            type="tool_call",
            tool_call=ToolCallDelta(
                id="call_1",
                name="vector_search",
                input_partial='{"query": "RAG"}',
            ),
        ),
        StreamDelta(type="stop", finish_reason="tool_calls", usage=Usage(input_tokens=20, output_tokens=10)),
    ]
    # Turn 2: model gives final answer
    turn2 = [
        StreamDelta(type="text", text="RAG is Retrieval-Augmented Generation."),
        StreamDelta(type="stop", finish_reason="stop", usage=Usage(input_tokens=30, output_tokens=15)),
    ]

    provider = FakeProvider(responses=[turn1, turn2])
    app = create_app(
        provider=provider,
        tool_registry=ToolRegistry(),
        embedding_provider=MockEmbedding(),
        retrieval_provider=MockRetrieval(),
        db_pool=MockPool(),
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/agentic-chat", json={
            "session_id": "test-session",
            "user_message": "什麼是 RAG？",
        })
        assert resp.status_code == 200

        events = [
            line for line in resp.text.split("\n")
            if line.startswith("data: ")
        ]
        types = [json.loads(line[6:])["type"] for line in events]
        # Should have: tool_call_started, message_delta (answer), usage, done
        assert "tool_call_started" in types
        assert "message_delta" in types
        assert "stream_done" in types
