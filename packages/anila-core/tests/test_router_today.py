"""主模型每次呼叫都要知道台北的今天。

日期在組好的系統提示尾端，不寫進治理中心可編輯的三段提示。
時間在每次請求現算，不在 import 時凍結。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
import pytest_asyncio
import respx
from fastapi.testclient import TestClient

from anila_core.api import router_prompts as rp
from anila_core.api import router_server as rs
from anila_core.api.router_server import create_router_app
from anila_core.config import settings
from anila_core.memory import close_all_connections


CSP_URL = f"{settings.csp_base_url}/v1/chat/completions"
CSP_AGENTS_URL = f"{settings.csp_base_url}/v1/agents"
TAIPEI = ZoneInfo("Asia/Taipei")
FROZEN = datetime(2026, 9, 25, 12, 0, tzinfo=TAIPEI)
ZH_LINE = "今天是 2026 年 9 月 25 日（民國 115 年，星期五），時區 Asia/Taipei。"
EN_LINE = "Today is Friday, September 25, 2026 (ROC year 115), timezone Asia/Taipei."


@pytest_asyncio.fixture
async def db_path(tmp_path: Path):
    db = tmp_path / "router-today.db"
    yield db
    await close_all_connections()


def _freeze(monkeypatch, when: datetime) -> None:
    monkeypatch.setattr(rs, "_taipei_now", lambda: when)


def _llm(content: str, finish: str = "stop", reasoning: str = "") -> dict:
    message: dict = {"role": "assistant", "content": content}
    if reasoning:
        message["reasoning_content"] = reasoning
    return {
        "id": "chatcmpl-today",
        "object": "chat.completion",
        "model": "router-llm",
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
    }


def _system_text(payload: dict) -> str:
    for message in payload.get("messages") or []:
        if isinstance(message, dict) and message.get("role") == "system":
            return str(message.get("content") or "")
    return ""


def test_editable_prompts_do_not_contain_the_date():
    for text in (
        rp.DEFAULT_ROUTER_SYSTEM,
        rp.DEFAULT_PLAIN_ASSISTANT,
        rp.DEFAULT_FORCED_ANSWER,
    ):
        assert "今天是" not in text
        assert "Today is" not in text
        assert "Asia/Taipei" not in text


def test_date_is_computed_per_request_not_at_import(monkeypatch):
    _freeze(monkeypatch, FROZEN)
    first = rs._build_system_prompt([])
    assert ZH_LINE in first
    assert first.rstrip().endswith(ZH_LINE)
    _freeze(monkeypatch, datetime(2026, 9, 26, 8, 0, tzinfo=TAIPEI))
    second = rs._build_system_prompt([])
    assert "9 月 26 日" in second
    assert "星期六" in second
    assert "9 月 25 日" not in second


def test_clock_uses_taipei_not_utc(monkeypatch):
    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            # 16:30 UTC 在台北已是隔天 00:30。
            instant = datetime(2026, 9, 24, 16, 30, tzinfo=timezone.utc)
            if tz is None:
                return instant.replace(tzinfo=None)
            return instant.astimezone(tz)

    monkeypatch.setattr(rs, "datetime", _Clock)
    assert rs._taipei_now().day == 25
    line_source = rs._build_system_prompt([])
    assert "9 月 25 日" in line_source
    assert "9 月 24 日" not in line_source


def test_admin_edit_cannot_drop_the_date_and_english_prompt_stays_english(monkeypatch):
    _freeze(monkeypatch, FROZEN)
    monkeypatch.setattr(
        rs,
        "current_router_prompts",
        lambda: {
            rp.KEY_SYSTEM: "Route the request.\n{agent_list}",
            rp.KEY_PLAIN: "Answer in English.",
            rp.KEY_FORCED: "Answer from the regulations.",
        },
    )
    plain = rs._build_system_prompt([])
    forced = rs._forced_answer_prompt()
    routed = rs._build_system_prompt([_FakeAgent()])
    assert plain.startswith("Answer in English.")
    assert EN_LINE in plain
    assert "今天是" not in plain
    assert forced.startswith("Answer from the regulations.")
    assert EN_LINE in forced
    assert routed.startswith("Route the request.")
    assert EN_LINE in routed
    stored = rs.current_router_prompts()
    assert "Today is" not in stored[rp.KEY_PLAIN]
    assert "Today is" not in stored[rp.KEY_FORCED]
    assert "Today is" not in stored[rp.KEY_SYSTEM]


def test_chinese_prompt_uses_the_chinese_line(monkeypatch):
    _freeze(monkeypatch, FROZEN)
    monkeypatch.setattr(
        rs,
        "current_router_prompts",
        lambda: {
            rp.KEY_SYSTEM: "你是派工器。\n{agent_list}",
            rp.KEY_PLAIN: "請用繁體中文回答。",
            rp.KEY_FORCED: "請依院內規章回答。",
        },
    )
    plain = rs._build_system_prompt([])
    forced = rs._forced_answer_prompt()
    assert ZH_LINE in plain
    assert ZH_LINE in forced
    assert "Today is" not in plain
    assert rs.current_router_prompts()[rp.KEY_PLAIN] == "請用繁體中文回答。"


class _FakeAgent:
    def to_tool_description(self) -> str:
        return "demo: 示範"


def _post(client: TestClient, **extra):
    headers = {"Authorization": "Bearer sk-test"}
    headers.update(extra.pop("headers", {}))
    body = {"messages": [{"role": "user", "content": "幫我做一份報告"}], "stream": False}
    body.update(extra)
    return client.post("/v1/chat/completions", json=body, headers=headers)


def _empty_agents() -> httpx.Response:
    return httpx.Response(200, json={"data": []})


def _demo_agents() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "data": [
                {
                    "id": "demo",
                    "name": "demo",
                    "description_for_router": "示範",
                    "endpoint_url": "http://demo",
                    "requires_encryption": False,
                }
            ]
        },
    )


@respx.mock
def test_direct_answer_system_prompt_names_taipei_today(db_path: Path, monkeypatch) -> None:
    _freeze(monkeypatch, FROZEN)
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content.decode()))
        return httpx.Response(200, json=_llm("直接回答。"))

    respx.get(CSP_AGENTS_URL).mock(return_value=_empty_agents())
    respx.post(CSP_URL).mock(side_effect=handler)
    client = TestClient(create_router_app(session_db_path=str(db_path)))
    resp = _post(client, session_id="s-direct")
    assert resp.status_code == 200, resp.text
    assert _system_text(seen[0]).rstrip().endswith(ZH_LINE)


@respx.mock
def test_dispatch_decision_system_prompt_names_taipei_today(db_path: Path, monkeypatch) -> None:
    _freeze(monkeypatch, FROZEN)
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content.decode()))
        if len(seen) == 1:
            return httpx.Response(200, json=_llm("DISPATCH:demo:幫我做一份報告"))
        return httpx.Response(200, json=_llm("agent 已處理。"))

    respx.get(CSP_AGENTS_URL).mock(return_value=_demo_agents())
    respx.post(CSP_URL).mock(side_effect=handler)
    client = TestClient(create_router_app(session_db_path=str(db_path)))
    resp = _post(client, session_id="s-dispatch")
    assert resp.status_code == 200, resp.text
    system = _system_text(seen[0])
    assert system.rstrip().endswith(ZH_LINE)
    assert "DISPATCH:" in system


@respx.mock
def test_ask_resume_and_rescue_system_prompts_name_taipei_today(
    db_path: Path, monkeypatch
) -> None:
    _freeze(monkeypatch, FROZEN)
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content.decode()))
        n = len(seen)
        if n == 1:
            return httpx.Response(200, json=_llm("ASK:要哪一種？|技術評估|市場"))
        if n == 2:
            return httpx.Response(200, json=_llm(" \n ", "length", "想過題目"))
        return httpx.Response(200, json=_llm("救援後的正文。"))

    respx.get(CSP_AGENTS_URL).mock(return_value=_empty_agents())
    respx.post(CSP_URL).mock(side_effect=handler)
    client = TestClient(create_router_app(session_db_path=str(db_path)))
    asked = _post(client, session_id="s-ask")
    assert asked.status_code == 200, asked.text
    state = client.get(
        "/v1/sessions/s-ask/state",
        headers={"Authorization": "Bearer sk-test"},
    )
    interrupt_id = state.json()["pending_interrupts"][0]["id"]
    resumed = client.post(
        "/v1/sessions/s-ask/answer",
        json={
            "interrupt_id": interrupt_id,
            "answer": {"selected": ["技術評估"], "other_text": ""},
            "stream": False,
        },
        headers={"Authorization": "Bearer sk-test"},
    )
    assert resumed.status_code == 200, resumed.text
    assert len(seen) >= 3
    for payload in seen:
        assert _system_text(payload).rstrip().endswith(ZH_LINE)


@respx.mock
def test_forced_answer_system_prompt_names_taipei_today(db_path: Path, monkeypatch) -> None:
    _freeze(monkeypatch, FROZEN)
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content.decode()))
        return httpx.Response(200, json=_llm("依規章回答。"))

    respx.get(CSP_AGENTS_URL).mock(return_value=_demo_agents())
    respx.post(CSP_URL).mock(side_effect=handler)
    client = TestClient(create_router_app(session_db_path=str(db_path)))
    resp = _post(client, session_id="s-forced", headers={"X-ANILA-Route": "forced"})
    assert resp.status_code == 200, resp.text
    system = _system_text(seen[0])
    assert system.rstrip().endswith(ZH_LINE)
    assert "院內規章" in system
