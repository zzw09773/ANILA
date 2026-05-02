"""Provider-agnostic conversation and run-item types.

Inspired by ``items.py`` from openai-agents-python (MIT, ~864 LOC), but
this is a **fresh-write trim, not a port**. The upstream file is dense
with OpenAI Responses-API specific item types (``ResponseFileSearchToolCall``,
``ResponseComputerToolCall``, ``ResponseCodeInterpreterToolCall``, hosted
MCP approval items, image-generation items, local-shell items, …) which
we deliberately drop because:

1. The framework targets Chat-Completions-shape providers in v0.1
   (OpenAI / vLLM / NIM / TGI / Ollama). Responses-API extras are
   provider-specific and would leak vendor concepts into the core.
2. Hosted built-ins (Code Interpreter, Web Search, etc.) are surfaced
   later as provider extensions on top of the base Protocol, not as
   first-class core item types.
3. Streaming chunk types are kept provider-side; the runtime sees fully
   assembled messages.

What this module ships:

- ``Role`` enum — one of ``system / user / assistant / tool``
- ``Message`` — a single conversation turn (text + optional tool calls
  on the assistant side, tool result body on the tool side)
- ``ToolCall`` — an LLM-issued request to invoke a tool (id + name + args)
- ``ToolResult`` — the runtime's reply to a ToolCall (id + output | error)
- ``Content*`` parts — text / image_url / refusal content blocks for
  multimodal messages
- ``RunItem`` and subclasses — the audit-trail items the runner emits
  alongside the new ``Message`` history (one ``RunItem`` per side-effecting
  step, useful for tracing / replay / UI)

The shapes match Chat Completions message structure intentionally so the
default ``OpenAICompatProvider`` can map back and forth without a
translation layer of its own.

Provenance: trimmed from openai-agents-python (MIT). Original file:
``runtime_logic/openai-agents-python/src/agents/items.py``.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal, Union

from agentic_rag.runtime.framework.usage import Usage

# ── Roles ────────────────────────────────────────────────────────────────


class Role(StrEnum):
    """Speaker of a ``Message``.

    Maps 1:1 onto Chat Completions roles. ``DEVELOPER`` is the
    OpenAI-compat name for the system-style instruction channel some
    newer models prefer; we accept both ``SYSTEM`` and ``DEVELOPER`` and
    let the provider decide whether to coalesce.
    """

    SYSTEM = "system"
    DEVELOPER = "developer"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


# ── Content parts (multimodal-ready) ─────────────────────────────────────


@dataclass(frozen=True)
class TextContent:
    """A plain text content block in a multimodal message."""

    text: str
    type: Literal["text"] = "text"


@dataclass(frozen=True)
class ImageURLContent:
    """An image referenced by URL or data URI.

    ``detail`` mirrors OpenAI's ``low``/``high``/``auto`` setting; most
    other providers ignore it but accepting the field keeps parity.
    """

    url: str
    detail: Literal["low", "high", "auto"] = "auto"
    type: Literal["image_url"] = "image_url"


@dataclass(frozen=True)
class RefusalContent:
    """A model refusal carried as content rather than thrown.

    Providers that return refusals as a structured field (OpenAI does
    this for some models) get them surfaced here so middleware can
    inspect rather than crash.
    """

    refusal: str
    type: Literal["refusal"] = "refusal"


ContentPart = Union[TextContent, ImageURLContent, RefusalContent]
"""Discriminated union of message content blocks. ``type`` is the tag."""


# ── Tool call / result ──────────────────────────────────────────────────


@dataclass(frozen=True)
class ToolCall:
    """An assistant message's request to invoke a tool.

    The id is provider-issued (OpenAI returns ``call_xxx``); we preserve
    it verbatim so the matching ``ToolResult`` can be paired without
    ambiguity. ``arguments`` is the raw JSON string the model produced;
    parsing into a dict happens at the runner boundary so providers
    that send malformed JSON surface a clean ``ModelBehaviorError``
    instead of a crash deep in the tool handler.
    """

    id: str
    name: str
    arguments: str
    type: Literal["function"] = "function"

    def parsed_arguments(self) -> dict[str, Any]:
        """Decode the JSON arguments. Raises ``ValueError`` if invalid."""
        if not self.arguments:
            return {}
        try:
            decoded = json.loads(self.arguments)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"ToolCall {self.id} ({self.name}) arguments are not valid JSON: {exc}"
            ) from exc
        if not isinstance(decoded, dict):
            raise ValueError(
                f"ToolCall {self.id} ({self.name}) arguments must decode to an "
                f"object, got {type(decoded).__name__}"
            )
        return decoded


@dataclass(frozen=True)
class ToolResult:
    """The runtime's reply to a ``ToolCall``.

    Exactly one of ``output`` or ``error`` is set:
    - ``output`` is the serialised result the LLM will see (string by
      convention; structured payloads should be JSON-encoded by the
      caller).
    - ``error`` carries a human-readable error string the LLM can read
      and decide whether to retry / handoff / give up.
    """

    call_id: str
    name: str
    output: str | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if (self.output is None) == (self.error is None):
            raise ValueError(
                f"ToolResult for call {self.call_id} must set exactly one of "
                f"output or error (got output={self.output!r}, error={self.error!r})"
            )

    @property
    def is_error(self) -> bool:
        return self.error is not None

    def as_message_content(self) -> str:
        """Render as the tool-role message body the LLM consumes."""
        if self.is_error:
            return f"[error] {self.error}"
        return self.output or ""


# ── Message ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Message:
    """A single conversation turn.

    Modeled to round-trip cleanly through Chat Completions:

    - ``role=system|developer|user``: ``content`` carries text or content
      parts; ``tool_calls`` and ``tool_call_id`` are unused.
    - ``role=assistant``: ``content`` may be empty (the assistant chose
      to call a tool with no preamble); ``tool_calls`` holds any
      requested invocations.
    - ``role=tool``: ``tool_call_id`` references the assistant's
      ``ToolCall.id``; ``content`` is the result body; ``name`` is the
      tool name (some providers require it for routing).

    ``content`` accepts either a string (the simple case) or a tuple of
    ``ContentPart`` for multimodal payloads. Strings are not re-wrapped
    into TextContent here — the provider decides what its API shape
    actually wants.
    """

    role: Role
    content: str | tuple[ContentPart, ...] = ""
    name: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None

    def __post_init__(self) -> None:
        # Tool messages must reference the originating call.
        if self.role is Role.TOOL and not self.tool_call_id:
            raise ValueError("Tool-role messages require tool_call_id")
        # Only assistant messages may carry tool_calls.
        if self.tool_calls and self.role is not Role.ASSISTANT:
            raise ValueError(
                f"tool_calls only allowed on assistant messages, got role={self.role}"
            )

    @classmethod
    def system(cls, text: str) -> Message:
        return cls(role=Role.SYSTEM, content=text)

    @classmethod
    def developer(cls, text: str) -> Message:
        return cls(role=Role.DEVELOPER, content=text)

    @classmethod
    def user(cls, content: str | tuple[ContentPart, ...]) -> Message:
        return cls(role=Role.USER, content=content)

    @classmethod
    def assistant(
        cls,
        content: str | tuple[ContentPart, ...] = "",
        *,
        tool_calls: tuple[ToolCall, ...] = (),
    ) -> Message:
        return cls(role=Role.ASSISTANT, content=content, tool_calls=tool_calls)

    @classmethod
    def tool(cls, *, call_id: str, name: str, content: str) -> Message:
        return cls(role=Role.TOOL, content=content, name=name, tool_call_id=call_id)

    @classmethod
    def from_tool_result(cls, result: ToolResult) -> Message:
        """Convenience: wrap a ``ToolResult`` as a tool-role ``Message``."""
        return cls.tool(
            call_id=result.call_id,
            name=result.name,
            content=result.as_message_content(),
        )


# ── Run items (audit-trail, separate from Message history) ───────────────


def _new_item_id() -> str:
    """Generate a short unique id for a RunItem.

    Used for tracing / dedup across middleware. Callers may override
    by passing an explicit id at construction.
    """
    return f"item_{uuid.uuid4().hex[:16]}"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class RunItem:
    """Base class — an immutable audit-trail entry from a single run.

    The runner emits one ``RunItem`` per observable event (LLM call,
    tool call, tool result, handoff). They are not the conversation
    history — they're the side-effects log used by tracing,
    cost-attribution, replay, and UI rendering. Subclasses carry the
    payload specific to each kind.

    Why separate from ``Message``: the conversation history is what the
    LLM next sees. The run-item log is what humans / dashboards see.
    Conflating them (as ``items.py`` upstream does) means every UI
    consumer has to re-derive the message history from the item stream.
    Keeping them split lets each be optimised independently.
    """

    item_id: str = field(default_factory=_new_item_id)
    created_at: datetime = field(default_factory=_utc_now)


@dataclass(frozen=True)
class MessageOutputItem(RunItem):
    """An assistant message produced by an LLM turn."""

    message: Message = field(default_factory=lambda: Message.assistant(""))
    usage: Usage = field(default_factory=Usage)


@dataclass(frozen=True)
class ToolCallItem(RunItem):
    """A tool-call request the assistant emitted.

    Distinct from ``MessageOutputItem`` so middleware that only cares
    about side-effects can subscribe at the right granularity.
    """

    call: ToolCall = field(default_factory=lambda: ToolCall(id="", name="", arguments=""))


@dataclass(frozen=True)
class ToolResultItem(RunItem):
    """The runtime's response to a ``ToolCallItem``.

    ``elapsed_seconds`` measures wall-clock duration of the handler;
    cost / token attribution lives in ``MessageOutputItem.usage`` for
    LLM calls and in middleware-attached metadata for tools.
    """

    result: ToolResult = field(
        default_factory=lambda: ToolResult(call_id="", name="", output="")
    )
    elapsed_seconds: float = 0.0


@dataclass(frozen=True)
class HandoffItem(RunItem):
    """A control transfer from one agent to another.

    ``from_agent`` / ``to_agent`` are the agent names (string-keyed) so
    the item remains serialisable without holding live ``Agent``
    references. ``reason`` is the optional human-readable rationale the
    handing-off agent supplied.
    """

    from_agent: str = ""
    to_agent: str = ""
    reason: str | None = None


@dataclass(frozen=True)
class ErrorItem(RunItem):
    """A run-fatal error captured for the audit trail.

    The runner still raises the exception; this item exists so the
    audit trail records *what* failed, not just *that* something failed.
    """

    error_type: str = ""
    message: str = ""


# ── Provider response shape ─────────────────────────────────────────────


class FinishReason(StrEnum):
    """Why a chat completion stopped.

    Mirrors the Chat Completions ``finish_reason`` set. Providers that
    surface non-standard reasons normalise into ``OTHER`` and stash
    the original string in the response's raw payload.
    """

    STOP = "stop"
    LENGTH = "length"
    TOOL_CALLS = "tool_calls"
    CONTENT_FILTER = "content_filter"
    OTHER = "other"


@dataclass(frozen=True)
class ChatCompletionResponse:
    """The non-streaming return shape of ``LLMProvider.chat_completion``.

    Concrete providers build one of these from their native response.
    The runner reads only this — never a vendor object — so swapping
    providers is a matter of mapping the wire payload here.

    ``message`` is the assistant's reply (with any tool_calls attached).
    ``usage`` is the per-call token accounting; the runner aggregates
    into the per-run Usage. ``finish_reason`` drives the runner's
    decision tree (continue / stop / dispatch tools).
    ``raw`` keeps the original provider payload available for tracing
    middleware that wants to surface vendor-specific fields.
    """

    message: Message
    usage: Usage
    finish_reason: FinishReason
    raw: dict[str, Any] | None = None


# ── Public surface ───────────────────────────────────────────────────────


__all__ = [
    "ChatCompletionResponse",
    "ContentPart",
    "ErrorItem",
    "FinishReason",
    "HandoffItem",
    "ImageURLContent",
    "Message",
    "MessageOutputItem",
    "RefusalContent",
    "Role",
    "RunItem",
    "TextContent",
    "ToolCall",
    "ToolCallItem",
    "ToolResult",
    "ToolResultItem",
]
