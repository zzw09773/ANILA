"""Default backend: wraps the openai-agents SDK as a ``ConnectionStrategy``.

This is the only backend shipped with P1-11 first cut. It preserves the
exact behaviour anila-agent has today — the runner-level call into
``agents.Runner.run`` is unchanged — but exposes it through the
SDK-agnostic :class:`ConnectionStrategy` surface so future backends can be
swapped in without touching hooks / policy / trigger code.

Translation responsibilities:

- :class:`anila_agent.runtime.types.Message` → ``openai-agents`` ``input``.
- ``openai-agents`` ``Result.final_output`` → :class:`Response`.

Tool-call passthrough and streaming are wired pragmatically: openai-agents
already owns the tool-loop, so :meth:`send_message` returns the terminal
assistant turn; :meth:`stream_message` is a thin adapter over the SDK's
``Runner.run_streamed`` API when available, otherwise it falls back to a
single-chunk stream built from the non-streaming response.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from typing import Any

from anila_agent.runtime.connection import ConnectionStrategy
from anila_agent.runtime.types import Chunk, Message, Response, ToolCall, Usage


class OpenAIAgentsConnection(ConnectionStrategy):
    """Default backend wrapping ``agents.Runner`` from openai-agents.

    ``agent`` and ``runner_cls`` are injected so callers (typically the runner
    or test fixtures) can plug in either the real openai-agents objects or a
    mock. The class intentionally accepts the ``Agent`` and ``Runner`` types
    as plain ``Any`` — this keeps the import surface lazy and avoids leaking
    SDK types into module-level type checking.
    """

    name_: str = "openai_agents"

    def __init__(
        self,
        agent: Any,
        *,
        runner_cls: Any | None = None,
        max_turns: int | None = None,
        session: Any | None = None,
        hooks: Any | None = None,
    ) -> None:
        self._agent = agent
        self._runner_cls = runner_cls
        self._max_turns = max_turns
        self._session = session
        self._hooks = hooks
        self._closed = False

    @property
    def name(self) -> str:
        return self.name_

    @property
    def agent(self) -> Any:
        return self._agent

    def _resolve_runner(self) -> Any:
        if self._runner_cls is not None:
            return self._runner_cls
        # Lazy import so importing this module does not require openai-agents
        # at definition time (mock-driven tests can construct the connection
        # without pulling the SDK).
        from agents import Runner  # type: ignore[import-untyped]

        return Runner

    @staticmethod
    def _messages_to_input(messages: Sequence[Message]) -> str:
        """Collapse messages into the prompt openai-agents Runner consumes.

        openai-agents' ``Runner.run`` takes ``input: str`` (the user prompt)
        plus a session that already contains prior turns. The mapping below is
        pragmatic: we surface the final user turn as the prompt and surface
        any leading non-user content via ``metadata``-less concatenation so a
        mock can still observe the full payload.
        """
        if not messages:
            return ""
        last = messages[-1]
        if last.role == "user" and last.content is not None:
            return last.content
        # Fallback: join all textual content. Keeps the contract lossless for
        # non-standard sequences (e.g. system + assistant only).
        chunks = [m.content for m in messages if m.content is not None]
        return "\n".join(chunks)

    @staticmethod
    def _result_to_response(result: Any) -> Response:
        """Translate an openai-agents ``Result`` into a :class:`Response`.

        The SDK exposes ``final_output`` as ``Any``. We coerce to string for
        ``content`` while preserving the original via ``Response.raw``.
        """
        final_output = getattr(result, "final_output", None)
        content: str | None
        if final_output is None:
            content = None
        elif isinstance(final_output, str):
            content = final_output
        else:
            # Best-effort serialization; avoids raising on exotic outputs.
            try:
                content = json.dumps(final_output, default=str)
            except (TypeError, ValueError):
                content = str(final_output)

        tool_calls: tuple[ToolCall, ...] = ()
        raw_tool_calls = getattr(result, "tool_calls", None)
        if raw_tool_calls:
            tool_calls = tuple(
                ToolCall(
                    id=getattr(tc, "id", "") or "",
                    name=getattr(tc, "name", "") or "",
                    arguments=getattr(tc, "arguments", "") or "",
                )
                for tc in raw_tool_calls
            )

        usage_raw = getattr(result, "usage", None)
        usage = Usage(
            prompt_tokens=getattr(usage_raw, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(usage_raw, "completion_tokens", 0) or 0,
            total_tokens=getattr(usage_raw, "total_tokens", 0) or 0,
        )

        return Response(
            message=Message(role="assistant", content=content, tool_calls=tool_calls),
            finish_reason=getattr(result, "finish_reason", "stop"),
            usage=usage,
            raw=result,
        )

    async def send_message(
        self,
        messages: Sequence[Message],
        **kwargs: Any,
    ) -> Response:
        if self._closed:
            raise RuntimeError("OpenAIAgentsConnection is closed")
        runner_cls = self._resolve_runner()
        prompt = self._messages_to_input(messages)

        run_kwargs: dict[str, Any] = {
            "starting_agent": self._agent,
            "input": prompt,
        }
        if self._max_turns is not None:
            run_kwargs["max_turns"] = self._max_turns
        if self._session is not None:
            run_kwargs["session"] = self._session
        if self._hooks is not None:
            run_kwargs["hooks"] = self._hooks
        # Caller-provided kwargs override defaults so tests / advanced flows
        # can pin a model, force streaming options, etc.
        run_kwargs.update(kwargs)

        result = await runner_cls.run(**run_kwargs)
        return self._result_to_response(result)

    async def stream_message(
        self,
        messages: Sequence[Message],
        **kwargs: Any,
    ) -> AsyncIterator[Chunk]:
        if self._closed:
            raise RuntimeError("OpenAIAgentsConnection is closed")
        runner_cls = self._resolve_runner()
        prompt = self._messages_to_input(messages)

        # Prefer the SDK's native streaming entry point when present. Fall
        # back to single-shot non-streaming + one terminal chunk so callers
        # that depend on the streaming contract still get a usable iterator.
        run_streamed = getattr(runner_cls, "run_streamed", None)
        if run_streamed is None:
            response = await self.send_message(messages, **kwargs)
            yield Chunk(
                delta=response.message.content or "",
                tool_calls=response.message.tool_calls,
                finish_reason=response.finish_reason or "stop",
                usage=response.usage,
                raw=response.raw,
            )
            return

        run_kwargs: dict[str, Any] = {
            "starting_agent": self._agent,
            "input": prompt,
        }
        if self._max_turns is not None:
            run_kwargs["max_turns"] = self._max_turns
        if self._session is not None:
            run_kwargs["session"] = self._session
        if self._hooks is not None:
            run_kwargs["hooks"] = self._hooks
        run_kwargs.update(kwargs)

        stream = run_streamed(**run_kwargs)
        async for event in stream:
            delta = getattr(event, "delta", "") or ""
            finish_reason = getattr(event, "finish_reason", None)
            yield Chunk(delta=delta, finish_reason=finish_reason, raw=event)

    async def close(self) -> None:
        # The openai-agents Runner is stateless across calls; nothing to
        # release on our side. ``_closed`` is still flipped so further
        # send/stream calls fail loudly instead of silently re-opening.
        self._closed = True
