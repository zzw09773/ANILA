"""SDK-agnostic message / response / chunk schema for the runtime layer.

The shape mirrors the OpenAI chat-completion contract so that any backend
(openai-agents, Raw HTTP on vLLM, future frameworks) can be adapted into it
without losing fidelity. None of the types here reference openai-agents or any
other third-party SDK on purpose: keeping this layer pure means the
``ConnectionStrategy`` interface stays stable across backend implementations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Role = Literal["system", "user", "assistant", "tool"]


@dataclass(frozen=True)
class ToolCall:
    """One tool invocation requested by the assistant.

    Matches the OpenAI ``chat.completion.message.tool_calls[*]`` shape: an id,
    the tool name, and a JSON-encoded arguments string. Backends that surface
    arguments as a ``dict`` should ``json.dumps`` before populating ``arguments``
    so downstream consumers see a uniform shape.
    """

    id: str
    name: str
    arguments: str = ""


@dataclass(frozen=True)
class Usage:
    """Token accounting for a single ``send_message`` / ``stream_message`` call."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass(frozen=True)
class Message:
    """A single chat-completion-style message.

    ``content`` is intentionally optional: assistant turns that emit only tool
    calls carry ``content=None``. ``tool_call_id`` is populated on
    ``role="tool"`` messages so the backend can correlate the response with the
    originating ``ToolCall``.
    """

    role: Role
    content: str | None = None
    name: str | None = None
    tool_calls: tuple[ToolCall, ...] = field(default_factory=tuple)
    tool_call_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Response:
    """Full (non-streaming) backend response.

    ``raw`` keeps the original backend object available for diagnostics without
    leaking its type into the abstract interface. Callers that want to consume
    the SDK-agnostic shape should use ``message`` / ``usage`` instead.
    """

    message: Message
    finish_reason: str | None = None
    usage: Usage = field(default_factory=Usage)
    raw: Any | None = None


@dataclass(frozen=True)
class Chunk:
    """One delta from a streaming response.

    Backends translate their native streaming events into a sequence of
    ``Chunk`` objects. ``delta`` is a fragment of assistant text; ``tool_calls``
    is populated when the chunk advertises tool-call deltas. ``finish_reason``
    is set on the terminal chunk only.
    """

    delta: str = ""
    tool_calls: tuple[ToolCall, ...] = field(default_factory=tuple)
    finish_reason: str | None = None
    usage: Usage | None = None
    raw: Any | None = None
