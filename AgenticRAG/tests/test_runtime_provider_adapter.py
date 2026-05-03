"""Tests for FrameworkProviderAdapter — AgenticRAG Provider → framework LLMProvider."""

from __future__ import annotations

from typing import AsyncIterator

import pytest

from agentic_rag.runtime.framework.items import (
    ChatCompletionResponse,
    FinishReason,
    Message as FwMessage,
    Role as FwRole,
    ToolCall as FwToolCall,
)
from agentic_rag.runtime.framework.tool import ToolDefinition as FwToolDefinition

from agentic_rag.models.message import (
    StreamDelta,
    ToolCallDelta,
    Usage as RagUsage,
)
from agentic_rag.providers.base import ProviderRequest
from agentic_rag.runtime.bridge.provider_adapter import (
    FrameworkProviderAdapter,
    _content_as_text,
    _convert_usage,
    _framework_to_rag_message,
    _map_finish_reason,
    _split_system_and_rag_messages,
)


class _ScriptedProvider:
    """Records the request it received and replays a scripted delta stream."""

    def __init__(self, deltas: list[StreamDelta]) -> None:
        self._deltas = deltas
        self.last_request: ProviderRequest | None = None

    async def stream_completion(
        self, request: ProviderRequest
    ) -> AsyncIterator[StreamDelta]:
        self.last_request = request
        for d in self._deltas:
            yield d


# ── Conversion: framework → AgenticRAG ─────────────────────────────────


def test_split_system_pulls_system_messages_into_string() -> None:
    msgs = [
        FwMessage.system("you are helpful"),
        FwMessage.user("hi"),
    ]
    system, rag = _split_system_and_rag_messages(msgs)
    assert system == "you are helpful"
    assert len(rag) == 1
    assert rag[0].role == "user"


def test_split_system_concatenates_multiple_system_messages() -> None:
    msgs = [
        FwMessage.system("rule 1"),
        FwMessage.developer("rule 2"),
        FwMessage.user("hi"),
    ]
    system, rag = _split_system_and_rag_messages(msgs)
    assert system == "rule 1\n\nrule 2"
    assert len(rag) == 1


def test_assistant_with_tool_calls_round_trips() -> None:
    fw_msg = FwMessage.assistant(
        content="",
        tool_calls=(FwToolCall(id="c1", name="search", arguments='{"q":"x"}'),),
    )
    rag = _framework_to_rag_message(fw_msg)
    assert rag.role == "assistant"
    assert len(rag.tool_calls) == 1
    assert rag.tool_calls[0].name == "search"
    assert rag.tool_calls[0].input == {"q": "x"}


def test_tool_role_message_becomes_user_with_tool_result_block() -> None:
    fw_msg = FwMessage.tool(call_id="c1", name="search", content="result text")
    rag = _framework_to_rag_message(fw_msg)
    assert rag.role == "user"
    assert isinstance(rag.content, list)
    assert rag.content[0]["type"] == "tool_result"
    assert rag.content[0]["tool_use_id"] == "c1"
    assert rag.content[0]["content"] == "result text"


def test_content_as_text_handles_multimodal_parts() -> None:
    from agentic_rag.runtime.framework.items import ImageURLContent, TextContent

    text = _content_as_text(
        (TextContent(text="hello "), ImageURLContent(url="x"), TextContent(text="world"))
    )
    assert text == "hello world"


# ── Usage / finish reason mapping ──────────────────────────────────────


def test_convert_usage_populates_request_entry() -> None:
    rag_usage = RagUsage(
        input_tokens=100,
        output_tokens=30,
        cache_read_tokens=20,
    )
    fw_usage = _convert_usage(rag_usage)
    assert fw_usage.requests == 1
    assert fw_usage.input_tokens == 100
    assert fw_usage.output_tokens == 30
    assert fw_usage.input_tokens_details.cached_tokens == 20
    assert len(fw_usage.request_usage_entries) == 1


def test_convert_usage_handles_none() -> None:
    fw_usage = _convert_usage(None)
    assert fw_usage.requests == 0


def test_finish_reason_mapping() -> None:
    assert _map_finish_reason("stop") is FinishReason.STOP
    assert _map_finish_reason("end_turn") is FinishReason.STOP
    assert _map_finish_reason("tool_use") is FinishReason.TOOL_CALLS
    assert _map_finish_reason("tool_calls") is FinishReason.TOOL_CALLS
    assert _map_finish_reason("length") is FinishReason.LENGTH
    assert _map_finish_reason("unknown_thing") is FinishReason.OTHER


