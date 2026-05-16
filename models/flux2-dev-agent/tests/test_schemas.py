"""Schema tests — ensure the shim accepts OpenAI chat completion
requests in the shape that CSP forwards.

CSP forwards the body verbatim to the agent endpoint, so we must
accept the exact OpenAI v1 shape including the ``anila_session_id``
extension that anila-core's dispatch_tool.py embeds for stateful
conversations.
"""
from __future__ import annotations

import pytest

from app.schemas import ChatCompletionRequest, ChatMessage


def test_chat_request_minimal():
    req = ChatCompletionRequest(
        model="image-generator",
        messages=[ChatMessage(role="user", content="畫一張坦克")],
    )
    assert req.model == "image-generator"
    assert req.messages[0].content == "畫一張坦克"


def test_chat_request_with_anila_extension():
    req = ChatCompletionRequest(
        model="image-generator",
        messages=[ChatMessage(role="user", content="畫一張坦克")],
        anila_session_id="sess_abc123",
    )
    assert req.anila_session_id == "sess_abc123"


def test_chat_request_last_user_message_helper():
    req = ChatCompletionRequest(
        model="image-generator",
        messages=[
            ChatMessage(role="system", content="ignore me"),
            ChatMessage(role="user", content="first"),
            ChatMessage(role="assistant", content="ok"),
            ChatMessage(role="user", content="second"),
        ],
    )
    assert req.last_user_text() == "second"


def test_chat_request_rejects_empty_messages():
    with pytest.raises(ValueError):
        ChatCompletionRequest(model="image-generator", messages=[])
