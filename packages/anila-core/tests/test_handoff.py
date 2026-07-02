"""Tests for the handoff primitive (Sprint 10 PR 1).

Covers:

- :class:`HandoffRequest` model basics
- Built-in filters (:class:`NoFilter`, :class:`LastNFilter`, :class:`SummaryFilter`)
- Filter helpers (:func:`_to_dict`, :func:`_is_visible_turn`)
- :class:`RunHandoff` exception shape
- QueryEngine integration: tool returns HandoffRequest → RunHandoff raised
"""

from __future__ import annotations

import pytest

from anila_core.engine.handoff import (
    HandoffFilter,
    LastNFilter,
    NoFilter,
    RunHandoff,
    SummaryFilter,
    _is_visible_turn,
    _to_dict,
)
from anila_core.engine.query_engine import QueryConfig, QueryEngine
from anila_core.memory import MemorySession
from anila_core.models.handoff import HandoffRequest
from anila_core.models.message import AssistantMessage, UserMessage
from anila_core.models.tool import ToolDefinition, ToolSafety
from anila_core.providers.mock import (
    MockProvider,
    ScriptedResponse,
    ScriptedToolCall,
)
from anila_core.router.tool_router import ToolRegistry


# ---------------------------------------------------------------------------
# HandoffRequest model
# ---------------------------------------------------------------------------


def test_handoff_request_auto_ids_unique() -> None:
    seen = {
        HandoffRequest(target_agent_id="x", message="m").id for _ in range(50)
    }
    assert len(seen) == 50


def test_handoff_request_id_has_hand_prefix() -> None:
    req = HandoffRequest(target_agent_id="x", message="m")
    assert req.id.startswith("hand-")


def test_handoff_request_is_immutable() -> None:
    req = HandoffRequest(target_agent_id="x", message="m")
    with pytest.raises(Exception):
        req.message = "y"  # type: ignore[misc]


def test_handoff_request_defaults() -> None:
    req = HandoffRequest(target_agent_id="agent-b", message="please answer")
    assert req.context_messages == []
    assert req.reason is None
    assert req.metadata == {}


# ---------------------------------------------------------------------------
# NoFilter
# ---------------------------------------------------------------------------


def test_no_filter_passes_all_visible_turns() -> None:
    history = [
        UserMessage(content="hi"),
        AssistantMessage(content="hello"),
        UserMessage(content="how are you"),
    ]
    out = NoFilter()(history)
    assert [m["role"] for m in out] == ["user", "assistant", "user"]
    assert [m["content"] for m in out] == ["hi", "hello", "how are you"]


def test_no_filter_drops_assistant_tool_calls() -> None:
    """tool_calls metadata is dropped — the target has its own registry."""
    history = [
        AssistantMessage(
            content=[{"type": "text", "text": "calling X"}],
            tool_calls=[],  # tool_calls field intentionally not propagated
        ),
    ]
    out = NoFilter()(history)
    assert out == [{"role": "assistant", "content": "calling X"}]


# ---------------------------------------------------------------------------
# LastNFilter
# ---------------------------------------------------------------------------


def test_last_n_filter_keeps_last_n_visible_turns() -> None:
    history = [UserMessage(content=f"msg-{i}") for i in range(5)]
    out = LastNFilter(n=2)(history)
    assert [m["content"] for m in out] == ["msg-3", "msg-4"]


def test_last_n_filter_skips_tool_result_user_messages() -> None:
    history = [
        UserMessage(content="real user"),
        AssistantMessage(content="answer"),
        UserMessage(
            content=[
                {"type": "tool_result", "tool_use_id": "x", "content": "ok"}
            ]
        ),
        UserMessage(content="follow up"),
    ]
    out = LastNFilter(n=3)(history)
    # tool_result user msg should be skipped
    contents = [m["content"] for m in out]
    assert "follow up" in contents
    assert "answer" in contents
    assert "real user" in contents


def test_last_n_filter_n_zero_raises() -> None:
    with pytest.raises(ValueError):
        LastNFilter(n=0)


# ---------------------------------------------------------------------------
# SummaryFilter
# ---------------------------------------------------------------------------


def test_summary_filter_returns_one_assistant_message() -> None:
    out = SummaryFilter("user wants X")([])
    assert out == [
        {
            "role": "assistant",
            "content": "[handoff summary]\nuser wants X",
        }
    ]


def test_summary_filter_blank_summary_returns_empty() -> None:
    assert SummaryFilter("")([]) == []
    assert SummaryFilter("   ")([]) == []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_to_dict_user_assistant_round_trip() -> None:
    assert _to_dict(UserMessage(content="x")) == {"role": "user", "content": "x"}
    assert _to_dict(AssistantMessage(content="y")) == {
        "role": "assistant",
        "content": "y",
    }


