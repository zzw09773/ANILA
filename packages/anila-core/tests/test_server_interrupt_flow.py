"""End-to-end pause/resume via the FastAPI server.

Wires the ask_user tool into a fresh ``create_app()`` and exercises:

- POST /chat → SSE includes ``interrupt_requested`` event then closes paused
- GET  /sessions/{id}/state → reports the pending interrupt + history
- POST /sessions/{id}/answer → resumes, RESUMED event fires, answer streamed back
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from anila_core.api.server import create_app
from anila_core.memory import MemorySession, close_all_connections
from anila_core.providers.mock import (
    MockProvider,
    ScriptedResponse,
    ScriptedToolCall,
)
from anila_core.router.tool_router import ToolRegistry
from anila_core.tools.ask_user import ask_user_tool


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def session_db(tmp_path: Path):
    db = tmp_path / "sessions.db"
    yield db
    await close_all_connections()


@pytest.fixture
def app_and_provider(session_db: Path):
    """Two-turn script: pause on ask_user, then a final answer after resume."""
    provider = MockProvider(
        [
            ScriptedResponse(
                tool_calls=[
                    ScriptedToolCall(
                        name="ask_user",
                        input={
                            "question": "what color?",
                            "options": [
                                {"label": "blue"},
                                {"label": "red"},
                            ],
                        },
                        tool_id="c-ask",
                    )
                ],
                finish_reason="tool_use",
            ),
            ScriptedResponse(text="going with blue", finish_reason="end_turn"),
        ]
    )
    registry = ToolRegistry()
    registry.register(ask_user_tool())
    app = create_app(
        provider=provider,
        tool_registry=registry,
        api_dev_mode=True,
        session_db_path=str(session_db),
    )
    return app, provider


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_sse(body: str) -> list[dict]:
    """Parse ``event: x\\ndata: {...}\\n\\n`` blocks into dicts."""
    out: list[dict] = []
    for block in body.strip().split("\n\n"):
        event_name = ""
        data_payload = ""
        for line in block.splitlines():
            if line.startswith("event: "):
                event_name = line[7:]
            elif line.startswith("data: "):
                data_payload = line[6:]
        if data_payload:
            try:
                parsed = json.loads(data_payload)
            except json.JSONDecodeError:
                continue
            out.append({"event": event_name, "data": parsed})
    return out


# ---------------------------------------------------------------------------
# Pause path
# ---------------------------------------------------------------------------


def test_chat_pauses_on_ask_user_and_emits_interrupt_event(
    app_and_provider,
) -> None:
    app, _ = app_and_provider
    client = TestClient(app)

    response = client.post(
        "/chat",
        json={
            "session_id": "s-pause",
            "user_message": "pick a color",
            "history": [],
            "system_prompt": "be helpful",
        },
    )
    assert response.status_code == 200
    events = _parse_sse(response.text)
    types = [e["event"] for e in events]
    assert "interrupt_requested" in types
    assert "stream_done" in types
    interrupt_event = next(
        e for e in events if e["event"] == "interrupt_requested"
    )
    payload = interrupt_event["data"]["payload"]
    assert payload["kind"] == "ask_user"
    assert payload["payload"]["question"] == "what color?"
    # Stream finished with status "paused", not "completed" / "error".
    done = next(e for e in events if e["event"] == "stream_done")
    assert done["data"]["payload"]["status"] == "paused"


def test_session_state_reports_pending_interrupt(app_and_provider) -> None:
    app, _ = app_and_provider
    client = TestClient(app)

    client.post(
        "/chat",
        json={
            "session_id": "s-state",
            "user_message": "pick a color",
            "history": [],
            "system_prompt": "x",
        },
    )

    state = client.get("/sessions/s-state/state").json()
    assert state["session_id"] == "s-state"
    assert len(state["messages"]) == 2  # user + assistant(tool_call)
    assert len(state["pending_interrupts"]) == 1
    interrupt = state["pending_interrupts"][0]
    assert interrupt["kind"] == "ask_user"
    assert interrupt["payload"]["question"] == "what color?"


# ---------------------------------------------------------------------------
# Resume path
# ---------------------------------------------------------------------------


def test_answer_endpoint_resumes_and_streams_final_answer(
    app_and_provider,
) -> None:
    app, _ = app_and_provider
    client = TestClient(app)

    # First turn → pause.
    pause = client.post(
        "/chat",
        json={
            "session_id": "s-resume",
            "user_message": "pick a color",
            "history": [],
            "system_prompt": "x",
        },
    )
    pause_events = _parse_sse(pause.text)
    interrupt_id = next(
        e["data"]["payload"]["interrupt_id"]
        for e in pause_events
        if e["event"] == "interrupt_requested"
    )

    # Resume.
    resume = client.post(
        "/sessions/s-resume/answer",
        json={
            "interrupt_id": interrupt_id,
            "answer": {"selected": ["blue"]},
        },
    )
    assert resume.status_code == 200
    resume_events = _parse_sse(resume.text)
    types = [e["event"] for e in resume_events]
    assert types[0] == "resumed"
    assert "message_delta" in types
    assert "stream_done" in types
    # Aggregate text deltas — the model said "going with blue".
    text = "".join(
        e["data"]["payload"]["text"]
        for e in resume_events
        if e["event"] == "message_delta"
    )
    assert "blue" in text
    # Pending interrupts cleared.
    state = client.get("/sessions/s-resume/state").json()
    assert state["pending_interrupts"] == []


def test_answer_endpoint_unknown_interrupt_yields_error_event(
    app_and_provider,
) -> None:
    app, _ = app_and_provider
    client = TestClient(app)
    resume = client.post(
        "/sessions/s-ghost/answer",
        json={"interrupt_id": "does-not-exist", "answer": {}},
    )
    # Endpoint still returns 200 SSE; error surfaces as event.
    assert resume.status_code == 200
    events = _parse_sse(resume.text)
    assert any(e["event"] == "error" for e in events)


# ---------------------------------------------------------------------------
# session_factory override
# ---------------------------------------------------------------------------


def test_chat_emits_todos_updated_event_when_tool_called(
    session_db: Path,
) -> None:
    """End-to-end check: model calls todo_write → SSE carries todos_updated.

    Verifies the AgentContext.event_emitter wiring (PR 4) by exercising
    the full server stack rather than poking at it directly.
    """
    from anila_core.tools.todo_write import todo_write_tool

    provider = MockProvider(
        [
            ScriptedResponse(
                tool_calls=[
                    ScriptedToolCall(
                        name="todo_write",
                        input={
                            "todos": [
                                {
                                    "content": "step 1",
                                    "active_form": "doing step 1",
                                    "status": "in_progress",
                                },
                                {
                                    "content": "step 2",
                                    "active_form": "doing step 2",
                                },
                            ]
                        },
                        tool_id="c-todo",
                    )
                ],
                finish_reason="tool_use",
            ),
            ScriptedResponse(text="all set", finish_reason="end_turn"),
        ]
    )
    registry = ToolRegistry()
    registry.register(todo_write_tool())
    app = create_app(
        provider=provider,
        tool_registry=registry,
        api_dev_mode=True,
        session_db_path=str(session_db),
    )
    client = TestClient(app)

    response = client.post(
        "/chat",
        json={
            "session_id": "s-todos",
            "user_message": "do two things",
            "history": [],
            "system_prompt": "x",
        },
    )
    assert response.status_code == 200
    events = _parse_sse(response.text)
    todos_events = [e for e in events if e["event"] == "todos_updated"]
    assert len(todos_events) == 1
    payload = todos_events[0]["data"]["payload"]
    items = payload["todos"]
    assert [t["content"] for t in items] == ["step 1", "step 2"]
    assert items[0]["status"] == "in_progress"
    assert items[1]["status"] == "pending"


def test_session_factory_override_respected() -> None:
    """When session_factory is supplied, server uses it instead of SQLite."""
    captured = {}

    def factory(session_id: str):
        sess = MemorySession(session_id)
        captured[session_id] = sess
        return sess

    provider = MockProvider(
        [ScriptedResponse(text="hello", finish_reason="end_turn")]
    )
    app = create_app(
        provider=provider,
        tool_registry=ToolRegistry(),
        api_dev_mode=True,
        session_factory=factory,
    )
    client = TestClient(app)
    client.post(
        "/chat",
        json={
            "session_id": "s-factory",
            "user_message": "hi",
            "history": [],
            "system_prompt": "x",
        },
    )
    assert "s-factory" in captured
    # MemorySession persists in-process — the factory was used.
    assert isinstance(captured["s-factory"], MemorySession)
