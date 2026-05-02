"""OpenAI Chat-Completions-compatible provider.

One concrete implementation that covers everything that speaks
``POST /v1/chat/completions`` in the OpenAI dialect:

- OpenAI itself
- vLLM (``--api-server`` mode)
- NVIDIA NIM
- HuggingFace TGI (``--openai-api`` mode)
- Ollama (when run with the OpenAI-compat shim)
- LocalAI, llama.cpp server, etc.

How to use it:

    provider = OpenAICompatProvider(
        base_url="http://vllm:8000/v1",   # OpenAI uses the SDK default
        api_key="EMPTY",                   # vLLM ignores; OpenAI requires real key
        model="gemma-2-9b-it",
    )
    agent = Agent(name="rag-agent", instructions=..., actions=[...],
                  provider=provider, model="gemma-2-9b-it")
    result = await Runner().run(agent, "...")

Why we wrap the official ``openai`` SDK rather than a raw httpx call:

- The SDK already handles auth headers, retry on 429/503, JSON parsing,
  streaming framing. Re-implementing those is a tax we don't want to pay.
- The SDK is a soft dependency — the framework itself doesn't import
  ``openai`` at the top level. Users who never touch this provider don't
  need ``openai`` installed. The import is deferred to ``__init__``.

The provider raises a clean ``UserError`` if ``openai`` isn't installed,
pointing to the ``[openai]`` extra.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from agentic_rag.runtime.framework.exceptions import (
    ModelBehaviorError,
    ModelRefusalError,
    UserError,
)
from agentic_rag.runtime.framework.items import (
    ChatCompletionResponse,
    FinishReason,
    Message,
    Role,
    ToolCall,
)
from agentic_rag.runtime.framework.tool import ToolDefinition
from agentic_rag.runtime.framework.usage import (
    InputTokensDetails,
    OutputTokensDetails,
    RequestUsage,
    Usage,
)

if TYPE_CHECKING:
    # Type-only imports keep mypy happy without forcing the openai
    # dependency at module load.
    from openai import AsyncOpenAI


# ── Provider ────────────────────────────────────────────────────────────


class OpenAICompatProvider:
    """``LLMProvider`` impl backed by ``openai.AsyncOpenAI``.

    Construction validates that the ``openai`` package is importable
    and that ``base_url`` / ``api_key`` look usable. The actual HTTP
    client is created in __init__ and reused for the provider's
    lifetime — callers running many short-lived agents should share
    one provider instance.

    ``default_model`` is what's used when ``chat_completion`` doesn't
    receive an explicit ``model``; the runner always supplies one,
    but standalone use cases (one-shot completions outside the agent
    loop) appreciate a default.
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        default_model: str | None = None,
        timeout: float | None = 60.0,
        client: AsyncOpenAI | None = None,
    ) -> None:
        try:
            from openai import AsyncOpenAI as _AsyncOpenAI
        except ImportError as exc:
            raise UserError(
                "OpenAICompatProvider requires the 'openai' package. "
                "Install with: pip install 'agentic-rag[openai]'"
            ) from exc

        if client is not None:
            self._client = client
        else:
            # OpenAI's SDK requires an api_key string even for self-hosted
            # servers; vLLM / Ollama ignore the value but the SDK still
            # checks it's present. Pass "EMPTY" rather than None to keep
            # the SDK happy without lying about real credentials.
            effective_key = api_key or "EMPTY"
            self._client = _AsyncOpenAI(
                api_key=effective_key,
                base_url=base_url,
                timeout=timeout,
            )

        self._default_model = default_model
        self._base_url = base_url

    # ── Public API ───────────────────────────────────────────────────

    async def chat_completion(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        *,
        model: str | None = None,
        stream: bool = False,
        **kwargs: Any,
    ) -> ChatCompletionResponse | AsyncIterator[ChatCompletionChunk]:
        """Run one Chat Completions call.

        With ``stream=False`` returns a fully-assembled
        ``ChatCompletionResponse``. With ``stream=True`` returns an
        async iterator of ``ChatCompletionChunk`` objects; consumers
        are responsible for re-aggregating into a final response.

        ``kwargs`` flow through verbatim to the SDK call so consumers
        can pass provider-specific knobs (``response_format``, ``seed``,
        ``logit_bias``, etc.) without the provider class having to
        enumerate every one.
        """
        used_model = model or self._default_model
        if not used_model:
            raise UserError(
                "OpenAICompatProvider.chat_completion requires a model — "
                "either pass model= or set default_model= at construction."
            )

        wire_messages = [_message_to_openai(m) for m in messages]
        wire_tools = [t.to_openai_dict() for t in (tools or [])] or None

        if stream:
            return self._stream(used_model, wire_messages, wire_tools, kwargs)
        return await self._unary(used_model, wire_messages, wire_tools, kwargs)

    async def embeddings(
        self,
        texts: list[str],
        *,
        model: str,
        **kwargs: Any,
    ) -> list[list[float]]:
        """Compute embeddings via ``client.embeddings.create``.

        Returns one float vector per input text, in input order. Raises
        ``UserError`` if the underlying provider doesn't expose an
        embeddings endpoint (vLLM does not, by default).
        """
        if not texts:
            return []
        try:
            response = await self._client.embeddings.create(
                model=model, input=texts, **kwargs
            )
        except Exception as exc:  # noqa: BLE001
            raise UserError(
                f"Embeddings call failed against {self._base_url or 'openai default'}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        return [list(item.embedding) for item in response.data]

    # ── Unary path ───────────────────────────────────────────────────

    async def _unary(
        self,
        model: str,
        wire_messages: list[dict[str, Any]],
        wire_tools: list[dict[str, Any]] | None,
        extra_kwargs: dict[str, Any],
    ) -> ChatCompletionResponse:
        """Non-streaming completion + envelope conversion."""
        params: dict[str, Any] = {
            "model": model,
            "messages": wire_messages,
        }
        if wire_tools:
            params["tools"] = wire_tools
        params.update(extra_kwargs)

        try:
            raw = await self._client.chat.completions.create(**params)
        except Exception as exc:  # noqa: BLE001
            # ``openai.APIError`` and httpx errors all bubble through here.
            # Wrap as ModelBehaviorError so the runner's catch can see one
            # exception family (real auth/network errors should already
            # have been caught by the SDK's retry layer).
            raise ModelBehaviorError(
                f"chat.completions.create failed: {type(exc).__name__}: {exc}"
            ) from exc

        if not raw.choices:
            raise ModelBehaviorError("chat.completions returned no choices")

        choice = raw.choices[0]
        msg = choice.message

        if getattr(msg, "refusal", None):
            raise ModelRefusalError(msg.refusal)

        assistant = Message.assistant(
            content=msg.content or "",
            tool_calls=tuple(_openai_tool_calls(msg.tool_calls or [])),
        )
        usage = _usage_from_openai(raw.usage)
        finish = _finish_reason(choice.finish_reason)

        # ``raw`` is a pydantic-typed object; ``model_dump`` keeps it
        # serialisable for tracing without forcing tracing middleware to
        # know about the openai SDK.
        raw_payload = raw.model_dump() if hasattr(raw, "model_dump") else None

        return ChatCompletionResponse(
            message=assistant,
            usage=usage,
            finish_reason=finish,
            raw=raw_payload,
        )

    # ── Streaming path ───────────────────────────────────────────────

    async def _stream(
        self,
        model: str,
        wire_messages: list[dict[str, Any]],
        wire_tools: list[dict[str, Any]] | None,
        extra_kwargs: dict[str, Any],
    ) -> AsyncIterator[ChatCompletionChunk]:
        """Streaming completion as an async iterator of chunks.

        Stage B's runner only consumes the unary path; the streaming
        path is wired here so Sprint 2's TraceMiddleware / SSE adapter
        has something to call. Each chunk carries (a) any new text
        delta, (b) any new tool-call delta, (c) the cumulative
        finish_reason once known.
        """
        params: dict[str, Any] = {
            "model": model,
            "messages": wire_messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if wire_tools:
            params["tools"] = wire_tools
        params.update(extra_kwargs)

        try:
            stream = await self._client.chat.completions.create(**params)
        except Exception as exc:  # noqa: BLE001
            raise ModelBehaviorError(
                f"streaming chat.completions.create failed: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        async for raw_chunk in stream:
            yield ChatCompletionChunk.from_openai(raw_chunk)


# ── Streaming chunk type ────────────────────────────────────────────────


class ChatCompletionChunk:
    """One incremental piece of a streaming response.

    Lightweight on purpose — we expose only what middleware needs for
    SSE forwarding and tracing. Reassembling chunks back into a final
    ``ChatCompletionResponse`` is the consumer's job (Sprint 2's
    streaming runner will own that).

    ``text_delta`` is the new chunk of assistant text (empty string if
    this chunk only carried tool-call deltas).
    ``tool_call_deltas`` is a list of partial tool calls to merge into
    the in-progress assistant message.
    ``finish_reason`` is set on the final chunk.
    ``usage`` is set on the very last chunk when ``stream_options.
    include_usage`` was requested.
    """

    __slots__ = ("text_delta", "tool_call_deltas", "finish_reason", "usage", "raw")

    def __init__(
        self,
        *,
        text_delta: str = "",
        tool_call_deltas: list[dict[str, Any]] | None = None,
        finish_reason: FinishReason | None = None,
        usage: Usage | None = None,
        raw: Any = None,
    ) -> None:
        self.text_delta = text_delta
        self.tool_call_deltas = tool_call_deltas or []
        self.finish_reason = finish_reason
        self.usage = usage
        self.raw = raw

    @classmethod
    def from_openai(cls, raw_chunk: Any) -> ChatCompletionChunk:
        text = ""
        deltas: list[dict[str, Any]] = []
        finish: FinishReason | None = None

        choices = getattr(raw_chunk, "choices", None) or []
        if choices:
            choice = choices[0]
            delta = getattr(choice, "delta", None)
            if delta is not None:
                text = getattr(delta, "content", "") or ""
                tcs = getattr(delta, "tool_calls", None) or []
                for tc in tcs:
                    deltas.append(
                        {
                            "index": getattr(tc, "index", 0),
                            "id": getattr(tc, "id", None),
                            "name": getattr(getattr(tc, "function", None), "name", None),
                            "arguments": getattr(
                                getattr(tc, "function", None), "arguments", None
                            ),
                        }
                    )
            fr = getattr(choice, "finish_reason", None)
            if fr is not None:
                finish = _finish_reason(fr)

        usage_obj = getattr(raw_chunk, "usage", None)
        usage = _usage_from_openai(usage_obj) if usage_obj is not None else None

        return cls(
            text_delta=text,
            tool_call_deltas=deltas,
            finish_reason=finish,
            usage=usage,
            raw=raw_chunk,
        )


# ── Conversion helpers ─────────────────────────────────────────────────


def _message_to_openai(message: Message) -> dict[str, Any]:
    """Render a ``Message`` as the dict shape ``client.chat.completions``
    expects."""
    role = message.role.value
    payload: dict[str, Any] = {"role": role}

    if isinstance(message.content, str):
        # Tool-role messages carry their body as plain text in OpenAI's
        # shape; same for system/user/assistant when no multimodal parts.
        payload["content"] = message.content
    else:
        # Content parts → list-of-dicts shape OpenAI accepts on user/
        # assistant messages.
        payload["content"] = [_part_to_openai(p) for p in message.content]

    if message.role is Role.ASSISTANT and message.tool_calls:
        payload["tool_calls"] = [
            {
                "id": tc.id,
                "type": tc.type,
                "function": {"name": tc.name, "arguments": tc.arguments or "{}"},
            }
            for tc in message.tool_calls
        ]

    if message.role is Role.TOOL:
        if not message.tool_call_id:
            # Caught earlier in Message.__post_init__; guard for safety.
            raise UserError("Tool message missing tool_call_id")
        payload["tool_call_id"] = message.tool_call_id
        if message.name:
            payload["name"] = message.name

    if message.name and message.role is not Role.TOOL:
        payload["name"] = message.name

    return payload


def _part_to_openai(part: Any) -> dict[str, Any]:
    """Render a content part (text / image_url / refusal) as OpenAI dict."""
    type_ = getattr(part, "type", None)
    if type_ == "text":
        return {"type": "text", "text": part.text}
    if type_ == "image_url":
        return {
            "type": "image_url",
            "image_url": {"url": part.url, "detail": getattr(part, "detail", "auto")},
        }
    if type_ == "refusal":
        return {"type": "refusal", "refusal": part.refusal}
    raise UserError(f"Unsupported content part type: {type_!r}")


def _openai_tool_calls(raw_tool_calls: list[Any]) -> list[ToolCall]:
    """Convert OpenAI ``ChatCompletionMessageToolCall`` objects to ours."""
    out: list[ToolCall] = []
    for tc in raw_tool_calls:
        fn = getattr(tc, "function", None)
        if fn is None:
            continue
        name = getattr(fn, "name", None) or ""
        args = getattr(fn, "arguments", None) or "{}"
        if not isinstance(args, str):
            # Some providers return already-parsed dicts; normalise to
            # the JSON-string contract our ToolCall expects.
            args = json.dumps(args)
        out.append(
            ToolCall(
                id=getattr(tc, "id", "") or "",
                name=name,
                arguments=args,
                type=getattr(tc, "type", "function") or "function",
            )
        )
    return out


def _usage_from_openai(raw_usage: Any) -> Usage:
    """Map OpenAI's usage block into our ``Usage`` shape (single request)."""
    if raw_usage is None:
        return Usage()
    input_tokens = getattr(raw_usage, "prompt_tokens", 0) or 0
    output_tokens = getattr(raw_usage, "completion_tokens", 0) or 0
    total_tokens = getattr(raw_usage, "total_tokens", 0) or (
        input_tokens + output_tokens
    )

    cached = 0
    pti = getattr(raw_usage, "prompt_tokens_details", None)
    if pti is not None:
        cached = getattr(pti, "cached_tokens", 0) or 0

    reasoning = 0
    cti = getattr(raw_usage, "completion_tokens_details", None)
    if cti is not None:
        reasoning = getattr(cti, "reasoning_tokens", 0) or 0

    entry = RequestUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        input_tokens_details=InputTokensDetails(cached_tokens=cached),
        output_tokens_details=OutputTokensDetails(reasoning_tokens=reasoning),
    )

    return Usage(
        requests=1,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        input_tokens_details=InputTokensDetails(cached_tokens=cached),
        output_tokens_details=OutputTokensDetails(reasoning_tokens=reasoning),
        request_usage_entries=[entry],
    )


def _finish_reason(raw: str | None) -> FinishReason:
    """Normalise the wire string into our enum.

    Unknown values fall through to ``OTHER`` so weird providers don't
    crash the runner.
    """
    if not raw:
        return FinishReason.OTHER
    try:
        return FinishReason(raw)
    except ValueError:
        return FinishReason.OTHER


# ── Public surface ──────────────────────────────────────────────────────


__all__ = ["ChatCompletionChunk", "OpenAICompatProvider"]
