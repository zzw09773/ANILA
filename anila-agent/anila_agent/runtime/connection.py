"""``ConnectionStrategy`` — the backend abstraction every runtime honours.

The intent of this ABC is to decouple anila-agent's higher layers (hooks,
policy, trigger, runner) from any specific LLM SDK. Today the default
implementation wraps ``openai-agents``; a future Raw HTTP backend (P1-11
second cut) and any other backend (a different framework, an on-prem agent
server) will subclass this without touching the rest of the codebase.

Backends MUST honour the contract precisely:

- ``send_message`` returns a single :class:`~anila_agent.runtime.types.Response`.
- ``stream_message`` is an async iterator of
  :class:`~anila_agent.runtime.types.Chunk` deltas, terminating with a chunk
  whose ``finish_reason`` is set.
- ``close`` releases any persistent resources (HTTP clients, subprocesses).
  It MUST be idempotent.
- ``name`` is the stable identifier under which the strategy is registered in
  :class:`~anila_agent.runtime.registry.ConnectionRegistry`.

Because the interface is async, backends are free to multiplex calls across
the asyncio event loop, but no per-call concurrency guarantee is required at
this layer.
"""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator, Sequence
from typing import Any

from anila_agent.runtime.types import Chunk, Message, Response


class ConnectionStrategy(abc.ABC):
    """Abstract backend interface.

    Subclasses must implement :meth:`send_message`, :meth:`stream_message`,
    :meth:`close`, and the :attr:`name` property. Direct instantiation of this
    class raises ``TypeError`` (enforced by the ``@abstractmethod`` decorators
    and the abstract ``name`` property).
    """

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Stable identifier (e.g. ``"openai_agents"``, ``"raw_http"``)."""
        ...

    @abc.abstractmethod
    async def send_message(
        self,
        messages: Sequence[Message],
        **kwargs: Any,
    ) -> Response:
        """Synchronous (non-streaming) call.

        Args:
            messages: Ordered chat history. The last message is typically the
                user turn that triggered the call.
            **kwargs: Backend-specific overrides (model name, sampling params,
                tool-choice). Backends MUST tolerate unknown keys gracefully —
                either consume them or ignore them, never raise on contact.

        Returns:
            A :class:`~anila_agent.runtime.types.Response` whose ``message``
            field is the assistant turn produced by the backend.
        """
        ...

    @abc.abstractmethod
    def stream_message(
        self,
        messages: Sequence[Message],
        **kwargs: Any,
    ) -> AsyncIterator[Chunk]:
        """Streaming call.

        Implementations return an ``AsyncIterator`` of
        :class:`~anila_agent.runtime.types.Chunk` deltas. The terminal chunk
        carries a non-None ``finish_reason``; downstream consumers may rely on
        this to detect completion without inspecting backend-specific signals.

        Note: this method is declared synchronously (returns an iterator) so
        callers can ``async for chunk in conn.stream_message(...)`` without a
        prior ``await``. Concrete implementations are typically ``async def``
        generators, which fits this signature because Python treats an async
        generator function as returning an async iterator.
        """
        ...

    @abc.abstractmethod
    async def close(self) -> None:
        """Release persistent resources.

        Must be idempotent — calling it twice on the same instance is a no-op.
        """
        ...

    async def __aenter__(self) -> ConnectionStrategy:
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()
