"""超長回答改存成對話附件，聊天裡只留摘要、目錄和文件卡。"""
from __future__ import annotations

import asyncio
import json
import logging

import pytest

from anila_core.api import router_server as rs
from anila_core.memory import MemorySession


class _Resp:
    def __init__(self, payload: dict, status: int = 201):
        self._payload = payload
        self.status_code = status
        self.text = json.dumps(payload)

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
    def __init__(self, *, streams=None, upload_status: int = 201):
        self.streams: list = []
        self._streams = list(streams or [])
        self.uploads: list[dict] = []
        self.upload_status = upload_status

    def stream(self, method, url, json=None, headers=None):
        self.streams.append(json)
        return _StreamResp(self._streams.pop(0))

    async def post(self, url, json=None, headers=None, data=None, files=None, timeout=None):
        name, raw, _mime = files["file"]
        body = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        self.uploads.append({
            "url": url,
            "headers": headers or {},
            "data": data or {},
            "filename": name,
            "body": body,
        })
        if self.upload_status >= 400:
            return _Resp({"detail": "失敗"}, status=self.upload_status)
        return _Resp({
            "reference_id": "doc-1",
            "filename": name,
            "size_bytes": len(body.encode("utf-8")),
            "content_type": "text/markdown",
        })


class _EmptyRegistry:
    def get(self, *args, **kwargs):
        return None


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
                name = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data = line[len("data:"):].strip()
        if data:
            events.append((name, data))
    return events


def _visible(body: str) -> str:
    text: list[str] = []
    for name, data in _events(body):
        if name != "message" or data == "[DONE]":
            continue
        payload = json.loads(data)
        for choice in payload.get("choices") or []:
            piece = (choice.get("delta") or {}).get("content")
            if piece:
                text.append(piece)
    return "".join(text)


def _long_markdown() -> str:
    opening = "這是開頭摘要，說明這份文件在講什麼。"
    chapters = []
    for index in range(1, 8):
        chapters.append(f"## 第{index}章\n\n" + ("本章內容。" * 200))
    tail = "\n\n結尾標記UNIQUE-TAIL"
    return opening + "\n\n" + "\n\n".join(chapters) + tail


def _drive_fake(monkeypatch, content: str, *, finish: str = "stop", client: _Client, headers=None) -> str:
    async def fake_stream(*_args, **_kwargs):
        yield {"type": "delta", "content": content}
        yield {"type": "done", "finish_reason": finish}

    monkeypatch.setattr(rs, "_stream_llm_sse", fake_stream)
    monkeypatch.setattr(rs, "get_http_client", lambda: client)
    monkeypatch.setattr(rs, "_current_service_token", lambda: "csk-router-test")

    async def run() -> str:
        parts: list[str] = []
        async for line in rs._router_streaming(
            "sk-user",
            [{"role": "user", "content": "寫長文"}],
            [{"role": "user", "content": "寫長文"}],
            registry=_EmptyRegistry(),
            base_trace=[],
            started_at=0.0,
            session=MemorySession("long-doc"),
            route_signal=rs._ROUTE_DIRECT,
            forwarded_headers=headers,
        ):
            parts.append(line)
        return "".join(parts)

    return asyncio.run(run())


def test_limit_is_fixed_in_the_router_module():
    assert rs.LONG_ANSWER_CHAR_LIMIT == 7000


def test_answer_over_7000_chars_is_saved_as_a_document(monkeypatch):
    text = _long_markdown()
    assert len(text) > 7000
    client = _Client()
    body = _drive_fake(
        monkeypatch,
        text,
        client=client,
        headers={"X-ANILA-Conversation-Id": "42"},
    )
    assert len(client.uploads) == 1
    upload = client.uploads[0]
    assert upload["url"].endswith("/api/attachments")
    assert upload["headers"]["Authorization"] == "Bearer sk-user"
    assert upload["headers"]["X-CSP-Service-Token"] == "csk-router-test"
    assert upload["data"]["conversation_id"] == "42"
    assert upload["data"].get("origin") == "generated"
    assert upload["filename"].endswith(".md")
    assert "第1章" in upload["filename"]
    assert upload["body"] == text
    docs = [json.loads(data) for name, data in _events(body) if name == "anila.document"]
    assert len(docs) == 1
    card = docs[0]
    assert card["reference_id"] == "doc-1"
    assert card["filename"].endswith(".md")
    assert card["size_bytes"] == len(text.encode("utf-8"))
    assert card["char_count"] == len(text)
    assert "這是開頭摘要" in card["preview"]
    assert "目錄" in card["preview"]
    assert "第1章" in card["preview"]
    assert "UNIQUE-TAIL" not in card["preview"]
    assert len(card["preview"]) < 7000
    metas = [json.loads(data) for name, data in _events(body) if name == "anila.meta"]
    assert metas[-1]["document"]["reference_id"] == "doc-1"
    assert metas[-1]["document"]["preview"] == card["preview"]


def test_answer_of_exactly_7000_chars_stays_in_chat(monkeypatch):
    text = "甲" * 7000
    client = _Client()

    def _boom(*_args, **_kwargs):
        raise AssertionError("不該上傳")

    client.post = _boom
    body = _drive_fake(monkeypatch, text, client=client, headers={"X-ANILA-Conversation-Id": "42"})
    assert "anila.document" not in body
    assert _visible(body) == text


def test_three_exhausted_continuations_save_the_joined_answer(monkeypatch):
    client = _Client(streams=[_stream_lines("片段", "length") for _ in range(4)])
    monkeypatch.setattr(rs, "get_http_client", lambda: client)

    async def run() -> str:
        parts: list[str] = []
        async for line in rs._router_streaming(
            "sk-user",
            [{"role": "user", "content": "寫長文"}],
            [{"role": "user", "content": "寫長文"}],
            registry=_EmptyRegistry(),
            base_trace=[],
            started_at=0.0,
            session=MemorySession("long-doc"),
            route_signal=rs._ROUTE_DIRECT,
            forwarded_headers={"X-ANILA-Conversation-Id": "7"},
        ):
            parts.append(line)
        return "".join(parts)

    body = asyncio.run(run())
    assert len(client.streams) == 4
    assert len(client.uploads) == 1
    assert client.uploads[0]["body"] == "片段" * 4
    assert client.uploads[0]["data"]["conversation_id"] == "7"
    docs = [json.loads(data) for name, data in _events(body) if name == "anila.document"]
    assert docs[0]["char_count"] == 8
    assert docs[0]["reference_id"] == "doc-1"
    metas = [json.loads(data) for name, data in _events(body) if name == "anila.meta"]
    assert metas[-1]["document"]["reference_id"] == "doc-1"
    assert metas[-1].get("finish_reason") == "length"


def test_failed_attachment_save_keeps_the_full_answer(monkeypatch, caplog):
    text = _long_markdown()
    client = _Client(upload_status=500)
    with caplog.at_level(logging.WARNING):
        body = _drive_fake(
            monkeypatch,
            text,
            client=client,
            headers={"X-ANILA-Conversation-Id": "42"},
        )
    assert len(client.uploads) == 1
    assert "anila.document" not in body
    assert text in _visible(body)
    assert not any(
        json.loads(data).get("document")
        for name, data in _events(body)
        if name == "anila.meta"
    )
    assert any("附件" in rec.message or "attachment" in rec.message.lower() for rec in caplog.records)
