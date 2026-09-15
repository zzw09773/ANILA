"""Router auto-compact: long history is summarized before the main LLM call."""

from __future__ import annotations

import asyncio
import json

import httpx

from anila_core.api import router_server as rs
from anila_core.compact.openai_history import HISTORY_SUMMARY_PREFIX
from anila_core.compact.sliding_window import SLIDING_WINDOW_SUMMARY


class _Resp:
    def __init__(self, payload: dict, status: int = 200, text: str | None = None):
        self._payload = payload
        self.status_code = status
        self.text = text if text is not None else json.dumps(payload)
        self._request = httpx.Request("POST", "http://csp/v1/chat/completions")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "overflow",
                request=self._request,
                response=httpx.Response(self.status_code, text=self.text, request=self._request),
            )

    def json(self):
        return self._payload


class _StreamResp:
    def __init__(self, lines: list[str], status: int = 200, error_body: bytes = b""):
        self.status_code = status
        self._lines = lines
        self._error_body = error_body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self):
        return self._error_body


class _Client:
    def __init__(self, answers=None, streams=None):
        self.posts: list[dict] = []
        self.streams: list[dict] = []
        self._answers = list(answers or [])
        self._streams = list(streams or [])

    async def post(self, url, json=None, headers=None):
        self.posts.append(json)
        item = self._answers.pop(0)
        return item if isinstance(item, _Resp) else _Resp(item)

    def stream(self, method, url, json=None, headers=None):
        self.streams.append(json)
        return self._streams.pop(0)


def _reply(content: str) -> dict:
    return {
        "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}]
    }


def _stream_ok(content: str) -> _StreamResp:
    return _StreamResp(
        [
            "data: " + json.dumps({"choices": [{"delta": {"content": content}}]}),
            "data: " + json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
            "data: [DONE]",
        ]
    )


def _long_messages(turns: int = 8, size: int = 500) -> list[dict]:
    out = [{"role": "system", "content": "router"}]
    for i in range(turns):
        out.append({"role": "user", "content": f"u{i} " + ("問" * size)})
        out.append({"role": "assistant", "content": f"a{i} " + ("答" * size)})
    out.append({"role": "user", "content": "最新一問"})
    return out


def test_routing_compact_summarizes_before_main_call(monkeypatch):
    monkeypatch.setattr(rs, "current_router_context_window", lambda: 2_400)

    async def fake_summary(key, old, headers):
        assert any("u0" in str(m.get("content")) for m in old)
        return "先前在做太陽系頁"

    monkeypatch.setattr(rs, "_summarize_for_compact", fake_summary)
    client = _Client(answers=[_reply("好")])
    monkeypatch.setattr(rs, "get_http_client", lambda: client)

    compacted, step = asyncio.run(
        rs._auto_compact_routing_messages(
            _long_messages(),
            caller_api_key="sk",
            forwarded_headers=None,
        )
    )
    assert step is not None
    assert step["kind"] == "compact"
    assert HISTORY_SUMMARY_PREFIX in compacted[1]["content"]
    assert compacted[-1]["content"] == "最新一問"
    assert not any(str(m.get("content", "")).startswith("u0") for m in compacted)

    result = asyncio.run(rs._call_llm_non_stream("sk", compacted))
    assert result["content"] == "好"
    assert HISTORY_SUMMARY_PREFIX in client.posts[0]["messages"][1]["content"]


def test_prompt_too_long_retries_after_hard_trim(monkeypatch):
    monkeypatch.setattr(rs, "current_router_context_window", lambda: 1_000_000)
    client = _Client(
        answers=[
            _Resp(
                {"error": {"code": "context_length_exceeded"}},
                status=400,
                text='{"error":{"code":"context_length_exceeded"}}',
            ),
            _reply("縮過了"),
        ]
    )
    monkeypatch.setattr(rs, "get_http_client", lambda: client)
    result = asyncio.run(rs._call_llm_non_stream("sk", _long_messages(6, size=80)))
    assert result["content"] == "縮過了"
    assert result.get("error") is None
    assert len(client.posts) == 2
    second = client.posts[1]["messages"]
    assert any(SLIDING_WINDOW_SUMMARY in str(m.get("content")) for m in second)
    assert second[-1]["content"] == "最新一問"


