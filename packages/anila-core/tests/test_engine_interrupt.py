"""QueryEngine + Approvals integration: pause-resume turn loop.

Builds a fake ``ask_user`` tool that returns :class:`InterruptItem`,
runs a turn that triggers it, asserts:

- :class:`RunPaused` is raised with the right session/interrupt ids
- conversation history + interrupt are persisted to the Session
- ``resume_from_interrupt`` rehydrates and produces a final answer
- a sibling tool call in the same turn has its result preserved on resume
"""

from __future__ import annotations

import pytest

from anila_core.engine.approvals import MultipleInterruptsError, RunPaused
from anila_core.engine.query_engine import QueryConfig, QueryEngine
from anila_core.memory import MemorySession, new_session_id
from anila_core.models.interrupt import InterruptItem
from anila_core.models.message import UserMessage
from anila_core.models.tool import ToolDefinition, ToolSafety
from anila_core.providers.mock import (
    MockProvider,
    ScriptedResponse,
    ScriptedToolCall,
)
from anila_core.router.tool_router import ToolRegistry


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def make_ask_user_tool(interrupt_id: str = "int-fixed") -> ToolDefinition:
    """A fake ask_user tool that always returns the same InterruptItem."""

    async def impl(input: dict, **_):
        return InterruptItem(
            id=interrupt_id,
            kind="ask_user",
            payload={
                "question": input.get("question", ""),
                "options": input.get("options", []),
            },
        )

    return ToolDefinition(
        name="ask_user",
        description="Ask the user a clarifying question.",
        input_schema={
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "options": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["question"],
        },
        safety=ToolSafety.READ_ONLY,
        implementation=impl,
    )


def make_echo_tool() -> ToolDefinition:
    async def impl(input: dict, **_):
        return f"echo:{input.get('text', '')}"

    return ToolDefinition(
        name="echo",
        description="Echo input.",
        input_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
        },
        safety=ToolSafety.READ_ONLY,
        implementation=impl,
    )


def make_engine(
    script: list[ScriptedResponse],
    tools: list[ToolDefinition],
    session: MemorySession | None = None,
) -> tuple[QueryEngine, MockProvider, MemorySession]:
    provider = MockProvider(script)
    registry = ToolRegistry()
    for t in tools:
        registry.register(t)
    sess = session or MemorySession(new_session_id())
    engine = QueryEngine(
        provider,
        registry,
        QueryConfig(model="test-model"),
        session=sess,
    )
    return engine, provider, sess


# ---------------------------------------------------------------------------
# Pause path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_pauses_on_ask_user_interrupt() -> None:
    sess = MemorySession("s1")
    engine, _, _ = make_engine(
        script=[
            ScriptedResponse(
                tool_calls=[
                    ScriptedToolCall(
                        name="ask_user",
                        input={"question": "what color?"},
                        tool_id="c-ask",
                    )
                ],
                finish_reason="tool_use",
            )
        ],
        tools=[make_ask_user_tool(interrupt_id="int-A")],
        session=sess,
    )

    with pytest.raises(RunPaused) as excinfo:
        await engine.run([UserMessage(content="hi")])

    assert excinfo.value.session_id == "s1"
    assert excinfo.value.interrupt_id == "int-A"
    assert excinfo.value.kind == "ask_user"


@pytest.mark.asyncio
async def test_pause_persists_history_to_session() -> None:
    sess = MemorySession("s1")
    engine, _, _ = make_engine(
        script=[
            ScriptedResponse(
                tool_calls=[
                    ScriptedToolCall(
                        name="ask_user",
                        input={"question": "?"},
                        tool_id="c-ask",
                    )
                ],
                finish_reason="tool_use",
            )
        ],
        tools=[make_ask_user_tool()],
        session=sess,
    )

    with pytest.raises(RunPaused):
        await engine.run([UserMessage(content="hi")])

    items = await sess.get_items()
    # Should have the user message + assistant message (with tool call).
    assert len(items) == 2
    assert items[0].role == "user"
    assert items[1].role == "assistant"


@pytest.mark.asyncio
async def test_pause_persists_interrupt_with_payload() -> None:
    sess = MemorySession("s1")
    engine, _, _ = make_engine(
        script=[
            ScriptedResponse(
                tool_calls=[
                    ScriptedToolCall(
                        name="ask_user",
                        input={
                            "question": "what?",
                            "options": [{"label": "a"}, {"label": "b"}],
                        },
                        tool_id="c-ask",
                    )
                ],
                finish_reason="tool_use",
            )
        ],
        tools=[make_ask_user_tool(interrupt_id="int-X")],
        session=sess,
    )

    with pytest.raises(RunPaused):
        await engine.run([UserMessage(content="hi")])

    pending = await sess.pending_interrupts()
    assert len(pending) == 1
    rec = pending[0]
    assert rec.id == "int-X"
    assert rec.kind == "ask_user"
    assert rec.payload["data"]["question"] == "what?"
    assert rec.payload["tool_call"]["id"] == "c-ask"


@pytest.mark.asyncio
async def test_pause_captures_sibling_tool_results() -> None:
    """A normal tool + ask_user in the same turn → sibling result cached."""
    sess = MemorySession("s1")
    engine, _, _ = make_engine(
        script=[
            ScriptedResponse(
                tool_calls=[
                    ScriptedToolCall(
                        name="echo",
                        input={"text": "hello"},
                        tool_id="c-echo",
                    ),
                    ScriptedToolCall(
                        name="ask_user",
                        input={"question": "?"},
                        tool_id="c-ask",
                    ),
                ],
                finish_reason="tool_use",
            )
        ],
        tools=[make_echo_tool(), make_ask_user_tool(interrupt_id="int-S")],
        session=sess,
    )

    with pytest.raises(RunPaused):
        await engine.run([UserMessage(content="hi")])

    [rec] = await sess.pending_interrupts()
    sibling = rec.payload["sibling_results"]
    assert len(sibling) == 1
    assert sibling[0]["tool_call_id"] == "c-echo"
    assert sibling[0]["content"] == "echo:hello"


