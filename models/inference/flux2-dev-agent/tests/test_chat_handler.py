"""chat_handler: orchestrate translator + flux_client + image_store
and return an OpenAI-shape ChatCompletionResponse whose assistant
message contains a markdown image tag.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.chat_handler import ChatHandler
from app.image_store import ImageStore
from app.schemas import ChatCompletionRequest, ChatMessage


_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


@pytest.fixture
def store(tmp_path: Path) -> ImageStore:
    return ImageStore(local_dir=tmp_path, public_url_prefix="/uploads/flux")


@pytest.mark.asyncio
async def test_handle_returns_markdown_image_in_assistant_content(store: ImageStore):
    translator = AsyncMock()
    translator.translate.return_value = "a tank in the mountains"

    flux_client = AsyncMock()
    flux_client.generate.return_value = _PNG

    handler = ChatHandler(
        translator=translator,
        flux_client_factory=lambda: _AsyncContext(flux_client),
        image_store=store,
        default_aspect_ratio="16:9",
    )

    req = ChatCompletionRequest(
        model="image-generator",
        messages=[ChatMessage(role="user", content="畫一張在山上的坦克")],
    )
    resp = await handler.handle(req)

    assert resp.model == "image-generator"
    assert len(resp.choices) == 1
    content = resp.choices[0].message.content
    assert content.startswith("已為您繪製")
    assert "![](" in content
    assert "/uploads/flux/" in content
    assert content.rstrip().endswith(".png)")


@pytest.mark.asyncio
async def test_handle_translates_prompt_before_calling_flux(store: ImageStore):
    translator = AsyncMock()
    translator.translate.return_value = "ENGLISH PROMPT"

    flux_client = AsyncMock()
    flux_client.generate.return_value = _PNG

    handler = ChatHandler(
        translator=translator,
        flux_client_factory=lambda: _AsyncContext(flux_client),
        image_store=store,
        default_aspect_ratio="16:9",
    )

    req = ChatCompletionRequest(
        model="image-generator",
        messages=[ChatMessage(role="user", content="原始中文")],
    )
    await handler.handle(req)

    translator.translate.assert_awaited_once_with("原始中文")
    flux_client.generate.assert_awaited_once()
    call_args = flux_client.generate.await_args
    assert call_args.args[0] == "ENGLISH PROMPT" or call_args.kwargs.get("prompt") == "ENGLISH PROMPT"


@pytest.mark.asyncio
async def test_handle_passes_default_aspect_ratio(store: ImageStore):
    translator = AsyncMock()
    translator.translate.return_value = "x"
    flux_client = AsyncMock()
    flux_client.generate.return_value = _PNG

    handler = ChatHandler(
        translator=translator,
        flux_client_factory=lambda: _AsyncContext(flux_client),
        image_store=store,
        default_aspect_ratio="1:1",
    )

    req = ChatCompletionRequest(
        model="image-generator",
        messages=[ChatMessage(role="user", content="x")],
    )
    await handler.handle(req)

    call = flux_client.generate.await_args
    aspect = call.kwargs.get("aspect_ratio") or (call.args[1] if len(call.args) > 1 else None)
    assert aspect == "1:1"


class _AsyncContext:
    """Minimal async-context-manager wrapper around an already-built mock."""

    def __init__(self, target):
        self._target = target

    async def __aenter__(self):
        return self._target

    async def __aexit__(self, *exc):
        return None
