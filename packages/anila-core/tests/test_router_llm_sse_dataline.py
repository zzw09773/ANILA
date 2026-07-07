"""Regression test for ``_stream_llm_sse``'s SSE ``data:`` line parsing.

Companion to ``test_router_sse_passthrough.py`` (which covers the fully
rewritten ``_stream_agent_sse`` parser, including its
``test_data_with_optional_space_after_colon`` case). ``_stream_llm_sse`` is
a separate, simpler hand-rolled parser used for the Router's own
LLM-facing stream (plan C direct-answer detection) and had the same gap:
it only recognised ``data: `` (space after colon), so a spec-legal
``data:{...}`` (no space) line was silently dropped instead of parsed.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from anila_core.api.router_server import _stream_llm_sse
from anila_core.config import settings

CSP_URL = f"{settings.csp_base_url}/v1/chat/completions"


def _sse_response(body: str) -> httpx.Response:
    return httpx.Response(
        200,
        content=body.encode("utf-8"),
        headers={"Content-Type": "text/event-stream"},
    )


@pytest.mark.asyncio
@respx.mock
async def test_data_line_without_space_after_colon_parses() -> None:
    body = (
        'data:{"choices":[{"delta":{"content":"no"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"space"}}]}\n\n'
        "data: [DONE]\n\n"
    )
    respx.post(CSP_URL).mock(return_value=_sse_response(body))

    events = [ev async for ev in _stream_llm_sse("k", [{"role": "user", "content": "hi"}])]

    deltas = [ev["content"] for ev in events if ev["type"] == "delta"]
    assert deltas == ["no", "space"]
    assert events[-1] == {"type": "done"}
