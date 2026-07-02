"""Tests for the PromptSuggestion post-turn hook."""

from __future__ import annotations

import pytest

from anila_core.context.agent_context import (
    AgentContext,
    set_current_context,
)
from anila_core.engine.query_engine import TurnResult
from anila_core.models.message import AssistantMessage, Usage, UserMessage
from anila_core.post_turn.prompt_suggestion import (
    PromptSuggestion,
    _build_focus_window,
    _is_eligible,
    _parse_suggestions,
    make_prompt_suggestion_hook,
)
from anila_core.providers.mock import MockProvider, ScriptedResponse


# ---------------------------------------------------------------------------
# _parse_suggestions
# ---------------------------------------------------------------------------


def test_parse_suggestions_clean_array() -> None:
    out = _parse_suggestions('["a", "b", "c"]', limit=5)
    assert out == ["a", "b", "c"]


def test_parse_suggestions_extracts_array_from_prose() -> None:
    raw = 'Sure! Here are some: ["q1?", "q2?"] hope that helps.'
    assert _parse_suggestions(raw, limit=5) == ["q1?", "q2?"]


def test_parse_suggestions_truncates_to_limit() -> None:
    out = _parse_suggestions('["a","b","c","d","e"]', limit=2)
    assert out == ["a", "b"]


def test_parse_suggestions_drops_non_strings_and_blanks() -> None:
    out = _parse_suggestions('["ok", 42, "  ", "ok2"]', limit=5)
    assert out == ["ok", "ok2"]


def test_parse_suggestions_returns_empty_on_garbage() -> None:
    assert _parse_suggestions("no array here", limit=3) == []
    assert _parse_suggestions("[oops not json]", limit=3) == []
    assert _parse_suggestions('{"not":"a list"}', limit=3) == []


def test_parse_suggestions_empty_string() -> None:
    assert _parse_suggestions("", limit=3) == []


# ---------------------------------------------------------------------------
# _is_eligible
# ---------------------------------------------------------------------------


def _result(stop_reason: str = "completed", with_assistant: bool = True) -> TurnResult:
    msgs: list = [UserMessage(content="hi")]
    if with_assistant:
        msgs.append(AssistantMessage(content="hello"))
    return TurnResult(
        messages=msgs,
        total_usage=Usage(),
        turn_count=1,
        finish_reason=stop_reason,
        stop_reason=stop_reason,
    )


def test_eligible_for_completed_turn_with_assistant_reply() -> None:
    assert _is_eligible(_result()) is True


def test_not_eligible_when_paused() -> None:
    assert _is_eligible(_result(stop_reason="paused")) is False


def test_not_eligible_when_no_messages() -> None:
    r = TurnResult(
        messages=[],
        total_usage=Usage(),
        turn_count=0,
        finish_reason="completed",
        stop_reason="completed",
    )
    assert _is_eligible(r) is False


def test_not_eligible_when_last_message_is_user() -> None:
    r = _result(with_assistant=False)
    assert _is_eligible(r) is False


# ---------------------------------------------------------------------------
# _build_focus_window
# ---------------------------------------------------------------------------


def test_focus_window_uses_last_n_messages() -> None:
    history = [
        UserMessage(content=f"msg {i}") for i in range(10)
    ]
    text = _build_focus_window(history, window=3)
    assert "msg 7" in text and "msg 8" in text and "msg 9" in text
    assert "msg 0" not in text


def test_focus_window_skips_blank_content() -> None:
    history = [
        UserMessage(content="real"),
        AssistantMessage(content=""),
        UserMessage(content="another"),
    ]
    text = _build_focus_window(history, window=10)
    assert "real" in text and "another" in text


# ---------------------------------------------------------------------------
# PromptSuggestion (with MockProvider)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_emits_follow_ups_via_event_emitter() -> None:
    captured: list[tuple[str, dict]] = []

    async def emitter(name: str, payload: dict) -> None:
        captured.append((name, payload))

    ctx = AgentContext(event_emitter=emitter)
    set_current_context(ctx)

    provider = MockProvider(
        [
            ScriptedResponse(
                text='["What about errors?", "Can you show tests?", "Edge cases?"]'
            )
        ]
    )
    suggester = PromptSuggestion(provider=provider, model="m")
    await suggester(_result())

    assert len(captured) == 1
    name, payload = captured[0]
    assert name == "follow_ups"
    assert payload["suggestions"] == [
        "What about errors?",
        "Can you show tests?",
        "Edge cases?",
    ]


@pytest.mark.asyncio
async def test_skips_when_paused() -> None:
    captured: list[tuple[str, dict]] = []

    async def emitter(name: str, payload: dict) -> None:
        captured.append((name, payload))

    ctx = AgentContext(event_emitter=emitter)
    set_current_context(ctx)
    provider = MockProvider([ScriptedResponse(text='["x"]')])
    suggester = PromptSuggestion(provider=provider, model="m")
    await suggester(_result(stop_reason="paused"))

    assert captured == []
    # And the provider was never called either.
    assert provider.call_count == 0


@pytest.mark.asyncio
async def test_swallows_provider_errors() -> None:
    captured: list[tuple[str, dict]] = []

    async def emitter(name: str, payload: dict) -> None:
        captured.append((name, payload))

    ctx = AgentContext(event_emitter=emitter)
    set_current_context(ctx)

    provider = MockProvider(
        [ScriptedResponse(raise_error=RuntimeError("boom"))]
    )
    suggester = PromptSuggestion(provider=provider, model="m")
    # Must not raise.
    await suggester(_result())
    assert captured == []


@pytest.mark.asyncio
async def test_no_event_emitter_silent_pass() -> None:
    """Without an emitter we still call the model but don't crash."""
    ctx = AgentContext()  # no emitter
    set_current_context(ctx)
    provider = MockProvider([ScriptedResponse(text='["a"]')])
    suggester = PromptSuggestion(provider=provider, model="m")
    await suggester(_result())
    assert provider.call_count == 1


@pytest.mark.asyncio
async def test_factory_returns_callable_hook() -> None:
    captured: list[tuple[str, dict]] = []

    async def emitter(name: str, payload: dict) -> None:
        captured.append((name, payload))

    ctx = AgentContext(event_emitter=emitter)
    set_current_context(ctx)

    provider = MockProvider(
        [ScriptedResponse(text='["one", "two", "three"]')]
    )
    hook = make_prompt_suggestion_hook(provider, model="m")
    await hook(_result())
    assert captured[0][1]["suggestions"] == ["one", "two", "three"]
