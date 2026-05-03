"""Tests for OpenAICompatProvider conversion paths.

We do not hit a real Chat Completions endpoint. Instead we inject a
stub AsyncOpenAI-shaped client that records what params were sent and
returns canned responses. This validates the Message ↔ OpenAI dict
mapping plus the response → ChatCompletionResponse mapping in
isolation.

These tests are skipped when the ``openai`` package isn't installed
(framework supports that case — see test_stage_b for the import-safety
test).
"""

from __future__ import annotations

import pytest

pytest.importorskip("openai")  # noqa: E402

from types import SimpleNamespace

from agentic_rag.runtime.framework.items import (
    FinishReason,
    ImageURLContent,
    Message,
    Role,
    TextContent,
    ToolCall,
)
from agentic_rag.runtime.framework.providers.openai_compat import (
    ChatCompletionChunk,
    OpenAICompatProvider,
    _finish_reason,
    _message_to_openai,
    _openai_tool_calls,
    _usage_from_openai,
)
from agentic_rag.runtime.framework.tool import ToolDefinition


# ── Stub client ─────────────────────────────────────────────────────────


class StubChatCompletions:
    def __init__(self, scripted_response):
        self._scripted = scripted_response
        self.last_params: dict = {}

    async def create(self, **params):
        self.last_params = params
        return self._scripted


class StubChat:
    def __init__(self, scripted_response):
        self.completions = StubChatCompletions(scripted_response)


class StubEmbeddings:
    def __init__(self, vectors):
        self._vectors = vectors

    async def create(self, *, model, input, **kwargs):  # noqa: ARG002, A002
        data = [SimpleNamespace(embedding=v) for v in self._vectors]
        return SimpleNamespace(data=data)


class StubAsyncOpenAI:
    """Minimal AsyncOpenAI duck-type for unit tests."""

    def __init__(self, *, scripted_response=None, vectors=None):
        self.chat = StubChat(scripted_response)
        self.embeddings = StubEmbeddings(vectors or [])


def _scripted_completion(
    *,
    text="hi",
    tool_calls=(),
    finish="stop",
    prompt_tokens=10,
    completion_tokens=5,
):
    """Build a SimpleNamespace mimicking openai's ChatCompletion shape."""
    msg = SimpleNamespace(
        content=text,
        tool_calls=list(tool_calls) or None,
        refusal=None,
    )
    choice = SimpleNamespace(message=msg, finish_reason=finish, index=0)
    usage = SimpleNamespace(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
        prompt_tokens_details=SimpleNamespace(cached_tokens=0),
        completion_tokens_details=SimpleNamespace(reasoning_tokens=0),
    )
    return SimpleNamespace(
        choices=[choice],
        usage=usage,
        model_dump=lambda: {"choices": [{"finish_reason": finish}]},
    )


# ── Conversion: Message → OpenAI dict ───────────────────────────────────


def test_message_to_openai_user_string() -> None:
    msg = Message.user("hello")
    d = _message_to_openai(msg)
    assert d == {"role": "user", "content": "hello"}


def test_message_to_openai_user_multimodal_parts() -> None:
    msg = Message.user(
        (TextContent(text="describe"), ImageURLContent(url="https://x/y.png")),
    )
    d = _message_to_openai(msg)
    assert d["role"] == "user"
    assert d["content"][0] == {"type": "text", "text": "describe"}
    assert d["content"][1]["type"] == "image_url"
    assert d["content"][1]["image_url"]["url"] == "https://x/y.png"


def test_message_to_openai_assistant_with_tool_calls() -> None:
    tc = ToolCall(id="call_1", name="search", arguments='{"q":"x"}')
    msg = Message.assistant(content="", tool_calls=(tc,))
    d = _message_to_openai(msg)
    assert d["role"] == "assistant"
    assert d["tool_calls"][0]["id"] == "call_1"
    assert d["tool_calls"][0]["function"]["name"] == "search"
    assert d["tool_calls"][0]["function"]["arguments"] == '{"q":"x"}'


def test_message_to_openai_tool_role_carries_call_id() -> None:
    msg = Message.tool(call_id="call_1", name="search", content="result text")
    d = _message_to_openai(msg)
    assert d == {
        "role": "tool",
        "content": "result text",
        "tool_call_id": "call_1",
        "name": "search",
    }


# ── Conversion: OpenAI response → ChatCompletionResponse ────────────────


def test_openai_tool_calls_convert_to_our_shape() -> None:
    raw = [
        SimpleNamespace(
            id="call_99",
            type="function",
            function=SimpleNamespace(name="search", arguments='{"q":"x"}'),
        )
    ]
    tcs = _openai_tool_calls(raw)
    assert tcs[0].id == "call_99"
    assert tcs[0].name == "search"
    assert tcs[0].arguments == '{"q":"x"}'


def test_openai_tool_calls_normalize_dict_arguments() -> None:
    """Some providers return dict instead of JSON string — normalise it."""
    raw = [
        SimpleNamespace(
            id="c1",
            type="function",
            function=SimpleNamespace(name="f", arguments={"q": "x"}),
        )
    ]
    tcs = _openai_tool_calls(raw)
    # Should be a JSON string, not a dict
    assert isinstance(tcs[0].arguments, str)


