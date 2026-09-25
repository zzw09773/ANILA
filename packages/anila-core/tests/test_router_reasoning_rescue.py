"""思考用完輸出額度時，Router 只再呼叫一次、關掉思考，把正文補回來。

主模型回 finish_reason=length 且正文是空的（或只有空白）：同一模型再打一槍，
reasoning_effort 用 none；上游拒這個參數才改走既有的 off 檔。救援失敗回到
原本的空額度說明，不會第三輪再救。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
import respx
from fastapi.testclient import TestClient

from anila_core.api import router_server as rs
from anila_core.api.events import RESCUE_REASON_REASONING_EXHAUSTED
from anila_core.api.router_server import create_router_app
from anila_core.config import settings
from anila_core.memory import close_all_connections


CSP_BASE = settings.csp_base_url
CSP_URL = f"{CSP_BASE}/v1/chat/completions"
CSP_AGENTS_URL = f"{CSP_BASE}/v1/agents"
ANSWER = "第一章先界定範圍，其餘章節可以再拆開寫。"
USER = "寫一篇十萬字、五章的論文"


def _long_reasoning() -> str:
    return "HEADMARK" + ("思" * 7000) + "TAILMARK"


class _Resp:
    def __init__(self, payload: dict | None = None, status: int = 200, text: str | None = None):
        self.status_code = status
        self._payload = payload or {}
        self.text = text if text is not None else json.dumps(self._payload)

    def raise_for_status(self):
        if self.status_code < 400:
            return None
        request = httpx.Request("POST", CSP_URL)
        response = httpx.Response(self.status_code, text=self.text, request=request)
        raise httpx.HTTPStatusError("upstream", request=request, response=response)

    def json(self):
        return self._payload


class _StreamResp:
    def __init__(self, lines: list[str] | None = None, status: int = 200, body: bytes = b""):
        self.status_code = status
        self._lines = lines or []
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self):
        return self._body


class _Client:
    def __init__(self, answers=None, streams=None):
        self.posts: list[dict] = []
        self.streams: list[dict] = []
        self._answers = list(answers or [])
        self._streams = list(streams or [])

    async def post(self, url, json=None, headers=None):
        self.posts.append(json)
        return self._answers.pop(0)

    def stream(self, method, url, json=None, headers=None):
        self.streams.append(json)
        return self._streams.pop(0)


def _reply(content: str, finish: str = "stop", reasoning: str = "") -> _Resp:
    message: dict = {"role": "assistant", "content": content}
    if reasoning:
        message["reasoning_content"] = reasoning
    return _Resp(
        {
            "choices": [
                {"index": 0, "message": message, "finish_reason": finish}
            ],
            "usage": {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8},
        }
    )


def _stream_lines(content: str = "", finish: str = "stop", reasoning: str = "") -> list[str]:
    out: list[str] = []
    if reasoning:
        out.append(
            "data: "
            + json.dumps({"choices": [{"delta": {"reasoning_content": reasoning}}]})
        )
    if content:
        out.append(
            "data: " + json.dumps({"choices": [{"delta": {"content": content}}]})
        )
    out.append(
        "data: " + json.dumps({"choices": [{"delta": {}, "finish_reason": finish}]})
    )
    out.append("data: [DONE]")
    return out


def _assert_rescue_prompt(payload: dict, reasoning: str) -> None:
    assert payload.get("reasoning_effort") == "none"
    assert payload["chat_template_kwargs"]["enable_thinking"] is False
    assert "anila_thinking_tier" not in payload
    blob = json.dumps(payload["messages"], ensure_ascii=False)
    assert USER in blob
    last = payload["messages"][-1]
    assert last["role"] == "user"
    assert "直接寫出答案" in last["content"]
    assert "按章節" in last["content"]
    tail = reasoning[-rs._RESCUE_REASONING_TAIL_CHARS :]
    assert tail in last["content"]
    assert "HEADMARK" not in last["content"]
    assert len(tail) == rs._RESCUE_REASONING_TAIL_CHARS


def test_non_stream_rescue_bounds_reasoning_and_disables_thinking(monkeypatch):
    reasoning = _long_reasoning()
    client = _Client(answers=[_reply("  \n", "length", reasoning), _reply(ANSWER)])
    monkeypatch.setattr(rs, "get_http_client", lambda: client)
    token = rs.REQUEST_THINKING_TIER.set("deep")
    try:
        result = asyncio.run(
            rs._call_llm_non_stream(
                "sk",
                [{"role": "user", "content": USER}],
                apply_thinking_tier=True,
                rescue_empty_length=True,
            )
        )
    finally:
        rs.REQUEST_THINKING_TIER.reset(token)
    assert result["content"] == ANSWER
    assert result["rescued"] is True
    assert result["reasoning"] == reasoning
    assert len(client.posts) == 2
    assert client.posts[0]["anila_thinking_tier"] == "deep"
    assert client.posts[0]["model"] == client.posts[1]["model"]
    _assert_rescue_prompt(client.posts[1], reasoning)


def test_stream_rescue_emits_event_then_answer(monkeypatch):
    reasoning = _long_reasoning()
    client = _Client(
        streams=[
            _StreamResp(_stream_lines("", "length", reasoning)),
            _StreamResp(_stream_lines(ANSWER, "stop")),
        ]
    )
    monkeypatch.setattr(rs, "get_http_client", lambda: client)

    async def run():
        return [
            ev
            async for ev in rs._stream_llm_sse(
                "sk",
                [{"role": "user", "content": USER}],
                apply_thinking_tier=True,
                rescue_empty_length=True,
            )
        ]

    events = asyncio.run(run())
    kinds = [ev["type"] for ev in events]
    assert kinds[0] == "reasoning"
    assert events[0]["content"] == reasoning
    rescue_at = kinds.index("rescue")
    assert events[rescue_at]["reason"] == RESCUE_REASON_REASONING_EXHAUSTED
    deltas = [ev["content"] for ev in events if ev["type"] == "delta"]
    assert "".join(deltas) == ANSWER
    assert kinds.index("delta") > rescue_at
    assert events[-1] == {"type": "done", "finish_reason": "stop"}
    assert len(client.streams) == 2
    _assert_rescue_prompt(client.streams[1], reasoning)


def test_double_empty_length_keeps_the_old_notice(monkeypatch):
    client = _Client(
        answers=[_reply("", "length", "只有思考"), _reply("", "length", "還是沒有")]
    )
    monkeypatch.setattr(rs, "get_http_client", lambda: client)
    result = asyncio.run(
        rs._call_llm_non_stream(
            "sk",
            [{"role": "user", "content": USER}],
            rescue_empty_length=True,
        )
    )
    assert result["content"] == ""
    assert result["error"] == rs._EMPTY_LENGTH_ERROR
    assert "rescued" not in result
    assert len(client.posts) == 2


def test_normal_answer_does_not_rescue(monkeypatch):
    client = _Client(answers=[_reply(ANSWER), _reply("不該再打")])
    monkeypatch.setattr(rs, "get_http_client", lambda: client)
    result = asyncio.run(
        rs._call_llm_non_stream(
            "sk",
            [{"role": "user", "content": USER}],
            rescue_empty_length=True,
        )
    )
    assert result["content"] == ANSWER
    assert not result.get("rescued")
    assert len(client.posts) == 1


def test_rejected_reasoning_effort_retries_once_at_off(monkeypatch):
    rejected = _Resp(
        status=400,
        text='{"error":"Unexpected reasoning effort none is not supported"}',
    )
    client = _Client(answers=[_reply("", "length", "想過了"), rejected, _reply(ANSWER)])
    monkeypatch.setattr(rs, "get_http_client", lambda: client)
    result = asyncio.run(
        rs._call_llm_non_stream(
            "sk",
            [{"role": "user", "content": USER}],
            rescue_empty_length=True,
        )
    )
    assert result["content"] == ANSWER
    assert len(client.posts) == 3
    assert client.posts[1]["reasoning_effort"] == "none"
    assert "reasoning_effort" not in client.posts[2]
    assert client.posts[2]["anila_thinking_tier"] == "off"


def test_empty_stop_still_does_not_rescue(monkeypatch):
    client = _Client(answers=[_reply("", "stop"), _reply("不該再打")])
    monkeypatch.setattr(rs, "get_http_client", lambda: client)
    result = asyncio.run(
        rs._call_llm_non_stream(
            "sk",
            [{"role": "user", "content": USER}],
            rescue_empty_length=True,
        )
    )
    assert "回覆為空" in result["error"]
    assert len(client.posts) == 1


@pytest_asyncio.fixture
async def db_path(tmp_path: Path):
    db = tmp_path / "router-rescue.db"
    yield db
    await close_all_connections()


def _sse_body(content: str = "", finish: str = "stop", reasoning: str = "") -> str:
    return "".join(line + "\n\n" for line in _stream_lines(content, finish, reasoning))


def _parse_sse(body: str) -> list[dict]:
    out: list[dict] = []
    for block in body.split("\n\n"):
        event_name = ""
        data = ""
        for line in block.splitlines():
            if line.startswith("event:"):
                event_name = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data = line.split(":", 1)[1].strip()
        if not data:
            continue
        if data == "[DONE]":
            out.append({"event": "done", "data": "[DONE]"})
            continue
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            continue
        out.append({"event": event_name or "chunk", "data": payload})
    return out


def _chunk_text(events: list[dict]) -> str:
    parts: list[str] = []
    for event in events:
        if event["event"] != "chunk":
            continue
        data = event["data"]
        if not isinstance(data, dict):
            continue
        for choice in data.get("choices") or []:
            delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
            message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
            parts.append(str(delta.get("content") or message.get("content") or ""))
    return "".join(parts)


def _completion(content: str, finish: str = "stop", reasoning: str = "") -> dict:
    message: dict = {"role": "assistant", "content": content}
    if reasoning:
        message["reasoning_content"] = reasoning
    return {
        "id": "chatcmpl-r",
        "object": "chat.completion",
        "model": "router-llm",
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": {"prompt_tokens": 4, "completion_tokens": 6, "total_tokens": 10},
    }


@respx.mock
@pytest.mark.parametrize("stream", [True, False])
def test_direct_answer_rescue_reaches_the_client(db_path: Path, stream: bool) -> None:
    reasoning = _long_reasoning()
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        seen.append(body)
        if len(seen) == 1:
            if body.get("stream"):
                return httpx.Response(
                    200,
                    content=_sse_body("", "length", reasoning).encode(),
                    headers={"Content-Type": "text/event-stream"},
                )
            return httpx.Response(200, json=_completion(" \n ", "length", reasoning))
        if body.get("stream"):
            return httpx.Response(
                200,
                content=_sse_body(ANSWER, "stop").encode(),
                headers={"Content-Type": "text/event-stream"},
            )
        return httpx.Response(200, json=_completion(ANSWER))

    respx.get(CSP_AGENTS_URL).mock(return_value=httpx.Response(200, json={"data": []}))
    respx.post(CSP_URL).mock(side_effect=handler)
    client = TestClient(create_router_app(session_db_path=str(db_path)))
    response = client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": USER}],
            "stream": stream,
            "anila_thinking_tier": "deep",
        },
        headers={"Authorization": "Bearer sk-test"},
    )
    assert response.status_code == 200, response.text
    assert len(seen) == 2
    assert seen[0]["anila_thinking_tier"] == "deep"
    assert seen[0]["model"] == seen[1]["model"]
    _assert_rescue_prompt(seen[1], reasoning)
    if stream:
        events = _parse_sse(response.text)
        rescue_at = next(i for i, ev in enumerate(events) if ev["event"] == "anila.rescue")
        reason_at = next(i for i, ev in enumerate(events) if ev["event"] == "anila.reasoning")
        assert reason_at < rescue_at
        assert events[rescue_at]["data"]["reason"] == "reasoning_exhausted"
        assert events[reason_at]["data"]["delta"] == reasoning
        text = _chunk_text(events)
        assert ANSWER in text
        assert text.index(ANSWER) > 0 or rescue_at < next(
            i for i, ev in enumerate(events) if ev["event"] == "chunk" and ANSWER in json.dumps(ev["data"], ensure_ascii=False)
        )
        assert rs._EMPTY_LENGTH_FALLBACK not in text
    else:
        body = response.json()
        assert body["choices"][0]["message"]["content"] == ANSWER
        assert body["anila_meta"]["rescue"]["reason"] == "reasoning_exhausted"
        assert body["anila_meta"]["reasoning"] == reasoning


@respx.mock
@pytest.mark.parametrize("stream", [True, False])
def test_direct_answer_double_failure_keeps_the_notice(db_path: Path, stream: bool) -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        seen.append(body)
        if body.get("stream"):
            return httpx.Response(
                200,
                content=_sse_body("", "length", "想完了").encode(),
                headers={"Content-Type": "text/event-stream"},
            )
        return httpx.Response(200, json=_completion("", "length", "想完了"))

    respx.get(CSP_AGENTS_URL).mock(return_value=httpx.Response(200, json={"data": []}))
    respx.post(CSP_URL).mock(side_effect=handler)
    client = TestClient(create_router_app(session_db_path=str(db_path)))
    response = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": USER}], "stream": stream},
        headers={"Authorization": "Bearer sk-test"},
    )
    assert response.status_code == 200, response.text
    assert len(seen) == 2
    if stream:
        assert "沒有留下正文" in response.text
        assert ANSWER not in response.text
    else:
        assert "沒有留下正文" in response.json()["choices"][0]["message"]["content"]


@respx.mock
def test_direct_answer_normal_is_one_call(db_path: Path) -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content.decode()))
        return httpx.Response(
            200,
            content=_sse_body(ANSWER, "stop", "短思考").encode(),
            headers={"Content-Type": "text/event-stream"},
        )

    respx.get(CSP_AGENTS_URL).mock(return_value=httpx.Response(200, json={"data": []}))
    respx.post(CSP_URL).mock(side_effect=handler)
    client = TestClient(create_router_app(session_db_path=str(db_path)))
    response = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": USER}], "stream": True},
        headers={"Authorization": "Bearer sk-test"},
    )
    assert response.status_code == 200, response.text
    assert len(seen) == 1
    assert "anila.rescue" not in response.text
    assert ANSWER in response.text


def _pause_ask(client: TestClient, session_id: str) -> str:
    first = client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": USER}],
            "stream": False,
            "session_id": session_id,
        },
        headers={"Authorization": "Bearer sk-test"},
    )
    assert first.status_code == 200, first.text
    state = client.get(
        f"/v1/sessions/{session_id}/state",
        headers={"Authorization": "Bearer sk-test"},
    )
    assert state.status_code == 200, state.text
    return state.json()["pending_interrupts"][0]["id"]


@respx.mock
@pytest.mark.parametrize("stream", [True, False])
def test_ask_resume_rescue_reaches_the_client(db_path: Path, stream: bool) -> None:
    reasoning = _long_reasoning()
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        seen.append(body)
        if len(seen) == 1:
            return httpx.Response(200, json=_completion("ASK:要先寫哪一章？|第一章|目錄"))
        if len(seen) == 2:
            if body.get("stream"):
                return httpx.Response(
                    200,
                    content=_sse_body("", "length", reasoning).encode(),
                    headers={"Content-Type": "text/event-stream"},
                )
            return httpx.Response(200, json=_completion("", "length", reasoning))
        if body.get("stream"):
            return httpx.Response(
                200,
                content=_sse_body(ANSWER, "stop").encode(),
                headers={"Content-Type": "text/event-stream"},
            )
        return httpx.Response(200, json=_completion(ANSWER))

    respx.get(CSP_AGENTS_URL).mock(return_value=httpx.Response(200, json={"data": []}))
    respx.post(CSP_URL).mock(side_effect=handler)
    client = TestClient(create_router_app(session_db_path=str(db_path)))
    interrupt_id = _pause_ask(client, "s-rescue")
    resumed = client.post(
        "/v1/sessions/s-rescue/answer",
        json={
            "interrupt_id": interrupt_id,
            "answer": {"selected": ["第一章"], "other_text": ""},
            "stream": stream,
            "anila_thinking_tier": "deep",
        },
        headers={"Authorization": "Bearer sk-test"},
    )
    assert resumed.status_code == 200, resumed.text
    assert len(seen) == 3
    resume_calls = seen[1:]
    assert resume_calls[0].get("anila_thinking_tier") == "deep"
    assert resume_calls[0]["model"] == resume_calls[1]["model"]
    _assert_rescue_prompt(resume_calls[1], reasoning)
    events = _parse_sse(resumed.text)
    assert events[0]["event"] == "anila.resumed"
    rescue_at = next(i for i, ev in enumerate(events) if ev["event"] == "anila.rescue")
    assert events[rescue_at]["data"]["reason"] == "reasoning_exhausted"
    assert ANSWER in _chunk_text(events)
    assert "沒有留下正文" not in resumed.text
