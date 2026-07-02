"""OpenAI-compatible provider adapter.

Connects to any endpoint that implements the OpenAI Chat Completions API
(OpenAI, vLLM, LiteLLM, Ollama, etc.).

Thinking/reasoning content strips happen here, not in the engine, to keep
the core message model clean.
"""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator, Optional

import httpx

from ..models.message import AssistantMessage, StreamDelta, ToolCallDelta, Usage, UserMessage
from .base import ProviderRequest

logger = logging.getLogger(__name__)


def _messages_to_openai(messages: list) -> list[dict[str, Any]]:
    """Convert internal Message objects to OpenAI API format."""
    result: list[dict[str, Any]] = []
    for msg in messages:
        if isinstance(msg, UserMessage):
            content = msg.content
            if isinstance(content, str):
                result.append({"role": "user", "content": content})
            else:
                # Convert block format to OpenAI format.
                # tool_result blocks → individual "tool" role messages (one per block).
                # text blocks → "user" role message.
                openai_content = []
                for block in content:
                    if block.get("type") == "text":
                        openai_content.append({"type": "text", "text": block["text"]})
                    elif block.get("type") == "tool_result":
                        # Flush any accumulated text content first
                        if openai_content:
                            result.append({"role": "user", "content": openai_content})
                            openai_content = []
                        result.append({
                            "role": "tool",
                            "tool_call_id": block.get("tool_use_id", ""),
                            "content": block.get("content", ""),
                        })
                if openai_content:
                    result.append({"role": "user", "content": openai_content})
        elif isinstance(msg, AssistantMessage):
            out: dict[str, Any] = {"role": "assistant"}
            text = msg.get_text()
            if text:
                out["content"] = text
            else:
                out["content"] = None
            if msg.tool_calls:
                out["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.input),
                        },
                    }
                    for tc in msg.tool_calls
                ]
            result.append(out)
    return result


class OpenAICompatProvider:
    """Provider adapter for OpenAI-compatible REST endpoints."""

    def __init__(
        self,
        base_url: str,
        api_key: str = "not-set",
        timeout: float = 120.0,
        extra_headers: Optional[dict[str, str]] = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            **(extra_headers or {}),
        }
        self._timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                headers=self._headers,
                timeout=self._timeout,
            )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def stream_completion(
        self, request: ProviderRequest
    ) -> AsyncIterator[StreamDelta]:
        client = await self._get_client()
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": [],
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "stream": True,
        }

        if request.system:
            payload["messages"].append({"role": "system", "content": request.system})

        payload["messages"].extend(_messages_to_openai(request.messages))

        if request.tools:
            payload["tools"] = request.tools
            payload["tool_choice"] = "auto"

        async for delta in self._stream(client, payload):
            yield delta

    async def _stream(
        self, client: httpx.AsyncClient, payload: dict[str, Any]
    ) -> AsyncIterator[StreamDelta]:
        partial_tool_calls: dict[int, dict[str, Any]] = {}
        usage_data: Optional[Usage] = None
        finish_reason = "end_turn"

        async with client.stream("POST", "/chat/completions", json=payload) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                line = line.strip()
                if not line or not line.startswith("data: "):
                    continue
                data_str = line[6:]
                if data_str == "[DONE]":
                    break
                try:
                    data = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                # Extract usage if present
                if "usage" in data and data["usage"]:
                    u = data["usage"]
                    usage_data = Usage(
                        input_tokens=u.get("prompt_tokens", 0),
                        output_tokens=u.get("completion_tokens", 0),
                    )

                choices = data.get("choices", [])
                if not choices:
                    continue
                choice = choices[0]
                finish = choice.get("finish_reason")
                if finish:
                    finish_reason = finish

                delta = choice.get("delta", {})

                # Text content
                content = delta.get("content")
                if content:
                    yield StreamDelta(type="text", text=content)

                # Tool calls
                for tc_delta in delta.get("tool_calls", []):
                    idx = tc_delta.get("index", 0)
                    if idx not in partial_tool_calls:
                        partial_tool_calls[idx] = {
                            "id": tc_delta.get("id", ""),
                            "name": "",
                            "input_partial": "",
                        }
                    if tc_delta.get("id"):
                        partial_tool_calls[idx]["id"] = tc_delta["id"]
                    fn = tc_delta.get("function", {})
                    if fn.get("name"):
                        partial_tool_calls[idx]["name"] = fn["name"]
                    if fn.get("arguments"):
                        partial_tool_calls[idx]["input_partial"] += fn["arguments"]
                        yield StreamDelta(
                            type="tool_call",
                            tool_call=ToolCallDelta(
                                id=partial_tool_calls[idx]["id"],
                                name=partial_tool_calls[idx]["name"],
                                input_partial=fn["arguments"],
                            ),
                        )

        yield StreamDelta(
            type="stop",
            finish_reason=finish_reason,
            usage=usage_data or Usage(),
        )
