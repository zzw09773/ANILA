"""有正文的 length 先在同一輪自動接寫，最多三次。

仍被截斷才把 finish_reason=length 交回 Shell，讓「繼續」出現。
空正文的 length 仍走救援。使用者按繼續的那一輪走同一條 Router
（模型、身分、日期與不得外洩），但續寫正文裡的 DISPATCH／ASK／RECALL
與 STAGE 行都保持原文，不當成控制行。
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
from anila_core.api.router_prompts import DISCLOSURE_RULE_ZH
from anila_core.api.router_server import create_router_app
from anila_core.config import settings
from anila_core.memory import MemorySession, close_all_connections


CSP_URL = f"{settings.csp_base_url}/v1/chat/completions"
CSP_AGENTS_URL = f"{settings.csp_base_url}/v1/agents"


class _Resp:
    def __init__(self, payload: dict, status: int = 200):
        self._payload = payload
        self.status_code = status
        self.text = json.dumps(payload)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=None, response=self)
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
    return {
        "choices": [
            {"message": {"role": "assistant", "content": content}, "finish_reason": finish}
        ]
    }


def _stream_lines(content: str, finish: str = "stop") -> list[str]:
    out = []
    if content:
        out.append("data: " + json.dumps({"choices": [{"delta": {"content": content}}]}))
    out.append("data: " + json.dumps({"choices": [{"delta": {}, "finish_reason": finish}]}))
    out.append("data: [DONE]")
    return out


def _events(body: str) -> list[tuple[str, str]]:
    events: list[tuple[str, str]] = []
    for block in body.split("\n\n"):
        name = "message"
        data = ""
        for line in block.split("\n"):
            if line.startswith("event:"):
                name = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data = line[len("data:") :].strip()
        if data:
            events.append((name, data))
    return events


def _visible(body: str) -> tuple[str, dict | None]:
    text: list[str] = []
    meta = None
    for name, data in _events(body):
        if name == "anila.meta":
            meta = json.loads(data)
        elif name == "message" and data != "[DONE]":
            payload = json.loads(data)
            for choice in payload.get("choices") or []:
                piece = (choice.get("delta") or {}).get("content")
                if piece:
                    text.append(piece)
                reason = choice.get("finish_reason")
                if reason and meta is not None:
                    meta = {**meta, "_chunk_finish": reason}
                elif reason:
                    meta = {"_chunk_finish": reason}
    return "".join(text), meta


class _EmptyRegistry:
    def get(self, *args, **kwargs):
        return None


def _drive(monkeypatch, content: str, *, finish: str = "stop", continue_answer: bool = False) -> str:
    async def fake_stream(*_args, **_kwargs):
        yield {"type": "delta", "content": content}
        yield {"type": "done", "finish_reason": finish}

    monkeypatch.setattr(rs, "_stream_llm_sse", fake_stream)

    async def run() -> str:
        token = rs.REQUEST_CONTINUE.set(continue_answer)
        try:
            parts: list[str] = []
            async for line in rs._router_streaming(
                "sk",
                [
                    {"role": "system", "content": "系統"},
                    {"role": "user", "content": "問題"},
                ],
                [{"role": "user", "content": "問題"}],
                registry=_EmptyRegistry(),
                base_trace=[],
                started_at=0.0,
                session=MemorySession("continue"),
                route_signal=rs._ROUTE_DIRECT,
            ):
                parts.append(line)
            return "".join(parts)
        finally:
            rs.REQUEST_CONTINUE.reset(token)

    return asyncio.run(run())


def test_stream_length_with_content_auto_continues_until_stop(monkeypatch):
    client = _Client(streams=[_stream_lines("已經寫到這裡", "length"), _stream_lines("不該再打")])
    monkeypatch.setattr(rs, "get_http_client", lambda: client)

    async def run():
        return [ev async for ev in rs._stream_llm_sse("sk", [{"role": "user", "content": "q"}])]

    events = asyncio.run(run())
    assert len(client.streams) == 2
    assert events[-1] == {"type": "done", "finish_reason": "stop"}
    assert "".join(ev["content"] for ev in events if ev.get("type") == "delta") == "已經寫到這裡不該再打"
    assert not any(ev.get("type") == "error" for ev in events)


def test_non_stream_length_with_content_auto_continues_until_stop(monkeypatch):
    client = _Client(answers=[_reply("已經寫到這裡", "length"), _reply("不該再打")])
    monkeypatch.setattr(rs, "get_http_client", lambda: client)
    result = asyncio.run(rs._call_llm_non_stream("sk", [{"role": "user", "content": "q"}]))
    assert len(client.posts) == 2
    assert result["error"] is None
    assert result["content"] == "已經寫到這裡\n不該再打"
    assert result["finish_reason"] == "stop"


def test_router_streaming_puts_length_on_meta(monkeypatch):
    body = _drive(monkeypatch, "已經寫到這裡", finish="length")
    text, meta = _visible(body)
    assert text == "已經寫到這裡"
    assert meta["finish_reason"] == "length"
    assert '"finish_reason": "length"' in body


@pytest.mark.parametrize(
    "text",
    [
        "DISPATCH:weather:今天天氣如何",
        "ASK:要繼續哪一章？|第一章|第二章",
        "RECALL:上次的預算討論",
    ],
)
def test_continue_text_is_not_a_directive(monkeypatch, text):
    recalls: list = []

    async def no_recall(*_args, **_kwargs):
        recalls.append(True)
        return []

    monkeypatch.setattr(rs, "_fetch_recall_hits", no_recall)
    body = _drive(monkeypatch, text, continue_answer=True)
    visible, _meta = _visible(body)
    assert text in visible
    assert recalls == []
    assert "找不到助手" not in body
    assert "選擇 agent" not in body
    assert "anila.interrupt_requested" not in body


def test_auto_continue_joins_until_stop_and_shows_writing_stage(monkeypatch):
    client = _Client(
        streams=[
            _stream_lines("已經寫到這裡", "length"),
            _stream_lines("，後半寫完了", "stop"),
        ]
    )
    monkeypatch.setattr(rs, "get_http_client", lambda: client)

    async def run() -> str:
        parts: list[str] = []
        async for line in rs._router_streaming(
            "sk",
            [{"role": "user", "content": "寫長文"}],
            [{"role": "user", "content": "寫長文"}],
            registry=_EmptyRegistry(),
            base_trace=[],
            started_at=0.0,
            session=MemorySession("auto-continue"),
            route_signal=rs._ROUTE_DIRECT,
        ):
            parts.append(line)
        return "".join(parts)

    body = asyncio.run(run())
    assert len(client.streams) == 2
    assert "已經寫到這裡" in body
    assert "後半寫完了" in body
    titles = [
        json.loads(data).get("title")
        for name, data in _events(body)
        if name == "anila.thinking_stage"
    ]
    assert "繼續撰寫" in titles
    assert '"finish_reason": "stop"' in body


def test_auto_continue_stops_at_three_extra_rounds_still_length(monkeypatch):
    client = _Client(streams=[_stream_lines("片段", "length") for _ in range(5)])
    monkeypatch.setattr(rs, "get_http_client", lambda: client)

    async def run():
        return [ev async for ev in rs._stream_llm_sse("sk", [{"role": "user", "content": "q"}])]

    events = asyncio.run(run())
    assert len(client.streams) == 4
    assert events[-1] == {"type": "done", "finish_reason": "length"}
    assert "".join(ev.get("content", "") for ev in events if ev.get("type") == "delta") == "片段" * 4


def test_recompose_auto_continues_a_cut_rewrite(monkeypatch):
    client = _Client(
        answers=[
            _reply("前半還沒寫完", "length"),
            _reply("，補上結尾。", "stop"),
        ]
    )
    monkeypatch.setattr(rs, "get_http_client", lambda: client)
    text, status = asyncio.run(rs._recompose_reply("原始完整答案", "sk"))
    assert len(client.posts) == 2
    assert status == "applied"
    assert "前半還沒寫完" in text
    assert "補上結尾" in text


def test_recompose_still_cut_after_budget_keeps_the_original(monkeypatch):
    client = _Client(answers=[_reply("只有一半", "length") for _ in range(5)])
    monkeypatch.setattr(rs, "get_http_client", lambda: client)
    text, status = asyncio.run(rs._recompose_reply("原始完整答案", "sk"))
    assert status == "fallback"
    assert text == "原始完整答案"


def test_continue_compact_keeps_the_answer_being_resumed():
    tail = "TAILMARKER-這是必須原樣留下的續寫結尾位置"
    answer = ("前文很長。" * 400) + tail
    messages = [
        {"role": "system", "content": "系統提示"},
        {"role": "user", "content": "請寫長文"},
        {"role": "assistant", "content": answer},
        {
            "role": "user",
            "content": "請接續上文，直接從中斷處往下寫，不要重複已經寫過的內容。",
        },
    ]
    token = rs.REQUEST_CONTINUE.set(True)
    try:
        compacted = asyncio.run(
            rs._messages_after_prompt_too_long(
                400,
                "context_length_exceeded",
                messages,
                already=0,
            )
        )
    finally:
        rs.REQUEST_CONTINUE.reset(token)
    assert compacted is not None
    assistants = [
        row.get("content")
        for row in compacted
        if row.get("role") == "assistant"
    ]
    assert any(isinstance(text, str) and tail in text for text in assistants)


def test_continue_keeps_stage_lines_in_the_answer(monkeypatch):
    body = _drive(
        monkeypatch,
        "STAGE: 這是範例不是階段\n後半從這裡開始。",
        continue_answer=True,
    )
    visible, _meta = _visible(body)
    assert "STAGE: 這是範例不是階段" in visible
    titles = [
        json.loads(data).get("title")
        for name, data in _events(body)
        if name == "anila.thinking_stage"
    ]
    assert "這是範例不是階段" not in titles


def test_continue_non_stream_keeps_stage_lines_in_the_answer():
    live = rs.LiveThinkingStages()
    stage_token = rs._REQUEST_STAGES.set(live)
    continue_token = rs.REQUEST_CONTINUE.set(True)
    try:
        text = rs._absorb_llm_turn(
            {"reasoning": "", "content": "STAGE: 範例程式\nprint(1)"},
            "STAGE: 範例程式\nprint(1)",
        )
    finally:
        rs.REQUEST_CONTINUE.reset(continue_token)
        rs._REQUEST_STAGES.reset(stage_token)
    assert "STAGE: 範例程式" in text
    assert all(item.get("title") != "範例程式" for item in live.snapshot())


def test_continue_does_not_promote_example_stage_lines(monkeypatch):
    body = _drive(
        monkeypatch,
        "STAGE: 接寫後半\n後半從這裡開始。",
        continue_answer=True,
    )
    visible, meta = _visible(body)
    assert "STAGE: 接寫後半" in visible
    assert "後半從這裡開始。" in visible
    titles = [
        json.loads(data).get("title")
        for name, data in _events(body)
        if name == "anila.thinking_stage"
    ]
    assert "接寫後半" not in titles
    assert isinstance(meta, dict)


@pytest_asyncio.fixture
async def db_path(tmp_path: Path):
    yield tmp_path / "router-continue.db"
    await close_all_connections()


def _agents_payload() -> dict:
    return {
        "data": [
            {
                "id": "image-generator",
                "name": "Image Generator",
                "description_for_router": "Draws pictures",
                "endpoint_url": "http://image-generator",
                "requires_encryption": False,
            }
        ]
    }


def _sse(content: str, finish: str = "stop") -> httpx.Response:
    raw = (
        "data: "
        + json.dumps({"choices": [{"delta": {"content": content}}]}, ensure_ascii=False)
        + "\n\n"
        + "data: "
        + json.dumps({"choices": [{"delta": {}, "finish_reason": finish}]}, ensure_ascii=False)
        + "\n\n"
        + "data: [DONE]\n\n"
    )
    return httpx.Response(
        200,
        content=raw.encode("utf-8"),
        headers={"Content-Type": "text/event-stream"},
    )


class _Seen:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def record(self, request: httpx.Request) -> None:
        payload = json.loads(request.content)
        messages = payload.get("messages") or []
        system = messages[0].get("content", "") if messages else ""
        if payload.get("model") != rs.current_router_model():
            kind = "dispatch"
        else:
            kind = "router-llm"
        self.calls.append({"kind": kind, "payload": payload, "system": system})

    def of(self, kind: str) -> list[dict]:
        return [row for row in self.calls if row["kind"] == kind]


@respx.mock
def test_continue_request_uses_router_prompt_and_ignores_dispatch(db_path: Path):
    seen = _Seen()

    def handler(request: httpx.Request) -> httpx.Response:
        seen.record(request)
        payload = json.loads(request.content)
        if payload.get("model") != rs.current_router_model():
            return _sse("不該派去畫圖")
        return _sse("DISPATCH:image-generator:畫一張圖")

    respx.get(CSP_AGENTS_URL).mock(return_value=httpx.Response(200, json=_agents_payload()))
    respx.post(CSP_URL).mock(side_effect=handler)
    client = TestClient(create_router_app(session_db_path=str(db_path)))
    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer sk-test"},
        json={
            "model": "client-picked",
            "stream": True,
            "anila_continue": True,
            "messages": [
                {"role": "user", "content": "寫長文"},
                {"role": "assistant", "content": "第一章在太陽系的形成過程被截斷"},
                {
                    "role": "user",
                    "content": "請接續上文，直接從中斷處往下寫，不要重複已經寫過的內容。",
                },
            ],
        },
    )
    assert response.status_code == 200, response.text
    body = response.text
    assert len(seen.of("router-llm")) == 1
    assert seen.of("dispatch") == []
    sent = seen.of("router-llm")[0]
    assert sent["payload"]["model"] == rs.current_router_model()
    assert "max_tokens" in (sent["payload"].get(rs.SAMPLING_DEFAULTS_MARKER) or [])
    assert DISCLOSURE_RULE_ZH in sent["system"]
    assert "今天是" in sent["system"]
    assert "STAGE:" in sent["system"]
    assert any(
        row.get("role") == "assistant" and "被截斷" in str(row.get("content") or "")
        for row in sent["payload"]["messages"]
    )
    assert "DISPATCH:image-generator:畫一張圖" in body
    assert "不該派去畫圖" not in body


@respx.mock
def test_length_answer_reaches_the_client_meta(db_path: Path):
    seen = _Seen()

    def handler(request: httpx.Request) -> httpx.Response:
        seen.record(request)
        return _sse("已經寫到這裡", "length")

    respx.get(CSP_AGENTS_URL).mock(return_value=httpx.Response(200, json={"data": []}))
    respx.post(CSP_URL).mock(side_effect=handler)
    client = TestClient(create_router_app(session_db_path=str(db_path)))
    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer sk-test"},
        json={
            "stream": True,
            "messages": [{"role": "user", "content": "寫長文"}],
        },
    )
    assert response.status_code == 200, response.text
    assert len(seen.of("router-llm")) == 4
    body = response.text
    assert "已經寫到這裡" in body
    assert '"finish_reason": "length"' in body
    metas = [
        json.loads(data)
        for name, data in _events(body)
        if name == "anila.meta"
    ]
    assert metas and metas[-1].get("finish_reason") == "length"


@respx.mock
def test_non_stream_length_reaches_the_json(db_path: Path):
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "已經寫到這裡"},
                        "finish_reason": "length",
                    }
                ]
            },
        )

    respx.get(CSP_AGENTS_URL).mock(return_value=httpx.Response(200, json={"data": []}))
    respx.post(CSP_URL).mock(side_effect=handler)
    client = TestClient(create_router_app(session_db_path=str(db_path)))
    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer sk-test"},
        json={
            "stream": False,
            "messages": [{"role": "user", "content": "寫長文"}],
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert calls["n"] == 4
    assert data["choices"][0]["message"]["content"] == "\n".join(["已經寫到這裡"] * 4)
    assert data["choices"][0]["finish_reason"] == "length"
    assert data["anila_meta"]["finish_reason"] == "length"
