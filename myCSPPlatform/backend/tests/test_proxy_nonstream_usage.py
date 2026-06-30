"""P-3: non-streaming agent dispatch must write a token_usage row.

The streaming agent path attributes usage via proxy_stream; the non-stream
agent forward (proxy.py) previously emitted NO token_usage row at all, so any
``stream:false`` agent traffic was silently under-counted in the dashboards.
These tests pin that the non-stream path enqueues usage attributed to the
agent (model_id + caller_agent_id = agent id), from the payload usage or a
server-side estimate when the agent omits it.
"""

from __future__ import annotations

import asyncio

from app.api import proxy


def test_non_stream_agent_dispatch_enqueues_usage_from_payload(monkeypatch):
    recorded: list[dict] = []

    async def fake_enqueue(**kwargs):
        recorded.append(kwargs)

    monkeypatch.setattr(proxy, "enqueue_usage", fake_enqueue, raising=False)

    asyncio.run(
        proxy._enqueue_agent_dispatch_usage(
            api_key_id=11,
            user_id=2,
            department_id=3,
            agent_id=7,
            agent_name="hr-agent",
            payload={
                "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
                "choices": [{"message": {"role": "assistant", "content": "hi"}}],
            },
            request_body={"messages": [{"role": "user", "content": "q"}]},
            duration_ms=123,
            conversation_id="c1",
            trace_id="t1",
        )
    )

    assert recorded, "non-stream agent dispatch must enqueue a usage row"
    r = recorded[0]
    assert r["user_id"] == 2
    assert r["api_key_id"] == 11
    assert r["model_id"] == 7
    assert r["caller_agent_id"] == 7
    assert r["prompt_tokens"] == 10
    assert r["completion_tokens"] == 20
    assert r["total_tokens"] == 30


def test_non_stream_agent_dispatch_estimates_usage_when_missing(monkeypatch):
    recorded: list[dict] = []

    async def fake_enqueue(**kwargs):
        recorded.append(kwargs)

    monkeypatch.setattr(proxy, "enqueue_usage", fake_enqueue, raising=False)

    asyncio.run(
        proxy._enqueue_agent_dispatch_usage(
            api_key_id=None,
            user_id=2,
            department_id=None,
            agent_id=7,
            agent_name="hr-agent",
            payload={
                "choices": [
                    {"message": {"role": "assistant", "content": "a fairly long assistant answer"}}
                ]
            },
            request_body={"messages": [{"role": "user", "content": "a user question goes here"}]},
            duration_ms=50,
            conversation_id=None,
            trace_id=None,
        )
    )

    assert recorded
    r = recorded[0]
    assert r["caller_agent_id"] == 7
    assert r["prompt_tokens"] > 0
    assert r["completion_tokens"] > 0
    assert r["total_tokens"] == r["prompt_tokens"] + r["completion_tokens"]
