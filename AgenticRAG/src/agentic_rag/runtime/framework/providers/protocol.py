"""LLMProvider Protocol — the boundary between framework and vendor SDK.

The run loop only ever talks to providers through this Protocol. Any
concrete provider (OpenAI Chat Completions, Anthropic Messages,
local TGI, etc.) just has to satisfy the shape.

v0.1 alpha: the Protocol has placeholders. Concrete shapes (Message,
ChatCompletionResponse, etc.) ship with Sprint 1 stage B once
``items.py`` lands. The Protocol surface here is deliberately
minimal — we'll widen it iteratively as concrete providers reveal
what they actually need.

Design rule: anything OpenAI-Responses-API-specific (built-in tools,
hosted Code Interpreter, server-side conversations) does NOT enter
this Protocol. Those are provider extensions, surfaced via
provider-specific Protocols that subclass ``LLMProvider``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class LLMProvider(Protocol):
    """Minimum surface every LLM provider must satisfy.

    The shapes of ``messages`` / ``tools`` / responses are defined in
    ``agentic_rag.runtime.framework.items`` (lands Sprint 1 stage B). Until then they're
    typed as ``Any`` here so the Protocol compiles standalone.
    """

    async def chat_completion(
        self,
        messages: list[Any],
        tools: list[Any] | None = None,
        *,
        model: str,
        stream: bool = False,
        **kwargs: Any,
    ) -> Any | AsyncIterator[Any]:
        """Run a single chat completion call.

        When ``stream=False`` returns the full response object. When
        ``stream=True`` returns an async iterator of chunks. Concrete
        chunk type is provider-defined; the runtime adapts.
        """
        ...

    async def embeddings(
        self,
        texts: list[str],
        *,
        model: str,
        **kwargs: Any,
    ) -> list[list[float]]:
        """Compute embeddings for a batch of texts.

        Optional in v0.1 — providers that don't do embeddings (or are
        used for chat-only roles) can raise ``NotImplementedError``.
        AgenticRAG's retrieval pipeline supplies its own embedder.
        """
        ...
