"""MockProvider - scripted responses for testing.

Supports injecting text responses, tool calls, errors, and token usage.
The script is consumed in order; after exhaustion, a default empty response
is returned.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Optional

from ..models.message import StreamDelta, ToolCallDelta, Usage
from .base import ProviderRequest


@dataclass
class ScriptedToolCall:
    """A scripted tool call to emit from the mock provider."""

    name: str
    input: dict[str, Any]
    tool_id: str = field(default_factory=lambda: str(uuid.uuid4()))


@dataclass
class ScriptedResponse:
    """One scripted turn in the mock provider's script.

    Either text or tool_calls should be set, not both.
    """

    text: str = ""
    tool_calls: list[ScriptedToolCall] = field(default_factory=list)
    finish_reason: str = "end_turn"
    usage: Optional[Usage] = None
    raise_error: Optional[Exception] = None
    delay_ms: float = 0.0


class MockProvider:
    """Provider that returns scripted responses for testing.

    Usage:
        mock = MockProvider([
            ScriptedResponse(text="Hello!"),
            ScriptedResponse(
                tool_calls=[ScriptedToolCall(name="bash", input={"command": "ls"})],
                finish_reason="tool_use",
            ),
            ScriptedResponse(text="Done."),
        ])
        engine = QueryEngine(mock, registry, config)
        result = await engine.run(messages)
    """

    def __init__(
        self,
        script: Optional[list[ScriptedResponse]] = None,
        default_response: Optional[ScriptedResponse] = None,
    ) -> None:
        self._script = list(script or [])
        self._default = default_response or ScriptedResponse(
            text="[MockProvider: script exhausted]"
        )
        self._call_count = 0
        self._requests: list[ProviderRequest] = []

    @property
    def call_count(self) -> int:
        return self._call_count

    @property
    def requests(self) -> list[ProviderRequest]:
        """Return all ProviderRequests received, in order."""
        return list(self._requests)

    def reset(self) -> None:
        """Reset call count and recorded requests."""
        self._call_count = 0
        self._requests.clear()

    async def stream_completion(
        self, request: ProviderRequest
    ) -> AsyncIterator[StreamDelta]:
        self._call_count += 1
        self._requests.append(request)

        if self._script:
            response = self._script.pop(0)
        else:
            response = self._default

        if response.delay_ms > 0:
            await asyncio.sleep(response.delay_ms / 1000)

        if response.raise_error is not None:
            raise response.raise_error

        # Yield events inline (this method IS an async generator)
        if response.text:
            yield StreamDelta(type="text", text=response.text)

        for tc in response.tool_calls:
            import json
            input_str = json.dumps(tc.input)
            yield StreamDelta(
                type="tool_call",
                tool_call=ToolCallDelta(
                    id=tc.tool_id,
                    name=tc.name,
                    input_partial=input_str,
                ),
            )

        usage = response.usage or Usage(
            input_tokens=100,
            output_tokens=len(response.text.split()) * 2 + len(response.tool_calls) * 20,
        )
        yield StreamDelta(
            type="stop",
            finish_reason=response.finish_reason,
            usage=usage,
        )
