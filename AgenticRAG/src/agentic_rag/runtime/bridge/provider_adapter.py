"""Adapter: AgenticRAG ``Provider`` → framework ``LLMProvider``.

The framework's ``LLMProvider.chat_completion`` returns a fully-assembled
``ChatCompletionResponse``. AgenticRAG's existing ``Provider.stream_completion``
yields ``StreamDelta`` events (text / tool_call / reasoning / stop). This
adapter aggregates the stream into a single response so the framework's
``Runner`` can drive AgenticRAG providers without any provider rewrite.

Why an adapter and not "just use framework's OpenAICompatProvider":

- The host application already configured an AgenticRAG ``Provider``
  with auth, retries, and connection pooling tuned to its deployment.
  Forcing a switch to the framework's provider in the same release as
  the runtime swap would multiply the failure surface.
- AgenticRAG's ``Provider`` subclasses bring vendor-specific quirks
  (NVIDIA NIM stop sequences, vLLM tool_choice handling). Sprint 2's
  Middleware framework will absorb most of those concerns; until then
  keeping the existing Provider adapter path lets us ship the runtime
  swap without breaking deployments.

Sprint 2 will likely deprecate this adapter once the framework's
``OpenAICompatProvider`` becomes the default and tracing/cost
attribution middleware land. For now it's the bridge.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

from agentic_rag.runtime.framework.exceptions import ModelBehaviorError
from agentic_rag.runtime.framework.items import (
    ChatCompletionResponse,
    FinishReason,
    Message as FwMessage,
    Role as FwRole,
    ToolCall as FwToolCall,
)
from agentic_rag.runtime.framework.tool import ToolDefinition as FwToolDefinition
from agentic_rag.runtime.framework.usage import (
    InputTokensDetails,
    OutputTokensDetails,
    RequestUsage,
    Usage as FwUsage,
)

from agentic_rag.models.message import (
    AssistantMessage,
    Message as RagMessage,
    UserMessage,
)
from agentic_rag.providers.base import Provider, ProviderRequest


# ── Adapter class ──────────────────────────────────────────────────────


class FrameworkProviderAdapter:
    """Wraps an AgenticRAG ``Provider`` to satisfy framework ``LLMProvider``.

    Construction is cheap — the wrapped provider is held by reference
    and called per-request. The adapter is stateless beyond that.

    The wrapped provider is responsible for its own stream framing;
    this adapter simply concatenates text deltas, gathers tool-call
    fragments by call id, and surfaces the final usage.
    """

    def __init__(
        self,
        provider: Provider,
        *,
        default_max_tokens: int = 4096,
        default_temperature: float = 0.0,
    ) -> None:
        self._provider = provider
        self._default_max_tokens = default_max_tokens
        self._default_temperature = default_temperature

    # ── framework LLMProvider Protocol ───────────────────────────────

    async def chat_completion(
        self,
        messages: list[FwMessage],
        tools: list[FwToolDefinition] | None = None,
        *,
        model: str,
        stream: bool = False,
        **kwargs: Any,
    ) -> ChatCompletionResponse | AsyncIterator[Any]:
        """Aggregate a single completion and return ``ChatCompletionResponse``.

        ``stream=True`` is accepted but currently aggregates internally
        and still returns the assembled response. True streaming
        through the adapter lands when Sprint 2 wires SSE through
        Middleware; for now the Runner only consumes the unary path so
        re-aggregation is invisible.
        """
        # Pull the framework's system message out of the history; AgenticRAG
        # providers take ``system`` as a separate request field rather than
        # interleaving it into messages.
        system_text, rag_messages = _split_system_and_rag_messages(messages)

        request = ProviderRequest(
            model=model,
            system=system_text,
            messages=rag_messages,
            tools=[t.to_openai_dict() for t in (tools or [])],
            max_tokens=int(kwargs.pop("max_tokens", self._default_max_tokens)),
            temperature=float(kwargs.pop("temperature", self._default_temperature)),
            stream=True,
            extra=dict(kwargs),
        )

        text_buf: list[str] = []
        partial_calls: dict[str, dict[str, Any]] = {}
        finish_reason = FinishReason.STOP
        rag_usage = None

        try:
            async for delta in self._provider.stream_completion(request):
                if delta.type == "text" and delta.text:
                    text_buf.append(delta.text)
                elif delta.type == "tool_call" and delta.tool_call:
                    tc = delta.tool_call
                    bucket = partial_calls.setdefault(
                        tc.id,
                        {"id": tc.id, "name": tc.name, "input_raw": ""},
                    )
                    bucket["input_raw"] += tc.input_partial
                    # Model occasionally emits the name on a later delta;
                    # let it overwrite if non-empty.
                    if tc.name:
                        bucket["name"] = tc.name
                elif delta.type == "stop":
                    if delta.finish_reason:
                        finish_reason = _map_finish_reason(delta.finish_reason)
                    if delta.usage:
                        rag_usage = delta.usage
        except Exception as exc:  # noqa: BLE001
            raise ModelBehaviorError(
                f"Provider stream failed: {type(exc).__name__}: {exc}"
            ) from exc

        # Build the framework ToolCall list. ``arguments`` is the raw
        # JSON string the model produced; framework parses on demand
        # via ``ToolCall.parsed_arguments()`` so we don't pre-parse here.
        tool_calls = tuple(
            FwToolCall(
                id=data["id"] or f"call_{uuid.uuid4().hex[:12]}",
                name=data["name"],
                arguments=data["input_raw"] or "{}",
            )
            for data in partial_calls.values()
        )

        # If the LLM emitted tool_calls, ``finish_reason`` should be
        # ``tool_calls``. Some providers report ``stop`` even when tool
        # calls are present; normalise so the runner's contract holds.
        if tool_calls and finish_reason is not FinishReason.TOOL_CALLS:
            finish_reason = FinishReason.TOOL_CALLS

        message = FwMessage.assistant(
            content="".join(text_buf), tool_calls=tool_calls
        )

        usage = _convert_usage(rag_usage)

        return ChatCompletionResponse(
            message=message,
            usage=usage,
            finish_reason=finish_reason,
            raw=None,
        )

    async def embeddings(
        self,
        texts: list[str],
        *,
        model: str,
        **kwargs: Any,
    ) -> list[list[float]]:
        """Not implemented — AgenticRAG configures embedding providers
        separately from chat providers, so an embedding call here would
        likely point at the wrong endpoint. Use the host's
        ``embedding_provider`` directly."""
        raise NotImplementedError(
            "FrameworkProviderAdapter does not proxy embeddings. Configure "
            "the embedding provider directly on the agent_builder."
        )


# ── Conversion helpers ─────────────────────────────────────────────────


def _split_system_and_rag_messages(
    messages: list[FwMessage],
) -> tuple[str, list[RagMessage]]:
    """Pull system/developer messages out as a single string; convert the
    rest to AgenticRAG ``UserMessage`` / ``AssistantMessage``."""
    system_parts: list[str] = []
    rag_messages: list[RagMessage] = []

    for msg in messages:
        if msg.role in (FwRole.SYSTEM, FwRole.DEVELOPER):
            text = _content_as_text(msg.content)
            if text:
                system_parts.append(text)
            continue
        rag_messages.append(_framework_to_rag_message(msg))

    return "\n\n".join(system_parts), rag_messages


def _framework_to_rag_message(msg: FwMessage) -> RagMessage:
    """Convert one framework message to AgenticRAG's shape.

    Tool-role messages are folded into a UserMessage carrying a
    ``tool_result`` content block — that's the shape AgenticRAG's
    QueryEngine emits for tool results today, so providers built for
    AgenticRAG already know how to render it.
    """
    if msg.role is FwRole.USER:
        return UserMessage(content=_content_as_text(msg.content) or "")
    if msg.role is FwRole.ASSISTANT:
        # AgenticRAG AssistantMessage uses dict tool_calls; framework
        # uses ToolCall dataclasses. Convert.
        from agentic_rag.models.message import ToolCall as RagToolCall

        rag_tool_calls = []
        for tc in msg.tool_calls:
            try:
                parsed = json.loads(tc.arguments) if tc.arguments else {}
            except json.JSONDecodeError:
                parsed = {}
            rag_tool_calls.append(
                RagToolCall(id=tc.id, name=tc.name, input=parsed)
            )
        return AssistantMessage(
            content=_content_as_text(msg.content) or "",
            tool_calls=rag_tool_calls,
        )
    if msg.role is FwRole.TOOL:
        # AgenticRAG renders tool results as a UserMessage containing a
        # tool_result content block — see QueryEngine._build_tool_result_message.
        block = {
            "type": "tool_result",
            "tool_use_id": msg.tool_call_id,
            "content": _content_as_text(msg.content) or "",
        }
        return UserMessage(content=[block])
    raise ModelBehaviorError(f"Cannot convert framework message with role={msg.role}")


def _content_as_text(content: Any) -> str:
    """Flatten framework Message content to a string.

    Multi-modal content parts (image_url) are dropped — AgenticRAG's
    chat path is text-only for now. Image RAG ingestion lives in a
    separate pipeline.
    """
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for p in content:
        text = getattr(p, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts)


def _convert_usage(rag_usage: Any) -> FwUsage:
    """Convert AgenticRAG ``Usage`` → framework ``Usage`` (single request)."""
    if rag_usage is None:
        return FwUsage()
    input_tokens = int(getattr(rag_usage, "input_tokens", 0) or 0)
    output_tokens = int(getattr(rag_usage, "output_tokens", 0) or 0)
    cached = int(getattr(rag_usage, "cache_read_tokens", 0) or 0)
    total = input_tokens + output_tokens
    entry = RequestUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total,
        input_tokens_details=InputTokensDetails(cached_tokens=cached),
        output_tokens_details=OutputTokensDetails(reasoning_tokens=0),
    )
    return FwUsage(
        requests=1,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total,
        input_tokens_details=InputTokensDetails(cached_tokens=cached),
        output_tokens_details=OutputTokensDetails(reasoning_tokens=0),
        request_usage_entries=[entry],
    )


def _map_finish_reason(rag_reason: str) -> FinishReason:
    """Map AgenticRAG finish_reason strings to framework enum.

    AgenticRAG providers commonly use ``end_turn`` (Anthropic-style) and
    ``stop`` (OpenAI-style) interchangeably; both map to ``STOP``.
    """
    mapping = {
        "stop": FinishReason.STOP,
        "end_turn": FinishReason.STOP,
        "length": FinishReason.LENGTH,
        "max_tokens": FinishReason.LENGTH,
        "tool_use": FinishReason.TOOL_CALLS,
        "tool_calls": FinishReason.TOOL_CALLS,
        "content_filter": FinishReason.CONTENT_FILTER,
    }
    return mapping.get(rag_reason, FinishReason.OTHER)


__all__ = ["FrameworkProviderAdapter"]
