"""S1-S4 coverage for the registered-agent short-reply observation."""

from __future__ import annotations

import asyncio
import json
import logging

import pytest

from app.api import proxy as proxy_api
from app.services import proxy_service
from app.services.agent_reply_signal import (
    AGENT_REPLY_OBSERVATION_KEY,
    attach_agent_reply_observation,
)
from app.services.proxy import service as proxy_impl
from app.services.proxy.service import ProxyTuning


_PROXY_TUNING = ProxyTuning.from_registry_defaults()


class _FakeStreamResponse:
    def __init__(self, lines: list[str], status_code: int = 200):
        self._lines = lines
        self.status_code = status_code

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _FakeStreamClient:
    def __init__(self, lines: list[str], *args, **kwargs):
        self._lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def stream(self, method: str, url: str, json: dict, headers: dict):
        return _FakeStreamResponse(self._lines)


def _meta_frames(chunks: list[str]) -> list[dict]:
    frames = []
    for block in "".join(chunks).split("\n\n"):
        if "event: anila.meta" not in block:
            continue
        data = next(
            (line[5:].strip() for line in block.splitlines() if line.startswith("data:")),
            None,
        )
        if data and data != "[DONE]":
            frames.append(json.loads(data))
    return frames


@pytest.fixture(autouse=True)
def _agent_stream_guards(monkeypatch):
    monkeypatch.setattr(proxy_impl, "_guard_outbound", lambda *a, **k: None)
    monkeypatch.setattr(proxy_impl, "build_agent_headers", lambda **_k: {})


@pytest.mark.parametrize(
    ("completion_tokens", "short_reply"),
    [(121, True), (219, False)],
)
def test_streaming_agent_observation_rewrites_terminal_meta_without_changing_usage(
    monkeypatch, completion_tokens: int, short_reply: bool
):
    recorded: list[dict] = []
    lines = [
        'data: {"choices":[{"index":0,"delta":{"content":"原文"},"finish_reason":null}]}',
        "",
        (
            'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}],'
            f'"usage":{{"prompt_tokens":11,"completion_tokens":{completion_tokens},'
            f'"total_tokens":{completion_tokens + 11}}}}}'
        ),
        "",
        "event: anila.meta",
        'data: {"trace_id":"agent-trace","trace":[]}',
        "",
        "data: [DONE]",
        "",
    ]
    monkeypatch.setattr(
        proxy_service.httpx,
        "AsyncClient",
        lambda *args, **kwargs: _FakeStreamClient(lines, *args, **kwargs),
    )

    async def fake_enqueue(**kwargs):
        recorded.append(kwargs)

    monkeypatch.setattr(proxy_impl, "enqueue_usage", fake_enqueue)

    async def run():
        return [
            chunk
            async for chunk in proxy_service.proxy_stream(
                target_url="http://agent/v1/chat/completions",
                api_key_id=1,
                user_id=2,
                department_id=None,
                usage_model_id=3,
                request_body={
                    "model": "registered-agent",
                    "messages": [{"role": "user", "content": "長問題"}],
                    "stream": True,
                },
                model_name="registered-agent",
                target_agent_id=7,
                tuning=_PROXY_TUNING,
            )
        ]

    chunks = asyncio.run(run())
    frames = _meta_frames(chunks)
    assert len(frames) == 1
    observation = frames[0][AGENT_REPLY_OBSERVATION_KEY]
    assert observation["completion_tokens"] == completion_tokens
    assert observation["usage_source"] == "reported"
    assert ("short_reply" in observation) is short_reply
    assert recorded[0]["completion_tokens"] == completion_tokens
    assert recorded[0]["total_tokens"] == 11 + completion_tokens
    assert "原文" in "".join(chunks)
    assert "data: [DONE]" in "".join(chunks)


def test_streaming_agent_observation_marks_estimated_short_reply(monkeypatch):
    lines = [
        'data: {"choices":[{"delta":{"content":"OK"},"finish_reason":"stop"}]}',
        "",
        "event: anila.meta",
        'data: {"trace_id":"estimated-trace"}',
        "",
        "data: [DONE]",
        "",
    ]
    monkeypatch.setattr(
        proxy_service.httpx,
        "AsyncClient",
        lambda *args, **kwargs: _FakeStreamClient(lines, *args, **kwargs),
    )
    monkeypatch.setattr(proxy_impl, "enqueue_usage", lambda **_kwargs: asyncio.sleep(0))

    async def run():
        return [
            chunk
            async for chunk in proxy_service.proxy_stream(
                target_url="http://agent/v1/chat/completions",
                api_key_id=1,
                user_id=2,
                department_id=None,
                usage_model_id=3,
                request_body={"model": "registered-agent", "messages": []},
                model_name="registered-agent",
                target_agent_id=7,
                tuning=_PROXY_TUNING,
            )
        ]

    frames = _meta_frames(asyncio.run(run()))
    observation = frames[0][AGENT_REPLY_OBSERVATION_KEY]
    assert observation["usage_source"] == "estimated"
    assert observation["completion_tokens"] <= 160
    assert observation["short_reply"] is True


def test_non_streaming_agent_payload_hook_records_reported_and_estimated_observations():
    payloads = [
        {
            "choices": [{"message": {"role": "assistant", "content": "短答"}}],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 121,
                "total_tokens": 131,
            },
        },
        {
            "choices": [
                {"message": {"role": "assistant", "content": "normal " * 200}}
            ]
        },
    ]

    results = [
        proxy_api._annotate_agent_reply_payload(payload, "agent-signal-target")[
            "anila_meta"
        ][AGENT_REPLY_OBSERVATION_KEY]
        for payload in payloads
    ]

    assert results[0] == {
        "completion_tokens": 121,
        "usage_source": "reported",
        "short_reply": True,
    }
    assert results[1]["usage_source"] == "estimated"
    assert results[1]["completion_tokens"] > 160
    assert "short_reply" not in results[1]


def test_unavailable_usage_source_never_claims_short_reply():
    """串流中斷、上游沒給 usage：長度量不到，就不下短回覆的判斷。"""
    metadata = attach_agent_reply_observation(
        {},
        completion_tokens=0,
        usage_source="unavailable",
    )

    assert metadata[AGENT_REPLY_OBSERVATION_KEY] == {
        "completion_tokens": 0,
        "usage_source": "unavailable",
    }


def test_observation_failure_drops_flag_without_raising(caplog):
    metadata = {
        AGENT_REPLY_OBSERVATION_KEY: {
            "completion_tokens": 121,
            "usage_source": "reported",
            "short_reply": True,
        }
    }
    with caplog.at_level(logging.ERROR, logger="app.services.agent_reply_signal"):
        attach_agent_reply_observation(
            metadata,
            completion_tokens=-1,
            usage_source="reported",
        )
    assert AGENT_REPLY_OBSERVATION_KEY not in metadata
    assert "agent reply observation construction failed" in caplog.text
