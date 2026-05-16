"""Pydantic models matching the OpenAI v1 chat completion shape.

The shim accepts the exact body that anila-core's
``dispatch_to_agent_response`` sends, which is the standard OpenAI
``/v1/chat/completions`` body plus the ``anila_session_id`` extension
field (and optionally ``anila_handoff``). CSP forwards verbatim.
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[ChatMessage]
    stream: bool = False
    anila_session_id: Optional[str] = None
    anila_handoff: Optional[dict[str, Any]] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None

    @field_validator("messages")
    @classmethod
    def _non_empty(cls, v: list[ChatMessage]) -> list[ChatMessage]:
        if not v:
            raise ValueError("messages must not be empty")
        return v

    def last_user_text(self) -> str:
        for m in reversed(self.messages):
            if m.role == "user":
                return m.content
        raise ValueError("no user message in conversation")


class ChatCompletionChoice(BaseModel):
    index: int = 0
    message: ChatMessage
    finish_reason: str = "stop"


class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: list[ChatCompletionChoice]
    usage: dict[str, int] = Field(
        default_factory=lambda: {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }
    )