def _ptl_resp() -> _Resp:
    return _Resp(
        {"error": {"code": "context_length_exceeded"}},
        status=400,
        text='{"error":{"code":"context_length_exceeded"}}',
    )


def _messages_with_old_images(turns: int = 6, size: int = 80) -> list[dict]:
    big = "G" * 4000
    out: list[dict] = [{"role": "system", "content": "router"}]
    for i in range(turns):
        out.append({
            "role": "user",
            "content": [
                {"type": "text", "text": f"u{i} " + ("問" * size)},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{big}"}},
            ],
        })
        out.append({"role": "assistant", "content": f"a{i} " + ("答" * size)})
    out.append({"role": "user", "content": "最新一問"})
    return out


def _payload_blob(messages: list[dict]) -> str:
    return json.dumps(messages, ensure_ascii=False)


def test_ptl_first_retry_strips_images_only(monkeypatch):
    monkeypatch.setattr(rs, "current_router_context_window", lambda: 1_000_000)
    client = _Client(answers=[_ptl_resp(), _reply("剝過圖")])
    monkeypatch.setattr(rs, "get_http_client", lambda: client)
    result = asyncio.run(rs._call_llm_non_stream("sk", _messages_with_old_images()))
    assert result["content"] == "剝過圖"
    assert result.get("error") is None
    assert len(client.posts) == 2
    second = client.posts[1]["messages"]
    blob = _payload_blob(second)
    # PTL strip follows keep_recent_turns=2: oldest images gone, second-newest kept.
    u0 = next(m for m in second if "u0" in str(m.get("content")))
    u5 = next(m for m in second if "u5" in str(m.get("content")))
    assert "圖片已省略" in str(u0.get("content"))
    assert "data:image" not in str(u0.get("content"))
    assert "data:image" in str(u5.get("content"))
    assert not any(SLIDING_WINDOW_SUMMARY in str(m.get("content")) for m in second)
    assert any("u0" in str(m.get("content")) for m in second)
    assert second[-1]["content"] == "最新一問"
    assert "圖片已省略" in blob


def test_ptl_second_retry_hard_trims_after_strip(monkeypatch):
    monkeypatch.setattr(rs, "current_router_context_window", lambda: 1_000_000)
    client = _Client(answers=[_ptl_resp(), _ptl_resp(), _reply("硬截了")])
    monkeypatch.setattr(rs, "get_http_client", lambda: client)
    result = asyncio.run(rs._call_llm_non_stream("sk", _messages_with_old_images()))
    assert result["content"] == "硬截了"
    assert result.get("error") is None
    assert len(client.posts) == 3
    first_retry = client.posts[1]["messages"]
    u0 = next(m for m in first_retry if "u0" in str(m.get("content")))
    assert "data:image" not in str(u0.get("content"))
    assert "圖片已省略" in str(u0.get("content"))
    third = client.posts[2]["messages"]
    assert any(SLIDING_WINDOW_SUMMARY in str(m.get("content")) for m in third)
    assert third[-1]["content"] == "最新一問"


def test_ptl_third_overflow_does_not_retry(monkeypatch):
    monkeypatch.setattr(rs, "current_router_context_window", lambda: 1_000_000)
    client = _Client(answers=[_ptl_resp(), _ptl_resp(), _ptl_resp()])
    monkeypatch.setattr(rs, "get_http_client", lambda: client)
    result = asyncio.run(rs._call_llm_non_stream("sk", _messages_with_old_images()))
    assert result.get("error")
    assert len(client.posts) == 3


def test_stream_prompt_too_long_retries(monkeypatch):
    monkeypatch.setattr(rs, "current_router_context_window", lambda: 1_000_000)
    client = _Client(
        streams=[
            _StreamResp([], status=400, error_body=b'{"error":{"code":"context_overflow"}}'),
            _stream_ok("串流縮過了"),
        ]
    )
    monkeypatch.setattr(rs, "get_http_client", lambda: client)

    async def run():
        return [ev async for ev in rs._stream_llm_sse("sk", _long_messages(5, size=80))]

    events = asyncio.run(run())
    assert any(ev.get("type") == "delta" and ev["content"] == "串流縮過了" for ev in events)
    assert len(client.streams) == 2
    assert any(
        SLIDING_WINDOW_SUMMARY in str(m.get("content"))
        for m in client.streams[1]["messages"]
    )