# ── End-to-end through the adapter ─────────────────────────────────────


@pytest.mark.asyncio
async def test_adapter_aggregates_text_stream_into_response() -> None:
    deltas = [
        StreamDelta(type="text", text="hello "),
        StreamDelta(type="text", text="world"),
        StreamDelta(
            type="stop",
            finish_reason="stop",
            usage=RagUsage(input_tokens=10, output_tokens=2),
        ),
    ]
    provider = _ScriptedProvider(deltas)
    adapter = FrameworkProviderAdapter(provider)

    response = await adapter.chat_completion(
        messages=[FwMessage.system("be brief"), FwMessage.user("greet me")],
        model="m",
    )

    assert isinstance(response, ChatCompletionResponse)
    assert response.message.role is FwRole.ASSISTANT
    assert response.message.content == "hello world"
    assert response.finish_reason is FinishReason.STOP
    assert response.usage.input_tokens == 10
    assert response.usage.output_tokens == 2

    # Adapter pulled system out of messages and into the request field
    assert provider.last_request is not None
    assert provider.last_request.system == "be brief"
    assert len(provider.last_request.messages) == 1
    assert provider.last_request.messages[0].role == "user"


@pytest.mark.asyncio
async def test_adapter_aggregates_tool_call_stream() -> None:
    deltas = [
        StreamDelta(
            type="tool_call",
            tool_call=ToolCallDelta(
                id="call_1",
                name="search",
                input_partial='{"q":"x"}',
            ),
        ),
        StreamDelta(
            type="stop",
            finish_reason="tool_calls",
            usage=RagUsage(input_tokens=20, output_tokens=5),
        ),
    ]
    adapter = FrameworkProviderAdapter(_ScriptedProvider(deltas))

    response = await adapter.chat_completion(
        messages=[FwMessage.user("look up x")], model="m"
    )

    assert response.finish_reason is FinishReason.TOOL_CALLS
    assert len(response.message.tool_calls) == 1
    assert response.message.tool_calls[0].name == "search"
    assert response.message.tool_calls[0].arguments == '{"q":"x"}'


@pytest.mark.asyncio
async def test_adapter_normalises_finish_when_tool_calls_present_but_stop_reported() -> None:
    """Some providers report finish_reason=stop even when tool_calls were emitted.
    Adapter must normalise to TOOL_CALLS so the runner doesn't terminate early."""
    deltas = [
        StreamDelta(
            type="tool_call",
            tool_call=ToolCallDelta(id="c1", name="t", input_partial="{}"),
        ),
        StreamDelta(type="stop", finish_reason="stop", usage=None),
    ]
    adapter = FrameworkProviderAdapter(_ScriptedProvider(deltas))

    response = await adapter.chat_completion(
        messages=[FwMessage.user("x")], model="m"
    )
    assert response.finish_reason is FinishReason.TOOL_CALLS


@pytest.mark.asyncio
async def test_adapter_concatenates_partial_tool_call_inputs() -> None:
    """Tool call arguments commonly arrive in multiple deltas; adapter
    must concat them into a single arguments string."""
    deltas = [
        StreamDelta(
            type="tool_call",
            tool_call=ToolCallDelta(id="c1", name="search", input_partial='{"q":'),
        ),
        StreamDelta(
            type="tool_call",
            tool_call=ToolCallDelta(id="c1", name="", input_partial='"hello"}'),
        ),
        StreamDelta(type="stop", finish_reason="tool_calls", usage=None),
    ]
    adapter = FrameworkProviderAdapter(_ScriptedProvider(deltas))

    response = await adapter.chat_completion(
        messages=[FwMessage.user("x")], model="m"
    )
    assert response.message.tool_calls[0].arguments == '{"q":"hello"}'


@pytest.mark.asyncio
async def test_adapter_passes_tools_through_request() -> None:
    adapter = FrameworkProviderAdapter(
        _ScriptedProvider([StreamDelta(type="stop", finish_reason="stop")])
    )
    td = FwToolDefinition(
        name="search", description="x", parameters={"type": "object"}
    )

    await adapter.chat_completion(
        messages=[FwMessage.user("hi")], tools=[td], model="m"
    )

    provider = adapter._provider  # type: ignore[attr-defined]
    assert provider.last_request.tools  # type: ignore[union-attr]
    assert provider.last_request.tools[0]["function"]["name"] == "search"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_adapter_embeddings_raises_not_implemented() -> None:
    adapter = FrameworkProviderAdapter(_ScriptedProvider([]))
    with pytest.raises(NotImplementedError):
        await adapter.embeddings(["x"], model="m")