def test_usage_from_openai_populates_request_entry() -> None:
    raw_usage = SimpleNamespace(
        prompt_tokens=100,
        completion_tokens=30,
        total_tokens=130,
        prompt_tokens_details=SimpleNamespace(cached_tokens=20),
        completion_tokens_details=SimpleNamespace(reasoning_tokens=5),
    )
    usage = _usage_from_openai(raw_usage)
    assert usage.requests == 1
    assert usage.input_tokens == 100
    assert usage.output_tokens == 30
    assert usage.input_tokens_details.cached_tokens == 20
    assert usage.output_tokens_details.reasoning_tokens == 5
    assert len(usage.request_usage_entries) == 1


def test_finish_reason_normalises_unknown_to_other() -> None:
    assert _finish_reason("stop") is FinishReason.STOP
    assert _finish_reason("tool_calls") is FinishReason.TOOL_CALLS
    assert _finish_reason("weird-vendor-thing") is FinishReason.OTHER
    assert _finish_reason(None) is FinishReason.OTHER


# ── End-to-end with stub client ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_provider_chat_completion_unary_through_stub() -> None:
    stub = StubAsyncOpenAI(scripted_response=_scripted_completion(text="hello"))
    provider = OpenAICompatProvider(default_model="m", client=stub)

    response = await provider.chat_completion(
        messages=[Message.user("hi")], model="m"
    )

    assert response.message.role is Role.ASSISTANT
    assert response.message.content == "hello"
    assert response.finish_reason is FinishReason.STOP
    assert response.usage.input_tokens == 10
    assert response.usage.output_tokens == 5

    # The stub recorded what the provider sent
    sent = stub.chat.completions.last_params
    assert sent["model"] == "m"
    assert sent["messages"][0] == {"role": "user", "content": "hi"}


@pytest.mark.asyncio
async def test_provider_passes_tools_when_supplied() -> None:
    stub = StubAsyncOpenAI(scripted_response=_scripted_completion())
    provider = OpenAICompatProvider(default_model="m", client=stub)

    td = ToolDefinition(name="search", description="x", parameters={"type": "object"})
    await provider.chat_completion(
        messages=[Message.user("go")], tools=[td], model="m"
    )

    sent = stub.chat.completions.last_params
    assert sent["tools"][0]["function"]["name"] == "search"


@pytest.mark.asyncio
async def test_provider_chat_completion_returns_tool_calls() -> None:
    """Provider correctly surfaces tool_calls from the wire response."""
    raw_tc = SimpleNamespace(
        id="call_1",
        type="function",
        function=SimpleNamespace(name="search", arguments='{"q":"x"}'),
    )
    stub = StubAsyncOpenAI(
        scripted_response=_scripted_completion(
            text="", tool_calls=(raw_tc,), finish="tool_calls"
        )
    )
    provider = OpenAICompatProvider(default_model="m", client=stub)

    response = await provider.chat_completion(messages=[Message.user("go")], model="m")
    assert response.finish_reason is FinishReason.TOOL_CALLS
    assert len(response.message.tool_calls) == 1
    assert response.message.tool_calls[0].name == "search"


@pytest.mark.asyncio
async def test_provider_embeddings_returns_vectors_in_order() -> None:
    stub = StubAsyncOpenAI(vectors=[[1.0, 2.0], [3.0, 4.0]])
    provider = OpenAICompatProvider(default_model="m", client=stub)

    vectors = await provider.embeddings(["a", "b"], model="emb")
    assert vectors == [[1.0, 2.0], [3.0, 4.0]]


@pytest.mark.asyncio
async def test_provider_requires_model() -> None:
    stub = StubAsyncOpenAI(scripted_response=_scripted_completion())
    provider = OpenAICompatProvider(client=stub)  # no default_model

    with pytest.raises(Exception, match="model"):
        await provider.chat_completion(messages=[Message.user("hi")])  # type: ignore[arg-type]


# ── Streaming chunk conversion ──────────────────────────────────────────


def test_chat_completion_chunk_from_openai_extracts_text_delta() -> None:
    raw = SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content="hello", tool_calls=None),
                finish_reason=None,
            )
        ],
        usage=None,
    )
    chunk = ChatCompletionChunk.from_openai(raw)
    assert chunk.text_delta == "hello"
    assert chunk.tool_call_deltas == []
    assert chunk.finish_reason is None


def test_chat_completion_chunk_from_openai_extracts_tool_call_delta() -> None:
    raw = SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(
                    content=None,
                    tool_calls=[
                        SimpleNamespace(
                            index=0,
                            id="call_1",
                            function=SimpleNamespace(
                                name="search", arguments='{"q":'
                            ),
                        )
                    ],
                ),
                finish_reason=None,
            )
        ],
        usage=None,
    )
    chunk = ChatCompletionChunk.from_openai(raw)
    assert chunk.text_delta == ""
    assert chunk.tool_call_deltas[0]["id"] == "call_1"
    assert chunk.tool_call_deltas[0]["name"] == "search"