@pytest.mark.asyncio
async def test_run_without_session_rejects_interrupt() -> None:
    provider = MockProvider(
        [
            ScriptedResponse(
                tool_calls=[
                    ScriptedToolCall(name="ask_user", input={"question": "?"})
                ],
                finish_reason="tool_use",
            )
        ]
    )
    registry = ToolRegistry()
    registry.register(make_ask_user_tool())
    engine = QueryEngine(
        provider, registry, QueryConfig(model="test-model")
    )  # no session=

    with pytest.raises(RuntimeError, match="no Session"):
        await engine.run([UserMessage(content="hi")])


@pytest.mark.asyncio
async def test_multiple_interrupts_in_one_turn_raises() -> None:
    sess = MemorySession("s1")
    engine, _, _ = make_engine(
        script=[
            ScriptedResponse(
                tool_calls=[
                    ScriptedToolCall(
                        name="ask_user", input={"question": "1"}, tool_id="c1"
                    ),
                    ScriptedToolCall(
                        name="ask_user", input={"question": "2"}, tool_id="c2"
                    ),
                ],
                finish_reason="tool_use",
            )
        ],
        tools=[make_ask_user_tool()],
        session=sess,
    )

    with pytest.raises(MultipleInterruptsError):
        await engine.run([UserMessage(content="hi")])


# ---------------------------------------------------------------------------
# Resume path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_from_interrupt_continues_to_final_answer() -> None:
    sess = MemorySession("s1")
    engine, _, _ = make_engine(
        script=[
            # Turn 1: model asks user.
            ScriptedResponse(
                tool_calls=[
                    ScriptedToolCall(
                        name="ask_user",
                        input={"question": "what color?"},
                        tool_id="c-ask",
                    )
                ],
                finish_reason="tool_use",
            ),
            # Turn 2 (after resume): model answers.
            ScriptedResponse(
                text="Got it — going with blue.", finish_reason="end_turn"
            ),
        ],
        tools=[make_ask_user_tool(interrupt_id="int-R")],
        session=sess,
    )

    with pytest.raises(RunPaused) as excinfo:
        await engine.run([UserMessage(content="pick a color")])

    result = await engine.resume_from_interrupt(
        excinfo.value.interrupt_id,
        {"selected": ["blue"]},
    )

    assert "blue" in result.messages[-1].get_text()
    assert result.stop_reason == "completed"


@pytest.mark.asyncio
async def test_resume_unknown_interrupt_raises() -> None:
    sess = MemorySession("s1")
    engine, _, _ = make_engine(
        script=[ScriptedResponse(text="ok")],
        tools=[make_ask_user_tool()],
        session=sess,
    )

    with pytest.raises(ValueError, match="not found"):
        await engine.resume_from_interrupt("ghost", {"selected": []})


@pytest.mark.asyncio
async def test_resume_without_session_raises() -> None:
    provider = MockProvider([ScriptedResponse(text="ok")])
    registry = ToolRegistry()
    engine = QueryEngine(
        provider, registry, QueryConfig(model="test-model")
    )
    with pytest.raises(RuntimeError, match="requires a Session"):
        await engine.resume_from_interrupt("int-x", {})


@pytest.mark.asyncio
async def test_resume_replays_sibling_results_in_next_turn_input() -> None:
    """The model's next turn must see ALL tool_results, not just the answer."""
    sess = MemorySession("s1")
    engine, provider, _ = make_engine(
        script=[
            ScriptedResponse(
                tool_calls=[
                    ScriptedToolCall(
                        name="echo", input={"text": "hi"}, tool_id="c-echo"
                    ),
                    ScriptedToolCall(
                        name="ask_user",
                        input={"question": "?"},
                        tool_id="c-ask",
                    ),
                ],
                finish_reason="tool_use",
            ),
            ScriptedResponse(text="done", finish_reason="end_turn"),
        ],
        tools=[make_echo_tool(), make_ask_user_tool(interrupt_id="int-Z")],
        session=sess,
    )

    with pytest.raises(RunPaused):
        await engine.run([UserMessage(content="go")])

    await engine.resume_from_interrupt("int-Z", {"selected": ["x"]})

    # Provider was called twice; the second call's last user message must
    # carry tool_result blocks for BOTH c-echo and c-ask.
    assert provider.call_count == 2
    second_request = provider.requests[1]
    last_user = second_request.messages[-1]
    blocks = last_user.content
    assert isinstance(blocks, list)
    tool_use_ids = [b["tool_use_id"] for b in blocks]
    assert tool_use_ids == ["c-echo", "c-ask"]


# ---------------------------------------------------------------------------
# Back-compat: no-session, no-interrupt path still works
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_existing_no_session_flow_unchanged() -> None:
    provider = MockProvider(
        [ScriptedResponse(text="hi back", finish_reason="end_turn")]
    )
    registry = ToolRegistry()
    engine = QueryEngine(
        provider, registry, QueryConfig(model="test-model")
    )  # no session=

    result = await engine.run([UserMessage(content="hi")])
    assert "hi back" in result.messages[-1].get_text()
    assert result.stop_reason == "completed"
