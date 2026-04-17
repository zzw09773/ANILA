"""Message models for the ANILA Core agent runtime.

These models represent the fundamental units of communication between
the user, the assistant, and tools.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, Field


class Role(str, Enum):
    """Message role in a conversation."""

    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"


class Usage(BaseModel):
    """Token usage tracking for an API call."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def add(self, other: "Usage") -> "Usage":
        """Return a new Usage that is the sum of self and other."""
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
        )


class ToolCallDelta(BaseModel):
    """Streaming fragment of a tool call."""

    id: str
    type: Literal["tool_use"] = "tool_use"
    name: str
    input_partial: str = ""


class ToolCall(BaseModel):
    """A complete tool call from the assistant."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    input: dict[str, Any]


class ToolResult(BaseModel):
    """Result from executing a tool call."""

    tool_call_id: str
    content: Union[str, list[dict[str, Any]]]
    is_error: bool = False

    def as_text(self) -> str:
        """Return the content as a plain string."""
        if isinstance(self.content, str):
            return self.content
        parts = []
        for block in self.content:
            if block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(parts)


class StreamDelta(BaseModel):
    """A single event in a streaming response from the provider."""

    type: Literal["text", "tool_call", "reasoning", "stop"]
    text: Optional[str] = None
    tool_call: Optional[ToolCallDelta] = None
    finish_reason: Optional[str] = None
    usage: Optional[Usage] = None


class AssistantMessage(BaseModel):
    """A message produced by the assistant (model response)."""

    uuid: str = Field(default_factory=lambda: str(uuid.uuid4()))
    role: Literal["assistant"] = "assistant"
    content: Union[str, list[dict[str, Any]]]
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: Optional[Usage] = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)

    def get_text(self) -> str:
        """Extract plain text content from the message."""
        if isinstance(self.content, str):
            return self.content
        parts = []
        for block in self.content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(parts)

    def has_tool_calls(self) -> bool:
        return len(self.tool_calls) > 0


class UserMessage(BaseModel):
    """A message from the user."""

    uuid: str = Field(default_factory=lambda: str(uuid.uuid4()))
    role: Literal["user"] = "user"
    content: Union[str, list[dict[str, Any]]]
    timestamp: datetime = Field(default_factory=datetime.utcnow)

    def get_text(self) -> str:
        """Extract plain text content from the message."""
        if isinstance(self.content, str):
            return self.content
        parts = []
        for block in self.content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(parts)


# Union type for any message in conversation history
Message = Union[UserMessage, AssistantMessage]
