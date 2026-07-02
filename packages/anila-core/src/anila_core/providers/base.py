"""Provider Protocol - the only interface the engine needs from model backends.

All provider implementations must expose stream_completion as an async
generator that yields StreamDelta events.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Protocol, runtime_checkable

from ..models.message import Message, StreamDelta


@dataclass
class ProviderRequest:
    """Input to a provider completion call."""

    model: str
    messages: list[Message]
    system: str = ""
    tools: list[dict[str, Any]] = field(default_factory=list)
    max_tokens: int = 4096
    temperature: float = 0.0
    stream: bool = True
    extra: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Provider(Protocol):
    """Protocol that all provider adapters must satisfy."""

    def stream_completion(
        self, request: ProviderRequest
    ) -> AsyncIterator[StreamDelta]:
        """Stream a completion from the model.

        Implementations may be either an async generator function (``async def``
        with ``yield``) or a regular function returning an ``AsyncIterator``.
        Both satisfy this Protocol because async generators are AsyncIterators.

        Callers use::

            async for delta in provider.stream_completion(request):
                ...

        Yields StreamDelta events in order:
          - text deltas (type="text")
          - tool call deltas (type="tool_call")
          - reasoning deltas (type="reasoning")
          - stop event (type="stop", with finish_reason and usage)
        """
        ...  # pragma: no cover
