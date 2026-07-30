"""Coordinate translator + flux_client + image_store and assemble an
OpenAI-shape ChatCompletionResponse.

``flux_client_factory`` returns a context manager that yields the
actual ``FluxClient`` — this lets the handler own connection lifetime
per request without hard-coding the constructor (so tests can inject
a pre-built mock).
"""
from __future__ import annotations

import time
import uuid
from typing import Callable, Protocol

from .image_store import ImageStore
from .schemas import (
    ChatCompletionChoice,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
)


class _TranslatorProto(Protocol):
    async def translate(self, user_text: str) -> str: ...


class _FluxClientProto(Protocol):
    async def generate(self, prompt: str, aspect_ratio: str) -> bytes: ...


class _FluxClientCtxProto(Protocol):
    async def __aenter__(self) -> _FluxClientProto: ...
    async def __aexit__(self, *exc) -> None: ...


class _BackendResolverProto(Protocol):
    async def resolve(self) -> tuple[str, str]: ...


class ChatHandler:
    def __init__(
        self,
        *,
        translator: _TranslatorProto,
        flux_client_factory: Callable[[str, str], _FluxClientCtxProto],
        image_store: ImageStore,
        backend_resolver: _BackendResolverProto,
        default_aspect_ratio: str = "16:9",
    ) -> None:
        self._translator = translator
        self._flux_client_factory = flux_client_factory
        self._image_store = image_store
        self._backend_resolver = backend_resolver
        self._default_aspect_ratio = default_aspect_ratio

    async def handle(self, req: ChatCompletionRequest) -> ChatCompletionResponse:
        user_text = req.last_user_text()
        english_prompt = await self._translator.translate(user_text)

        # 每次生圖前重新解析 (endpoint, model):CSP image-primary 優先,
        # 60s TTL 快取;無值時 fallback env(見 backend_resolver.py)。
        # FluxClient 本來就每次生圖新建一個(見下方 flux_client_factory
        # 呼叫處),所以「endpoint/model 變更時重建」不需要額外快取邏輯,
        # 只要把解析出來的值傳進工廠即可。
        endpoint, model = await self._backend_resolver.resolve()

        async with self._flux_client_factory(endpoint, model) as flux:
            png_bytes = await flux.generate(
                english_prompt,
                aspect_ratio=self._default_aspect_ratio,
            )

        url = self._image_store.save(png_bytes)

        content = f"已為您繪製：\n\n![]({url})"

        return ChatCompletionResponse(
            id=f"flux-{uuid.uuid4().hex}",
            created=int(time.time()),
            model=req.model,
            choices=[
                ChatCompletionChoice(
                    index=0,
                    message=ChatMessage(role="assistant", content=content),
                    finish_reason="stop",
                ),
            ],
        )