def test_is_visible_turn_skips_tool_result_only_user_msg() -> None:
    msg = UserMessage(
        content=[
            {"type": "tool_result", "tool_use_id": "x", "content": "ok"}
        ]
    )
    assert _is_visible_turn(msg) is False


def test_is_visible_turn_keeps_real_user_msg() -> None:
    assert _is_visible_turn(UserMessage(content="real")) is True


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_built_in_filters_satisfy_protocol() -> None:
    assert isinstance(NoFilter(), HandoffFilter)
    assert isinstance(LastNFilter(n=3), HandoffFilter)
    assert isinstance(SummaryFilter("x"), HandoffFilter)


# ---------------------------------------------------------------------------
# RunHandoff exception
# ---------------------------------------------------------------------------


def test_run_handoff_carries_request_and_session_id() -> None:
    req = HandoffRequest(
        id="hand-1", target_agent_id="agent-b", message="please"
    )
    exc = RunHandoff(session_id="s1", request=req)
    assert exc.session_id == "s1"
    assert exc.request.target_agent_id == "agent-b"
    assert "agent-b" in str(exc)
    assert "hand-1" in str(exc)


# ---------------------------------------------------------------------------
# QueryEngine integration
# ---------------------------------------------------------------------------


def make_handoff_tool(target: str = "agent-b") -> ToolDefinition:
    """Tool that always returns a HandoffRequest."""

    async def impl(input: dict, **_):
        ctx_msgs = LastNFilter(n=2)(
            [UserMessage(content="prior 1"), UserMessage(content="prior 2")]
        )
        return HandoffRequest(
            target_agent_id=input.get("target", target),
            message=input.get("message", "please continue"),
            context_messages=ctx_msgs,
            reason=input.get("reason"),
        )

    return ToolDefinition(
        name="handoff_to",
        description="Transfer control to another agent.",
        input_schema={
            "type": "object",
            "properties": {
                "target": {"type": "string"},
                "message": {"type": "string"},
                "reason": {"type": "string"},
            },
        },
        safety=ToolSafety.READ_ONLY,
        implementation=impl,
    )


@pytest.mark.asyncio
async def test_engine_raises_run_handoff_when_tool_returns_handoff_request() -> None:
    sess = MemorySession("s1")
    provider = MockProvider(
        [
            ScriptedResponse(
                tool_calls=[
                    ScriptedToolCall(
                        name="handoff_to",
                        input={"target": "agent-b", "message": "do it"},
                        tool_id="c-h",
                    )
                ],
                finish_reason="tool_use",
            )
        ]
    )
    registry = ToolRegistry()
    registry.register(make_handoff_tool())
    engine = QueryEngine(
        provider, registry, QueryConfig(model="m"), session=sess
    )

    with pytest.raises(RunHandoff) as excinfo:
        await engine.run([UserMessage(content="hi")])

    assert excinfo.value.session_id == "s1"
    assert excinfo.value.request.target_agent_id == "agent-b"
    assert excinfo.value.request.message == "do it"


@pytest.mark.asyncio
async def test_engine_persists_history_before_handoff() -> None:
    sess = MemorySession("s1")
    provider = MockProvider(
        [
            ScriptedResponse(
                tool_calls=[
                    ScriptedToolCall(
                        name="handoff_to",
                        input={"target": "agent-b", "message": "x"},
                        tool_id="c-h",
                    )
                ],
                finish_reason="tool_use",
            )
        ]
    )
    registry = ToolRegistry()
    registry.register(make_handoff_tool())
    engine = QueryEngine(
        provider, registry, QueryConfig(model="m"), session=sess
    )

    with pytest.raises(RunHandoff):
        await engine.run([UserMessage(content="hi")])

    items = await sess.get_items()
    # User msg + assistant msg with tool call.
    assert len(items) == 2
    assert items[0].role == "user"
    assert items[1].role == "assistant"


@pytest.mark.asyncio
async def test_engine_handoff_without_session_still_raises() -> None:
    """No session → RunHandoff still raised, just no persistence."""
    provider = MockProvider(
        [
            ScriptedResponse(
                tool_calls=[
                    ScriptedToolCall(
                        name="handoff_to",
                        input={"target": "agent-b", "message": "x"},
                        tool_id="c-h",
                    )
                ],
                finish_reason="tool_use",
            )
        ]
    )
    registry = ToolRegistry()
    registry.register(make_handoff_tool())
    engine = QueryEngine(provider, registry, QueryConfig(model="m"))
    # No session= passed.

    with pytest.raises(RunHandoff) as excinfo:
        await engine.run([UserMessage(content="hi")])
    # session_id is empty when no session was bound.
    assert excinfo.value.session_id == ""
