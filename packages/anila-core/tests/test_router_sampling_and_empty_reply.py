"""Router upstream calls carry sampling parameters and survive empty replies.

Harness §6-5 / §9b-2 (wired 2026-09-02). Before: the Router sent only
``model / messages / stream`` upstream, so temperature and max_tokens were
whatever the model server defaulted to; a thinking model that spent its whole
budget on reasoning came back ``finish_reason=length`` with empty content and
the user saw a blank answer.

Invariants:
  1. every upstream call carries ``temperature`` and ``max_tokens`` from the
     ``router`` row of the sampling table;
  2. a caller-supplied ``temperature`` / ``max_tokens`` on the inbound request
     wins over the table;
  3. non-stream: ``finish_reason=length`` with empty content → one retry with a
     doubled ``max_tokens``; still empty → ``error`` set (never a silent "");
  4. stream: same rule — an empty ``length``-terminated stream is retried once;
  5. whichever of those two the Router filled in from the table is listed in
     ``anila_sampling_defaults`` so the CSP proxy can let the per-model
     governance knobs override them (a caller value is never listed).
  6. ``length`` with partial content auto-continues up to
     ``LENGTH_AUTO_CONTINUE_ROUNDS`` instead of stopping for a button click.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from anila_core.api import router_server as rs
from anila_core.prompts.sampling import get_sampling
from anila_core.providers.guards import bumped_max_tokens

ROUTER = get_sampling("router")


class _Resp:
    def __init__(self, payload: dict, status: int = 200):
        self._payload = payload
        self.status_code = status
        self.text = json.dumps(payload)

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _StreamResp:
    def __init__(self, lines: list[str]):
        self.status_code = 200
        self._lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self):
        return b""


class _Client:
    """Records every upstream payload; answers from a scripted queue."""

    def __init__(self, answers=None, streams=None):
        self.posts: list[dict] = []
        self.streams: list[dict] = []
        self._answers = list(answers or [])
        self._streams = list(streams or [])

    async def post(self, url, json=None, headers=None):
        self.posts.append(json)
        return _Resp(self._answers.pop(0))

    def stream(self, method, url, json=None, headers=None):
        self.streams.append(json)
        return _StreamResp(self._streams.pop(0))


def _reply(content: str, finish: str = "stop") -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": finish}]}


def _stream_lines(content: str, finish: str = "stop") -> list[str]:
    out = []
    if content:
        out.append("data: " + json.dumps({"choices": [{"delta": {"content": content}}]}))
    out.append("data: " + json.dumps({"choices": [{"delta": {}, "finish_reason": finish}]}))
    out.append("data: [DONE]")
    return out


def test_router_row_exists_in_sampling_table():
    assert 0 < ROUTER.temperature < 1
    # Claude-style: the user never sets max_tokens. The harness default
    # must be large enough for a thinking model + a long HTML artifact.
    assert ROUTER.max_tokens >= 16384


def test_non_stream_call_carries_table_sampling(monkeypatch):
    client = _Client(answers=[_reply("答")])
    monkeypatch.setattr(rs, "get_http_client", lambda: client)
    result = asyncio.run(rs._call_llm_non_stream("sk", [{"role": "user", "content": "q"}]))
    assert result["content"] == "答"
    sent = client.posts[0]
    assert sent["temperature"] == ROUTER.temperature
    assert sent["max_tokens"] == ROUTER.max_tokens


def test_stream_call_carries_table_sampling(monkeypatch):
    client = _Client(streams=[_stream_lines("答")])
    monkeypatch.setattr(rs, "get_http_client", lambda: client)

    async def run():
        return [ev async for ev in rs._stream_llm_sse("sk", [{"role": "user", "content": "q"}])]

    events = asyncio.run(run())
    assert any(ev.get("type") == "delta" and ev["content"] == "答" for ev in events)
    assert events[-1] == {"type": "done", "finish_reason": "stop"}
    assert client.streams[0]["temperature"] == ROUTER.temperature
    assert client.streams[0]["max_tokens"] == ROUTER.max_tokens


def test_caller_supplied_sampling_wins(monkeypatch):
    client = _Client(answers=[_reply("答")])
    monkeypatch.setattr(rs, "get_http_client", lambda: client)

    async def run():
        token = rs.REQUEST_SAMPLING.set(rs.sampling_overrides_from_body({"temperature": 0.7, "max_tokens": 512, "stream": False}))
        try:
            return await rs._call_llm_non_stream("sk", [{"role": "user", "content": "q"}])
        finally:
            rs.REQUEST_SAMPLING.reset(token)

    asyncio.run(run())
    assert client.posts[0]["temperature"] == 0.7
    assert client.posts[0]["max_tokens"] == 512


def test_sampling_overrides_ignore_garbage():
    assert rs.sampling_overrides_from_body({"temperature": "hot", "max_tokens": -5}) == {}
    assert rs.sampling_overrides_from_body({"temperature": 2.5}) == {}
    assert rs.sampling_overrides_from_body({"max_tokens": 300}) == {"max_tokens": 300}
    assert rs.sampling_overrides_from_body({"top_p": 1.5, "presence_penalty": 9}) == {}


def test_sampling_overrides_forward_top_p_and_presence_penalty():
    assert rs.sampling_overrides_from_body(
        {"top_p": 0.95, "presence_penalty": 1.5, "temperature": 0.6}
    ) == {"top_p": 0.95, "presence_penalty": 1.5, "temperature": 0.6}


def test_router_defaults_are_flagged_for_the_csp_proxy():
    params = rs._sampling_payload()
    assert params["temperature"] == ROUTER.temperature
    assert params["max_tokens"] == ROUTER.max_tokens
    assert params[rs.SAMPLING_DEFAULTS_MARKER] == ["temperature", "max_tokens"]


def test_caller_temperature_leaves_only_max_tokens_flagged():
    token = rs.REQUEST_SAMPLING.set(rs.sampling_overrides_from_body({"temperature": 0.7}))
    try:
        params = rs._sampling_payload()
    finally:
        rs.REQUEST_SAMPLING.reset(token)
    assert params["temperature"] == 0.7
    assert params["max_tokens"] == ROUTER.max_tokens
    assert params[rs.SAMPLING_DEFAULTS_MARKER] == ["max_tokens"]


def test_caller_supplying_both_sends_no_marker():
    token = rs.REQUEST_SAMPLING.set(
        rs.sampling_overrides_from_body({"temperature": 0.7, "max_tokens": 512})
    )
    try:
        params = rs._sampling_payload()
    finally:
        rs.REQUEST_SAMPLING.reset(token)
    assert params["temperature"] == 0.7
    assert params["max_tokens"] == 512
    assert rs.SAMPLING_DEFAULTS_MARKER not in params


def test_length_retry_bump_is_not_a_router_default():
    """The doubled budget is a deliberate Router decision, so the per-model
    max_tokens must not be allowed to undo it."""
    params = rs._sampling_payload(max_tokens_override=8192)
    assert params["max_tokens"] == 8192
    assert params[rs.SAMPLING_DEFAULTS_MARKER] == ["temperature"]


def test_non_stream_empty_length_reply_is_retried_with_doubled_budget(monkeypatch):
    client = _Client(answers=[_reply("", "length"), _reply("這次有內容")])
    monkeypatch.setattr(rs, "get_http_client", lambda: client)
    result = asyncio.run(rs._call_llm_non_stream("sk", [{"role": "user", "content": "q"}]))
    assert result["content"] == "這次有內容"
    assert result["error"] is None
    assert len(client.posts) == 2
    assert client.posts[1]["max_tokens"] == bumped_max_tokens(client.posts[0]["max_tokens"])


def test_non_stream_empty_twice_is_an_error_not_a_blank_answer(monkeypatch):
    client = _Client(answers=[_reply("", "length"), _reply("", "length")])
    monkeypatch.setattr(rs, "get_http_client", lambda: client)
    result = asyncio.run(rs._call_llm_non_stream("sk", [{"role": "user", "content": "q"}]))
    assert result["content"] == ""
    assert result["error"] and "length" in result["error"]
    assert len(client.posts) == 2


def test_non_stream_empty_stop_reply_is_not_retried(monkeypatch):
    """Only the length-terminated blank is the failure mode; a genuine empty
    'stop' answer is returned as-is (no retry loop)."""
    client = _Client(answers=[_reply("", "stop")])
    monkeypatch.setattr(rs, "get_http_client", lambda: client)
    result = asyncio.run(rs._call_llm_non_stream("sk", [{"role": "user", "content": "q"}]))
    assert result["content"] == "" and result["error"] is None
    assert len(client.posts) == 1


def test_stream_empty_length_reply_is_retried_once(monkeypatch):
    client = _Client(streams=[_stream_lines("", "length"), _stream_lines("補上的內容")])
    monkeypatch.setattr(rs, "get_http_client", lambda: client)

    async def run():
        return [ev async for ev in rs._stream_llm_sse("sk", [{"role": "user", "content": "q"}])]

    events = asyncio.run(run())
    assert [ev["content"] for ev in events if ev.get("type") == "delta"] == ["補上的內容"]
    assert events[-1]["type"] == "done"
    assert events[-1].get("finish_reason") == "stop"
    assert len(client.streams) == 2
    assert client.streams[1]["max_tokens"] == bumped_max_tokens(client.streams[0]["max_tokens"])


def test_stream_length_with_content_auto_continues_until_stop(monkeypatch):
    client = _Client(
        streams=[
            _stream_lines("<!DOCTYPE html><html>", "length"),
            _stream_lines("<body>太陽系</body></html>", "stop"),
        ]
    )
    monkeypatch.setattr(rs, "get_http_client", lambda: client)

    async def run():
        return [ev async for ev in rs._stream_llm_sse("sk", [{"role": "user", "content": "q"}])]

    events = asyncio.run(run())
    deltas = [ev["content"] for ev in events if ev.get("type") == "delta"]
    assert "".join(deltas).replace("\n", "") == "<!DOCTYPE html><html><body>太陽系</body></html>"
    assert events[-1] == {"type": "done", "finish_reason": "stop"}
    assert len(client.streams) == 2
    cont = client.streams[1]["messages"]
    assert cont[-2]["role"] == "assistant"
    assert "<!DOCTYPE html>" in cont[-2]["content"]
    assert cont[-1]["role"] == "user"
    assert "接續" in cont[-1]["content"]


def test_stream_length_stops_after_auto_continue_budget(monkeypatch):
    client = _Client(
        streams=[_stream_lines("chunk", "length") for _ in range(rs.LENGTH_AUTO_CONTINUE_ROUNDS + 1)]
    )
    monkeypatch.setattr(rs, "get_http_client", lambda: client)

    async def run():
        return [ev async for ev in rs._stream_llm_sse("sk", [{"role": "user", "content": "q"}])]

    events = asyncio.run(run())
    assert events[-1] == {"type": "done", "finish_reason": "length"}
    assert len(client.streams) == rs.LENGTH_AUTO_CONTINUE_ROUNDS + 1
    assert not any(ev.get("type") == "error" for ev in events)


def test_non_stream_length_with_content_auto_continues(monkeypatch):
    client = _Client(
        answers=[
            _reply("<!DOCTYPE html><html>", "length"),
            _reply("</html>", "stop"),
        ]
    )
    monkeypatch.setattr(rs, "get_http_client", lambda: client)
    result = asyncio.run(rs._call_llm_non_stream("sk", [{"role": "user", "content": "q"}]))
    assert result["error"] is None
    assert "<!DOCTYPE html>" in result["content"]
    assert "</html>" in result["content"]
    assert len(client.posts) == 2


class _EmptyRegistry:
    def get(self, *args, **kwargs):
        return None


def _collect_router_stream(monkeypatch, fake_stream_llm):
    monkeypatch.setattr(rs, "_stream_llm_sse", fake_stream_llm)

    async def run():
        chunks = []
        async for line in rs._router_streaming(
            "sk",
            [{"role": "user", "content": "做一個太陽系頁面"}],
            [{"role": "user", "content": "做一個太陽系頁面"}],
            registry=_EmptyRegistry(),
            base_trace=[],
            started_at=0.0,
            route_signal=rs._ROUTE_DIRECT,
        ):
            chunks.append(line)
        return "".join(chunks)

    return asyncio.run(run())


def test_router_streaming_preserves_length_finish_reason(monkeypatch):
    async def fake_stream_llm(*_args, **_kwargs):
        yield {"type": "delta", "content": "<!DOCTYPE html><html><body>太陽系"}
        yield {"type": "done", "finish_reason": "length"}

    body = _collect_router_stream(monkeypatch, fake_stream_llm)
    assert "太陽系" in body
    assert "暫時無法回應" not in body
    assert '"finish_reason": "length"' in body


def test_router_streaming_empty_length_is_truncation_not_outage(monkeypatch):
    async def fake_stream_llm(*_args, **_kwargs):
        yield {
            "type": "error",
            "error": rs._EMPTY_LENGTH_ERROR,
            "detail": "finish_reason=length, empty content",
        }

    body = _collect_router_stream(monkeypatch, fake_stream_llm)
    assert "暫時無法回應" not in body
    assert "截斷" in body
    assert '"finish_reason": "length"' in body


def test_router_streaming_keeps_partial_content_on_timeout(monkeypatch):
    async def fake_stream_llm(*_args, **_kwargs):
        yield {"type": "delta", "content": "const scene = new THREE.Scene();"}
        yield {"type": "error", "error": "LLM connection: ReadTimeout", "detail": "timed out"}

    body = _collect_router_stream(monkeypatch, fake_stream_llm)
    assert "THREE.Scene" in body
    assert "暫時無法回應" not in body
    assert '"finish_reason": "length"' in body


def test_stream_empty_twice_yields_an_error_event(monkeypatch):
    client = _Client(streams=[_stream_lines("", "length"), _stream_lines("", "length")])
    monkeypatch.setattr(rs, "get_http_client", lambda: client)

    async def run():
        return [ev async for ev in rs._stream_llm_sse("sk", [{"role": "user", "content": "q"}])]

    events = asyncio.run(run())
    assert events[-1]["type"] == "error" and "length" in events[-1]["error"]
    assert len(client.streams) == 2
