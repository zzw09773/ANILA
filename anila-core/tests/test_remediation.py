"""Tests covering the remediation plan fixes.

Verifies:
1. history is parsed and included in provider requests for /chat and /agentic-chat
2. user/project scope isolation for document list, keyword_search, read_document
3. MemoryFileStore read/write/list_headers round-trip with correct MemoryFile fields
4. Engine exceptions produce SSE error events (not silent success)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from anila_core.api.server import _parse_history, create_app
from anila_core.models.message import AssistantMessage, StreamDelta, Usage, UserMessage
from anila_core.models.memory import MemoryFile, MemoryHeader, MemoryScope, MemoryType
from anila_core.providers.base import ProviderRequest
from anila_core.router.tool_router import ToolRegistry
from anila_core.storage.adapters.memory_file_store import MemoryFileStore
from anila_core.tools import (
    create_keyword_search_tool,
    create_read_document_tool,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class CapturingProvider:
    """Captures every ProviderRequest it receives for assertions."""

    def __init__(self, responses: list[list[StreamDelta]] | None = None) -> None:
        self.captured: list[ProviderRequest] = []
        self._responses = responses or [
            [
                StreamDelta(type="text", text="OK"),
                StreamDelta(type="stop", finish_reason="stop", usage=Usage()),
            ]
        ]
        self._call_count = 0

    async def stream_completion(self, request: ProviderRequest) -> AsyncIterator[StreamDelta]:
        self.captured.append(request)
        idx = min(self._call_count, len(self._responses) - 1)
        self._call_count += 1
        for delta in self._responses[idx]:
            yield delta


class ExplodingProvider:
    """Provider that raises on the first call -- simulates engine failure."""

    async def stream_completion(self, request: ProviderRequest) -> AsyncIterator[StreamDelta]:
        raise RuntimeError("upstream is down")
        yield  # make this an async generator


class MockPool:
    """Minimal mock for asyncpg pool.acquire()."""

    def __init__(self, rows: list[dict] | None = None) -> None:
        self._rows = rows or []

    def acquire(self) -> "_MockConn":
        return _MockConn(self._rows)


class _MockConn:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    async def __aenter__(self) -> "_MockConn":
        return self

    async def __aexit__(self, *_: Any) -> None:
        pass

    async def fetch(self, sql: str, *args: Any) -> list[dict]:
        return self._rows


class MockEmbedding:
    async def embed(self, texts: list[str], **_: Any) -> list[list[float]]:
        return [[0.1] * 3 for _ in texts]


class MockRetrieval:
    async def search(self, **_: Any) -> list:
        return []


# ---------------------------------------------------------------------------
# 1. history parsing
# ---------------------------------------------------------------------------


class TestParseHistory:
    """Unit tests for the _parse_history helper."""

    def test_empty_history_returns_empty_list(self) -> None:
        assert _parse_history([]) == []

    def test_parses_user_and_assistant_roles(self) -> None:
        raw = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
        ]
        msgs = _parse_history(raw)
        assert len(msgs) == 2
        assert isinstance(msgs[0], UserMessage)
        assert isinstance(msgs[1], AssistantMessage)
        assert msgs[0].get_text() == "Hello"
        assert msgs[1].get_text() == "Hi there"

    def test_skips_unknown_roles(self) -> None:
        raw = [
            {"role": "system", "content": "System prompt"},
            {"role": "user", "content": "Question"},
        ]
        msgs = _parse_history(raw)
        assert len(msgs) == 1
        assert isinstance(msgs[0], UserMessage)

    def test_skips_empty_content(self) -> None:
        raw = [
            {"role": "user", "content": ""},
            {"role": "user", "content": "Valid"},
        ]
        msgs = _parse_history(raw)
        assert len(msgs) == 1
        assert msgs[0].get_text() == "Valid"


@pytest.mark.asyncio
async def test_chat_endpoint_includes_history_in_provider_request() -> None:
    """POST /chat must include history messages before the new user message."""
    provider = CapturingProvider()
    app = create_app(provider=provider, tool_registry=ToolRegistry())

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/chat",
            json={
                "session_id": "s1",
                "user_message": "What is RAG?",
                "history": [
                    {"role": "user", "content": "Hello"},
                    {"role": "assistant", "content": "Hi! How can I help?"},
                ],
            },
        )
        assert resp.status_code == 200

    assert len(provider.captured) >= 1
    first_request = provider.captured[0]
    # history (2 msgs) + new user message = 3 total
    assert len(first_request.messages) == 3
    assert isinstance(first_request.messages[0], UserMessage)
    assert isinstance(first_request.messages[1], AssistantMessage)
    assert isinstance(first_request.messages[2], UserMessage)
    assert first_request.messages[2].get_text() == "What is RAG?"


@pytest.mark.asyncio
async def test_agentic_chat_includes_history_in_provider_request() -> None:
    """POST /agentic-chat must include history messages before the new user message."""
    provider = CapturingProvider()
    app = create_app(
        provider=provider,
        tool_registry=ToolRegistry(),
        embedding_provider=MockEmbedding(),
        retrieval_provider=MockRetrieval(),
        db_pool=MockPool(),
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/agentic-chat",
            json={
                "session_id": "s2",
                "user_message": "Follow-up question",
                "history": [
                    {"role": "user", "content": "First turn"},
                    {"role": "assistant", "content": "First answer"},
                ],
            },
        )
        assert resp.status_code == 200

    first_request = provider.captured[0]
    assert len(first_request.messages) == 3
    assert first_request.messages[0].get_text() == "First turn"
    assert first_request.messages[2].get_text() == "Follow-up question"


# ---------------------------------------------------------------------------
# 2. Scope isolation
# ---------------------------------------------------------------------------


class TestKeywordSearchScope:
    """keyword_search must only query within the closure-bound user/project."""

    @pytest.mark.asyncio
    async def test_sql_scoped_to_user_project(self) -> None:
        captured_args: list[tuple] = []

        class TrackingConn(_MockConn):
            async def fetch(self, sql: str, *args: Any) -> list[dict]:
                captured_args.append(args)
                return []

        class TrackingPool(MockPool):
            def acquire(self) -> "TrackingConn":
                return TrackingConn([])

        tool = create_keyword_search_tool(
            db_pool=TrackingPool(),
            user_id="alice",
            project_id="proj-x",
        )
        await tool.implementation({"query": "test"})

        assert len(captured_args) >= 1
        args = captured_args[0]
        assert "alice" in args
        assert "proj-x" in args

    @pytest.mark.asyncio
    async def test_llm_cannot_override_scope(self) -> None:
        """user_id/project_id must not appear in the tool input schema."""
        tool = create_keyword_search_tool(db_pool=MockPool(), user_id="alice", project_id="p1")
        schema_props = tool.input_schema.get("properties", {})
        assert "user_id" not in schema_props
        assert "project_id" not in schema_props


class TestReadDocumentScope:
    """read_document must only query within the closure-bound user/project."""

    @pytest.mark.asyncio
    async def test_sql_scoped_to_user_project(self) -> None:
        captured_args: list[tuple] = []

        class TrackingConn(_MockConn):
            async def fetch(self, sql: str, *args: Any) -> list[dict]:
                captured_args.append(args)
                return []

        class TrackingPool(MockPool):
            def acquire(self) -> "TrackingConn":
                return TrackingConn([])

        tool = create_read_document_tool(
            db_pool=TrackingPool(),
            user_id="bob",
            project_id="proj-y",
        )
        await tool.implementation({"document_id": "doc-123"})

        assert len(captured_args) >= 1
        args = captured_args[0]
        assert "bob" in args
        assert "proj-y" in args

    @pytest.mark.asyncio
    async def test_returns_not_found_for_other_scope(self) -> None:
        """doc exists in another scope -- tool should return not-found."""
        tool = create_read_document_tool(
            db_pool=MockPool(rows=[]),  # empty = no match
            user_id="charlie",
            project_id="p",
        )
        result = await tool.implementation({"document_id": "foreign-doc"})
        assert "error" in result


# ---------------------------------------------------------------------------
# 3. MemoryFileStore round-trip
# ---------------------------------------------------------------------------


class TestMemoryFileStoreRoundTrip:
    """MemoryFileStore read/write must use correct MemoryFile fields."""

    def _make_memory(self, tmp_path: Path) -> tuple[MemoryFileStore, str, MemoryFile]:
        store = MemoryFileStore(str(tmp_path))
        file_path = str(tmp_path / "test_mem.md")
        header = MemoryHeader(
            filename="test_mem.md",
            file_path=file_path,
            title="Test Memory",
            description="A test memory file",
            memory_type=MemoryType.GENERAL,
            scope=MemoryScope.PROJECT,
        )
        memory = MemoryFile(header=header, body="This is the body content.")
        return store, file_path, memory

    @pytest.mark.asyncio
    async def test_write_creates_file(self, tmp_path: Path) -> None:
        store, file_path, memory = self._make_memory(tmp_path)
        await store.write(file_path, memory)
        assert Path(file_path).exists()

    @pytest.mark.asyncio
    async def test_read_returns_memory_file(self, tmp_path: Path) -> None:
        store, file_path, memory = self._make_memory(tmp_path)
        await store.write(file_path, memory)

        result = await store.read(file_path)
        assert result is not None
        # Must return correct types -- not dict
        assert isinstance(result, MemoryFile)
        assert isinstance(result.header, MemoryHeader)
        assert isinstance(result.body, str)

    @pytest.mark.asyncio
    async def test_round_trip_preserves_title_and_body(self, tmp_path: Path) -> None:
        store, file_path, memory = self._make_memory(tmp_path)
        await store.write(file_path, memory)
        result = await store.read(file_path)

        assert result is not None
        assert result.header.title == "Test Memory"
        assert "body content" in result.body

    @pytest.mark.asyncio
    async def test_read_nonexistent_returns_none(self, tmp_path: Path) -> None:
        store = MemoryFileStore(str(tmp_path))
        result = await store.read(str(tmp_path / "does_not_exist.md"))
        assert result is None

    @pytest.mark.asyncio
    async def test_list_headers_returns_memory_headers(self, tmp_path: Path) -> None:
        store, file_path, memory = self._make_memory(tmp_path)
        await store.write(file_path, memory)

        # list_headers expects files under {base}/{user_id}/{project_id}/{scope}/
        scope_dir = tmp_path / "u1" / "p1" / "project"
        scope_dir.mkdir(parents=True)
        dest = scope_dir / "test_mem.md"
        dest.write_text(Path(file_path).read_text())

        store2 = MemoryFileStore(str(tmp_path))
        headers = await store2.list_headers(user_id="u1", project_id="p1", scope="project")
        assert len(headers) == 1
        assert isinstance(headers[0], MemoryHeader)
        assert headers[0].title == "Test Memory"


# ---------------------------------------------------------------------------
# 4. SSE error events on engine failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_engine_failure_emits_error_event() -> None:
    """When QueryEngine raises, /chat must emit an error SSE event."""
    provider = ExplodingProvider()
    app = create_app(provider=provider, tool_registry=ToolRegistry())

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/chat",
            json={
                "session_id": "err-session",
                "user_message": "trigger error",
            },
        )
        assert resp.status_code == 200  # HTTP layer is fine

        events = [
            json.loads(line[6:])
            for line in resp.text.split("\n")
            if line.startswith("data: ")
        ]
        types = [e["type"] for e in events]

        assert "error" in types, f"Expected 'error' event, got: {types}"

        # Terminal stream_done must reflect failure status
        done = next((e for e in events if e["type"] == "stream_done"), None)
        assert done is not None
        assert done["payload"]["status"] == "error"


@pytest.mark.asyncio
async def test_agentic_chat_engine_failure_emits_error_event() -> None:
    """When QueryEngine raises, /agentic-chat must emit an error SSE event."""
    provider = ExplodingProvider()
    app = create_app(
        provider=provider,
        tool_registry=ToolRegistry(),
        embedding_provider=MockEmbedding(),
        retrieval_provider=MockRetrieval(),
        db_pool=MockPool(),
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/agentic-chat",
            json={
                "session_id": "err-agentic",
                "user_message": "trigger error",
            },
        )
        assert resp.status_code == 200

        events = [
            json.loads(line[6:])
            for line in resp.text.split("\n")
            if line.startswith("data: ")
        ]
        types = [e["type"] for e in events]
        assert "error" in types

        done = next((e for e in events if e["type"] == "stream_done"), None)
        assert done is not None
        assert done["payload"]["status"] == "error"


@pytest.mark.asyncio
async def test_compact_returns_501() -> None:
    """POST /sessions/{id}/compact must return 501 Not Implemented."""
    app = create_app(provider=CapturingProvider(), tool_registry=ToolRegistry())
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/sessions/abc/compact")
    assert resp.status_code == 501
