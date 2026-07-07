"""Tests for the self-rolled OpenAI-compatible provider adapter's SSE client.

Covers a prior review's confirmed deviations:

  * ``stream_completion`` didn't send ``stream_options: {include_usage: true}``
    on streaming requests, so usage never rides the last SSE chunk (csp's
    own proxy already forces this field — this adapter should match).
  * Turning that on means the server may send a *usage-only* chunk right
    before ``[DONE]`` — ``choices: []`` with only a ``usage`` field. Every
    ``choices[0]`` access must tolerate that shape.
  * The SSE line parser only recognised ``data: `` (space after colon);
    the SSE spec also allows ``data:`` with no space.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from anila_core.models.message import UserMessage
from anila_core.providers.base import ProviderRequest
from anila_core.providers.openai_compat import OpenAICompatProvider

BASE_URL = "http://fake-llm.test/v1"
CHAT_URL = f"{BASE_URL}/chat/completions"


def _sse_response(body: str) -> httpx.Response:
    return httpx.Response(
        200,
        content=body.encode("utf-8"),
        headers={"Content-Type": "text/event-stream"},
    )


def _request(*, stream: bool = True) -> ProviderRequest:
    return ProviderRequest(
        model="test-model",
        messages=[UserMessage(content="hello")],
        stream=stream,
    )


@pytest.mark.asyncio
@respx.mock
async def test_stream_request_body_includes_stream_options() -> None:
    """Streaming requests must ask the server to include usage in the SSE tail."""
    body = (
        'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
        "data: [DONE]\n\n"
    )
    route = respx.post(CHAT_URL).mock(return_value=_sse_response(body))

    provider = OpenAICompatProvider(base_url=BASE_URL)
    async for _ in provider.stream_completion(_request(stream=True)):
        pass

    sent = json.loads(route.calls.last.request.content)
    assert sent["stream"] is True
    assert sent["stream_options"] == {"include_usage": True}


@pytest.mark.asyncio
@respx.mock
async def test_non_streaming_request_omits_stream_options() -> None:
    """A non-streaming ProviderRequest must not carry stream_options."""
    body = (
        'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
        "data: [DONE]\n\n"
    )
    route = respx.post(CHAT_URL).mock(return_value=_sse_response(body))

    provider = OpenAICompatProvider(base_url=BASE_URL)
    async for _ in provider.stream_completion(_request(stream=False)):
        pass

    sent = json.loads(route.calls.last.request.content)
    assert sent["stream"] is False
    assert "stream_options" not in sent


@pytest.mark.asyncio
@respx.mock
async def test_usage_only_chunk_does_not_crash_and_usage_is_captured() -> None:
    """A trailing ``choices: []`` + ``usage`` chunk (enabled by include_usage)
    must not raise (no bare ``choices[0]``) and its usage must be read."""
    body = (
        'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
        'data: {"choices":[],"usage":{"prompt_tokens":11,"completion_tokens":22}}\n\n'
        "data: [DONE]\n\n"
    )
    respx.post(CHAT_URL).mock(return_value=_sse_response(body))

    provider = OpenAICompatProvider(base_url=BASE_URL)
    deltas = [d async for d in provider.stream_completion(_request(stream=True))]

    stop = deltas[-1]
    assert stop.type == "stop"
    assert stop.finish_reason == "stop"
    assert stop.usage is not None
    assert stop.usage.input_tokens == 11
    assert stop.usage.output_tokens == 22


@pytest.mark.asyncio
@respx.mock
async def test_data_line_without_space_after_colon_parses() -> None:
    """SSE spec: ``data:{...}`` (no space) must parse the same as ``data: {...}``."""
    body = (
        'data:{"choices":[{"delta":{"content":"no"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"space"}}]}\n\n'
        "data: [DONE]\n\n"
    )
    respx.post(CHAT_URL).mock(return_value=_sse_response(body))

    provider = OpenAICompatProvider(base_url=BASE_URL)
    deltas = [d async for d in provider.stream_completion(_request(stream=True))]

    text = "".join(d.text for d in deltas if d.type == "text" and d.text)
    assert text == "nospace"
