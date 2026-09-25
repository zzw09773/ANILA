"""Router-side ASK — a plain turn pauses without an owning agent.

A dispatched agent already pauses through ``ask_user``. A Router turn that
answers by itself has no tool loop, so the routing LLM emits ``ASK:`` and the
Router persists that pause on its own Session. These tests pin the contracts
that path has to keep:

* a non-streaming ``ASK:`` reply pauses, emits ``anila.interrupt_requested``,
  and never leaks the protocol text
* the pending interrupt is still readable from ``GET /v1/sessions/{id}/state``
  after the Session is reopened
* ``POST /v1/sessions/{id}/answer`` resumes that pause (``anila.resumed``
  first) and returns the next routing-LLM answer
* a reply whose first line is ``ASK:`` pauses, even if a later line is ``DISPATCH:``
* prose that merely quotes ``ASK:`` is not a pause

The directive is only recognised on the FIRST line; anything later is prose the
reader sees unchanged.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest_asyncio
import respx
from fastapi.testclient import TestClient
from starlette.requests import Request

import pytest

from anila_core.api.router_server import (
    _resume_content_text,
    _resume_router_ask,
    _router_answer_resume_message,
    _scrub_diagnostic_value,
    create_router_app,
)
from anila_core.engine.approvals import to_record
from anila_core.http_pool import reset_http_client
from anila_core.models.interrupt import InterruptItem
from anila_core.models.message import AssistantMessage, ToolCall, UserMessage
from anila_core.config import settings
from anila_core.memory import close_all_connections
from anila_core.memory.short_term.sqlite import SqliteSession


CSP_BASE = settings.csp_base_url
CSP_URL = f"{CSP_BASE}/v1/chat/completions"
CSP_AGENTS_URL = f"{CSP_BASE}/v1/agents"


@pytest_asyncio.fixture
async def db_path(tmp_path: Path):
    db = tmp_path / "router-ask.db"
    yield db
    await close_all_connections()


def _bare_completion(content: str, model: str = "router-llm") -> dict:
    """An upstream completion with no ``anila_meta`` for the Router to merge."""
    return {
        "id": "chatcmpl-a",
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
    }


def _llm_reply(content: str) -> dict:
    return {
        "id": "chatcmpl-r",
        "object": "chat.completion",
        "created": 0,
        "model": "router-llm",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
    }


def _agents_payload(agent_id: str) -> dict:
    return {
        "data": [
            {
                "id": agent_id,
                "name": agent_id,
                "description_for_router": "Demo agent",
                "endpoint_url": "http://" + agent_id,
                "requires_encryption": False,
            }
        ]
    }


def _llm_sse(*deltas: str) -> str:
    """Content chunks, then a stop, then ``[DONE]``."""
    out: list[str] = []
    for d in deltas:
        chunk = {
            "id": "chatcmpl-r",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "router-llm",
            "choices": [
                {"index": 0, "delta": {"content": d}, "finish_reason": None}
            ],
        }
        out.append("data: " + json.dumps(chunk) + "\n\n")
    stop = {
        "id": "chatcmpl-r",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "router-llm",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    out.append("data: " + json.dumps(stop) + "\n\n")
    out.append("data: [DONE]\n\n")
    return "".join(out)


def _sse_events(body: str) -> list[tuple[str, str]]:
    """Split an SSE body into (event_name, data) pairs, in order."""
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


@respx.mock
def test_non_streaming_ask_pauses_without_leaking_protocol(db_path: Path) -> None:
    """A leading ``ASK:`` pauses the turn; the directive never reaches the user."""
    respx.get(CSP_AGENTS_URL).mock(return_value=httpx.Response(200, json={"data": []}))
    respx.post(CSP_URL).mock(
        return_value=httpx.Response(
            200, json=_bare_completion("ASK:要查哪一年的規章？|2024|2025")
        )
    )

    client = TestClient(create_router_app(session_db_path=str(db_path)))
    resp = client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "幫我查規章"}],
            "stream": False,
            "session_id": "s-ask",
        },
        headers={"Authorization": "Bearer sk-test"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["choices"][0]["message"]["content"] == ""
    assert "ASK:" not in body["choices"][0]["message"]["content"]
    assert body["anila_meta"]["route"]["decision"] == "ask"
    interrupt = body["anila_meta"]["interrupt"]
    assert interrupt["kind"] == "ask_user"
    assert interrupt["payload"]["question"] == "要查哪一年的規章？"
    assert [o["value"] for o in interrupt["payload"]["options"]] == ["2024", "2025"]
    assert not interrupt["payload"].get("multi")


@respx.mock
def test_pending_ask_survives_session_reload(db_path: Path) -> None:
    """The pause is on the Router's own Session, so a fresh app still sees it."""
    respx.get(CSP_AGENTS_URL).mock(return_value=httpx.Response(200, json={"data": []}))
    respx.post(CSP_URL).mock(
        return_value=httpx.Response(200, json=_bare_completion("ASK:要繼續嗎？|要|不要"))
    )

    create = TestClient(create_router_app(session_db_path=str(db_path)))
    r = create.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "跑一下"}],
            "stream": False,
            "session_id": "s-reload",
        },
        headers={"Authorization": "Bearer sk-test"},
    )
    assert r.status_code == 200, r.text

    # A second app over the same DB — the reload shape.
    reopened = TestClient(create_router_app(session_db_path=str(db_path)))
    state = reopened.get(
        "/v1/sessions/s-reload/state",
        headers={"Authorization": "Bearer sk-test"},
    )
    assert state.status_code == 200, state.text
    pending = state.json()["pending_interrupts"]
    assert len(pending) == 1
    assert pending[0]["kind"] == "ask_user"
    assert pending[0]["payload"]["question"] == "要繼續嗎？"


@respx.mock
def test_streaming_ask_emits_interrupt_before_done(db_path: Path) -> None:
    """The interrupt event precedes the terminal chunk; question text is clean."""
    respx.get(CSP_AGENTS_URL).mock(return_value=httpx.Response(200, json={"data": []}))
    respx.post(CSP_URL).mock(
        return_value=httpx.Response(
            200,
            content=_llm_sse("ASK:要繼續嗎？|要|不要\n").encode("utf-8"),
            headers={"Content-Type": "text/event-stream"},
        )
    )

    client = TestClient(create_router_app(session_db_path=str(db_path)))
    resp = client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "跑一下"}],
            "stream": True,
            "session_id": "s-stream",
        },
        headers={"Authorization": "Bearer sk-test"},
    )
    assert resp.status_code == 200, resp.text
    events = _sse_events(resp.text)
    names = [name for name, _ in events]
    assert "anila.interrupt_requested" in names
    assert names.index("anila.interrupt_requested") < len(names) - 1
    payload = json.loads(events[names.index("anila.interrupt_requested")][1])
    assert payload["payload"]["question"] == "要繼續嗎？"
    assert "ASK:" not in _visible_sse_text(resp.text)


@respx.mock
def test_resumed_turn_can_ask_again(db_path: Path) -> None:
    """A resumed reply that is itself an ASK pauses again (no lost state)."""
    calls: list[list[dict]] = []

    def csp_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        calls.append(body.get("messages") or [])
        if len(calls) == 1:
            return httpx.Response(200, json=_llm_reply("ASK:第一次問？|A|B"))
        if len(calls) == 2:
            # The first resume turn streams and asks again.
            return httpx.Response(
                200,
                content=_llm_sse("ASK:第二次問？|C|D").encode("utf-8"),
                headers={"Content-Type": "text/event-stream"},
            )
        return httpx.Response(
            200,
            content=_llm_sse("好，答案是這個。").encode("utf-8"),
            headers={"Content-Type": "text/event-stream"},
        )

    respx.get(CSP_AGENTS_URL).mock(return_value=httpx.Response(200, json={"data": []}))
    respx.post(CSP_URL).mock(side_effect=csp_handler)

    client = TestClient(create_router_app(session_db_path=str(db_path)))
    first = client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "開始"}],
            "stream": False,
            "session_id": "s-twice",
        },
        headers={"Authorization": "Bearer sk-test"},
    )
    assert first.status_code == 200, first.text

    state = client.get(
        "/v1/sessions/s-twice/state",
        headers={"Authorization": "Bearer sk-test"},
    ).json()
    iid = state["pending_interrupts"][0]["id"]

    second = client.post(
        "/v1/sessions/s-twice/answer",
        json={
            "interrupt_id": iid,
            "answer": {"selected": ["A"], "other_text": ""},
        },
        headers={"Authorization": "Bearer sk-test"},
    )
    assert second.status_code == 200, second.text
    body = second.text
    assert "anila.resumed" in body
    assert "第二次問？" in body

    state2 = client.get(
        "/v1/sessions/s-twice/state",
        headers={"Authorization": "Bearer sk-test"},
    ).json()
    assert len(state2["pending_interrupts"]) == 1
    assert state2["pending_interrupts"][0]["payload"]["question"] == "第二次問？"


@respx.mock
def test_answer_resumes_router_ask(db_path: Path) -> None:
    """Answering a Router-side pause resumes it and returns the next answer."""
    seen: list[list[dict]] = []

    def csp_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        seen.append(body.get("messages") or [])
        if len(seen) == 1:
            return httpx.Response(200, json=_llm_reply("ASK:要查哪一年？|2024|2025"))
        # The resume turn streams.
        return httpx.Response(
            200,
            content=_llm_sse("2025 年的規章如下。").encode("utf-8"),
            headers={"Content-Type": "text/event-stream"},
        )

    respx.get(CSP_AGENTS_URL).mock(return_value=httpx.Response(200, json={"data": []}))
    respx.post(CSP_URL).mock(side_effect=csp_handler)

    client = TestClient(create_router_app(session_db_path=str(db_path)))
    first = client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "幫我查規章"}],
            "stream": False,
            "session_id": "s-resume",
        },
        headers={"Authorization": "Bearer sk-test"},
    )
    assert first.status_code == 200, first.text

    state = client.get(
        "/v1/sessions/s-resume/state",
        headers={"Authorization": "Bearer sk-test"},
    ).json()
    iid = state["pending_interrupts"][0]["id"]

    resumed = client.post(
        "/v1/sessions/s-resume/answer",
        json={"interrupt_id": iid, "answer": {"selected": ["2025"], "other_text": ""}},
        headers={"Authorization": "Bearer sk-test"},
    )
    assert resumed.status_code == 200, resumed.text
    assert "anila.resumed" in resumed.text
    assert "2025 年的規章如下" in resumed.text


@pytest.fixture
def _no_recompose(monkeypatch) -> None:
    """Keep the synthesis text verbatim so assertions are exact."""
    from anila_core.api import router_server

    async def fake(content, api_key, *, forwarded_headers=None):
        return content, "skipped"

    monkeypatch.setattr(router_server, "_recompose_reply", fake)


@respx.mock
def test_leading_ask_ignores_a_later_dispatch(
    db_path: Path, _no_recompose: None
) -> None:
    """第一行 ASK 決定這一輪，後面的 DISPATCH 不算。"""
    agent_id = "ag"
    respx.get(CSP_AGENTS_URL).mock(
        return_value=httpx.Response(200, json=_agents_payload(agent_id))
    )

    def csp_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        if body.get("model") == agent_id:
            return httpx.Response(200, json=_llm_reply("agent 說的。"))
        text = f"ASK:先確認？\nDISPATCH:{agent_id}:do\n"
        accept = request.headers.get("accept", "")
        if "text/event-stream" in accept:
            return httpx.Response(
                200, content=_llm_sse(text).encode("utf-8"),
                headers={"Content-Type": "text/event-stream"},
            )
        return httpx.Response(200, json=_llm_reply(text))

    respx.post(CSP_URL).mock(side_effect=csp_handler)

    client = TestClient(create_router_app(session_db_path=str(db_path)))
    resp = client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "做一下"}],
            "stream": False,
            "session_id": "s-both",
        },
        headers={"Authorization": "Bearer sk-test"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["anila_meta"]["route"]["decision"] == "ask"
    assert "interrupt" in body["anila_meta"]


def _visible_sse_text(body: str) -> str:
    out: list[str] = []
    for name, data in _sse_events(body):
        if name != "message" or data == "[DONE]":
            continue
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            continue
        for ch in payload.get("choices") or []:
            piece = (ch.get("delta") or {}).get("content")
            if piece:
                out.append(piece)
    return "".join(out)


@respx.mock
def test_other_caller_cannot_answer_router_ask(db_path: Path) -> None:
    """A different ``sk-*`` must not complete someone else's pause."""
    respx.get(CSP_AGENTS_URL).mock(return_value=httpx.Response(200, json={"data": []}))
    respx.post(CSP_URL).mock(
        return_value=httpx.Response(200, json=_bare_completion("ASK:要嗎？|要|不要"))
    )

    client = TestClient(create_router_app(session_db_path=str(db_path)))
    r = client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
            "session_id": "s-owner",
        },
        headers={"Authorization": "Bearer sk-alpha"},
    )
    assert r.status_code == 200, r.text

    state = client.get(
        "/v1/sessions/s-owner/state",
        headers={"Authorization": "Bearer sk-alpha"},
    )
    assert state.status_code == 200
    iid = state.json()["pending_interrupts"][0]["id"]

    intruder = client.post(
        "/v1/sessions/s-owner/answer",
        json={"interrupt_id": iid, "answer": {"selected": ["要"], "other_text": ""}},
        headers={"Authorization": "Bearer sk-beta"},
    )
    assert intruder.status_code == 403, intruder.text


_PROSE_WITH_QUOTED_ASK = (
    "先說明怎麼用。\n"
    "* `ASK:` 是反問指令，只認第一行\n"
    "所以後面的 ASK: 都是普通文字。"
)


@respx.mock
def test_prose_quoting_ask_is_not_a_pause(db_path: Path) -> None:
    """A later line that merely mentions ``ASK:`` is prose, not a pause."""
    respx.get(CSP_AGENTS_URL).mock(return_value=httpx.Response(200, json={"data": []}))
    respx.post(CSP_URL).mock(
        return_value=httpx.Response(200, json=_llm_reply(_PROSE_WITH_QUOTED_ASK))
    )

    client = TestClient(create_router_app(session_db_path=str(db_path)))
    resp = client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "解釋一下"}],
            "stream": False,
            "session_id": "s-prose",
        },
        headers={"Authorization": "Bearer sk-test"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "interrupt" not in body["anila_meta"]
    assert body["choices"][0]["message"]["content"] == _PROSE_WITH_QUOTED_ASK


@respx.mock
def test_streaming_prose_quoting_ask_passes_through(db_path: Path) -> None:
    """The streaming path has the same first-line rule as the non-streaming one."""
    respx.get(CSP_AGENTS_URL).mock(return_value=httpx.Response(200, json={"data": []}))
    respx.post(CSP_URL).mock(
        return_value=httpx.Response(
            200,
            content=_llm_sse(_PROSE_WITH_QUOTED_ASK).encode("utf-8"),
            headers={"Content-Type": "text/event-stream"},
        )
    )

    client = TestClient(create_router_app(session_db_path=str(db_path)))
    resp = client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "解釋一下"}],
            "stream": True,
            "session_id": "s-prose-stream",
        },
        headers={"Authorization": "Bearer sk-test"},
    )
    assert resp.status_code == 200, resp.text
    names = [name for name, _ in _sse_events(resp.text)]
    assert "anila.interrupt_requested" not in names
    assert _visible_sse_text(resp.text) == _PROSE_WITH_QUOTED_ASK


@pytest.mark.parametrize(
    "directive",
    [
        "ASK:要查哪一年？|2024|2025",
        "  * ASK:要查哪一年？|2024|2025",
        # Wrapper markers must not leak into the question text.
        "`ASK:要查哪一年？|2024|2025`",
        "**ASK:要查哪一年？|2024|2025**",
        "> ASK:要查哪一年？|2024|2025",
    ],
)
@respx.mock
def test_leading_ask_still_pauses(db_path: Path, directive: str) -> None:
    """A first-line ASK pauses, including a leading bullet and whitespace."""
    respx.get(CSP_AGENTS_URL).mock(return_value=httpx.Response(200, json={"data": []}))
    respx.post(CSP_URL).mock(
        return_value=httpx.Response(200, json=_llm_reply(directive))
    )

    client = TestClient(create_router_app(session_db_path=str(db_path)))
    resp = client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "查規章"}],
            "stream": False,
            "session_id": "s-leading",
        },
        headers={"Authorization": "Bearer sk-test"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["choices"][0]["message"]["content"] == ""
    assert body["anila_meta"]["interrupt"]["payload"]["options"][0]["value"] == "2024"
    assert body["anila_meta"]["route"]["decision"] == "ask"


@respx.mock
def test_resumed_turn_sees_the_question(db_path: Path) -> None:
    """The paused question is an assistant turn on the resumed routing call."""
    seen: list[list[dict]] = []

    def csp_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        seen.append(body.get("messages") or [])
        if len(seen) == 1:
            return httpx.Response(200, json=_llm_reply("ASK:要查哪一年的規章？|2024|2025"))
        return httpx.Response(200, json=_llm_reply("2025 的規章如下。"))

    respx.get(CSP_AGENTS_URL).mock(return_value=httpx.Response(200, json={"data": []}))
    respx.post(CSP_URL).mock(side_effect=csp_handler)

    client = TestClient(create_router_app(session_db_path=str(db_path)))
    first = client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "幫我查規章"}],
            "stream": False,
            "session_id": "s-question",
        },
        headers={"Authorization": "Bearer sk-test"},
    )
    assert first.status_code == 200, first.text

    state = client.get(
        "/v1/sessions/s-question/state",
        headers={"Authorization": "Bearer sk-test"},
    ).json()
    iid = state["pending_interrupts"][0]["id"]

    client.post(
        "/v1/sessions/s-question/answer",
        json={"interrupt_id": iid, "answer": {"selected": ["2025"], "other_text": ""}},
        headers={"Authorization": "Bearer sk-test"},
    )

    assert len(seen) >= 2
    flat = json.dumps(seen[-1], ensure_ascii=False)
    assert "要查哪一年的規章？" in flat


@respx.mock
def test_streaming_ask_then_dispatch_ask_wins(
    db_path: Path, _no_recompose: None
) -> None:
    """ASK wins on split deltas because a complete leading ASK pauses at once.

    First delta is a finished ``ASK:`` line; the following ``DISPATCH:`` delta
    is never read. That is deliberate: a reply whose first line is ASK owns
    the turn, same as the non-streaming first-line rule.
    """
    agent_id = "ax"
    respx.get(CSP_AGENTS_URL).mock(
        return_value=httpx.Response(200, json=_agents_payload(agent_id))
    )
    respx.post(CSP_URL).mock(
        return_value=httpx.Response(
            200,
            content=_llm_sse("ASK:先確認一下？\n", f"DISPATCH:{agent_id}:do\n").encode(
                "utf-8"
            ),
            headers={"Content-Type": "text/event-stream"},
        )
    )

    client = TestClient(create_router_app(session_db_path=str(db_path)))
    resp = client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "做一下"}],
            "stream": True,
            "session_id": "s-split",
        },
        headers={"Authorization": "Bearer sk-test"},
    )
    assert resp.status_code == 200, resp.text
    events = _sse_events(resp.text)
    names = [name for name, _ in events]
    assert "anila.interrupt_requested" in names
    payload = json.loads(events[names.index("anila.interrupt_requested")][1])
    assert payload["payload"]["question"] == "先確認一下？"
    meta = json.loads(events[names.index("anila.meta")][1])
    assert meta["interrupt"] == payload
    assert "DISPATCH:" not in resp.text
    state = client.get(
        "/v1/sessions/s-split/state",
        headers={"Authorization": "Bearer sk-test"},
    )
    assert state.json()["owner_agent_id"] in (None, "")


@respx.mock
def test_multiline_ask_uses_first_line_and_ships_the_rest_as_prose(
    db_path: Path,
) -> None:
    """Only the ASK line is directive syntax; a wrapped question is NOT folded in.

    Pinned here because the behaviour is easy to misread: the model putting the
    rest of its question on the next line does not extend the interrupt — the
    first line is the question and the remainder stays reader-visible prose.
    """
    respx.get(CSP_AGENTS_URL).mock(return_value=httpx.Response(200, json={"data": []}))
    respx.post(CSP_URL).mock(
        return_value=httpx.Response(
            200, json=_llm_reply("ASK:第一行是問題？|A|B\n第二行是補充說明。")
        )
    )

    client = TestClient(create_router_app(session_db_path=str(db_path)))
    resp = client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "查一下"}],
            "stream": False,
            "session_id": "s-multiline",
        },
        headers={"Authorization": "Bearer sk-test"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["choices"][0]["message"]["content"] == "第二行是補充說明。"
    assert "第一行是問題" not in body["choices"][0]["message"]["content"]
    assert len(body["anila_meta"]["interrupt"]["payload"]["options"]) == 2


@respx.mock
def test_multi_turn_path_pauses_on_ask_and_hides_the_directive(
    db_path: Path,
) -> None:
    """``anila_multi_turn>1`` must pause too, not ship the raw ``ASK:`` line.

    Regression: ``_router_streaming_multi_turn`` had its own direct-answer exit
    with no ``_parse_ask`` at all, so enabling multi-turn turned ASK into plain
    prose — the directive rode into the bubble and the turn never paused. The
    streaming single-shot path was already correct, which is what made it easy
    to miss.
    """
    respx.get(CSP_AGENTS_URL).mock(return_value=httpx.Response(200, json={"data": []}))
    respx.post(CSP_URL).mock(
        return_value=httpx.Response(200, json=_bare_completion("ASK:要先確認嗎？|A|B"))
    )

    client = TestClient(create_router_app(session_db_path=str(db_path)))
    resp = client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "做一下"}],
            "stream": True,
            "anila_multi_turn": 3,
            "session_id": "s-multiturn-ask",
        },
        headers={"Authorization": "Bearer sk-test"},
    )
    assert resp.status_code == 200, resp.text
    events = _sse_events(resp.text)
    names = [name for name, _ in events]
    assert "anila.interrupt_requested" in names
    payload = json.loads(events[names.index("anila.interrupt_requested")][1])
    assert payload["payload"]["question"] == "要先確認嗎？"
    meta = json.loads(events[names.index("anila.meta")][1])
    assert meta["interrupt"] == payload
    assert "ASK:" not in _visible_sse_text(resp.text)

    # ...and the pause is real: it is on the Session, so it survives a reload.
    state = client.get(
        "/v1/sessions/s-multiturn-ask/state",
        headers={"Authorization": "Bearer sk-test"},
    )
    assert state.status_code == 200, state.text
    assert any(p["kind"] == "ask_user" for p in state.json()["pending_interrupts"])


@respx.mock
def test_synthesis_turn_can_ask_after_a_dispatch(
    db_path: Path, _no_recompose: None
) -> None:
    """The post-dispatch synthesis is Router-authored, so it may ASK too.

    Regression: both multi-turn exits returned ``final_text`` verbatim, so a
    synthesis that paused shipped the raw directive instead of an interrupt.
    """
    agent_id = "syn"
    calls: list[str] = []

    def csp_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        if body.get("model") == agent_id:
            return httpx.Response(200, json=_bare_completion("agent 說了一些。", agent_id))
        if not calls:
            calls.append("dispatch")
            return httpx.Response(200, json=_bare_completion(f"DISPATCH:{agent_id}:do"))
        return httpx.Response(200, json=_bare_completion("ASK:合成後要問？|X|Y"))

    respx.get(CSP_AGENTS_URL).mock(
        return_value=httpx.Response(200, json=_agents_payload(agent_id))
    )
    respx.post(CSP_URL).mock(side_effect=csp_handler)

    client = TestClient(create_router_app(session_db_path=str(db_path)))
    resp = client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "go"}],
            "stream": False,
            "anila_multi_turn": 3,
            "session_id": "s-synth",
        },
        headers={"Authorization": "Bearer sk-test"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["anila_meta"]["route"]["decision"] == "ask"
    assert body["choices"][0]["message"]["content"] == ""
    assert "ASK:" not in body["choices"][0]["message"]["content"]


_RESUME_OUTAGE = "（暫時無法回應，請稍後再試。若一直發生，請聯絡管理員。）"
_RESOLVED_RESUME_MODEL = "glm-resume-not-env"
CSP_RESOLVE_URL = f"{CSP_BASE}/api/router-models/resolve"


def _choice_text(body: str) -> str:
    """Visible assistant text carried in OpenAI chunks, ignoring named events."""
    parts: list[str] = []
    for name, data in _sse_events(body):
        if data == "[DONE]":
            continue
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict) or "choices" not in payload:
            continue
        if name not in ("message", ""):
            continue
        for choice in payload.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
            message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
            parts.append(str(delta.get("content") or message.get("content") or ""))
    return "".join(parts)


def _pause_router_ask(client: TestClient, session_id: str) -> str:
    """Open a Router-side ASK and return its pending interrupt id."""
    first = client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "挑功能"}],
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
@pytest.mark.real_router_model_resolve
@pytest.mark.parametrize("stream", [True, False])
def test_router_ask_resume_uses_csp_resolved_model(
    db_path: Path, stream: bool
) -> None:
    """Resume is its own request: the routing LLM must use CSP's model.

    ``chat_completions`` resolves the model and stores it on a ContextVar.
    The answer endpoint does not inherit that. Falling through to
    ``settings.model`` is what made CSP answer 404 for a model it does
    not have.
    """
    assert _RESOLVED_RESUME_MODEL != settings.model
    seen: list[dict] = []

    def csp_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        seen.append(body)
        if len(seen) == 1:
            return httpx.Response(
                200, json=_llm_reply("ASK:要挑哪幾個？|一|全都要")
            )
        if body.get("stream"):
            return httpx.Response(
                200,
                content=_llm_sse("好，全都要。").encode("utf-8"),
                headers={"Content-Type": "text/event-stream"},
            )
        return httpx.Response(200, json=_llm_reply("好，全都要。"))

    resolve_route = respx.post(CSP_RESOLVE_URL).mock(
        return_value=httpx.Response(200, json={"name": _RESOLVED_RESUME_MODEL})
    )
    respx.get(CSP_AGENTS_URL).mock(return_value=httpx.Response(200, json={"data": []}))
    respx.post(CSP_URL).mock(side_effect=csp_handler)

    client = TestClient(create_router_app(session_db_path=str(db_path)))
    iid = _pause_router_ask(client, "s-resume-model")
    resumed = client.post(
        "/v1/sessions/s-resume-model/answer",
        json={
            "interrupt_id": iid,
            "answer": {"selected": ["全都要"], "other_text": ""},
            "stream": stream,
            "anila_thinking_tier": "deep",
            "temperature": 0.7,
        },
        headers={
            "Authorization": "Bearer sk-test",
            "X-ANILA-Conversation-Id": "42",
        },
    )
    assert resumed.status_code == 200, resumed.text
    # The opening turn resolves once. Resume must resolve again; the
    # conversation id on that second hop is how CSP latches the model.
    assert resolve_route.call_count == 2
    resume_resolve = json.loads(resolve_route.calls[-1].request.content.decode())
    assert resume_resolve["conversation_id"] == 42
    resume_payload = seen[-1]
    assert resume_payload["model"] == _RESOLVED_RESUME_MODEL
    assert resume_payload["model"] != settings.model
    assert resume_payload["anila_thinking_tier"] == "deep"
    assert resume_payload["temperature"] == 0.7


@respx.mock
@pytest.mark.parametrize("stream", [True, False])
def test_router_ask_resume_llm_failure_is_anila_error(
    db_path: Path, stream: bool, caplog: pytest.LogCaptureFixture
) -> None:
    """A failed resume routing call is not a successful answer.

    The outage sentence used to ride out as a content chunk closed with
    ``finish=stop`` and ``[DONE]``. The Shell appends that chunk to the
    question and stores the turn as complete. The turn now ends on
    ``anila.error``. The same interrupt can be answered again, and the
    upstream body does not land in the log.
    """
    secret = "sk-live-RESUME-ASK-DO-NOT-LEAK"
    query_secret = "resume-query-DO-NOT-LEAK"
    calls = {"n": 0}
    seen: list[list] = []

    def csp_handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        body = json.loads(request.content.decode())
        seen.append(body.get("messages") or [])
        if calls["n"] == 1:
            return httpx.Response(
                200, json=_llm_reply("ASK:要挑哪幾個？|一|全都要")
            )
        if calls["n"] == 2:
            return httpx.Response(
                404,
                content=(
                    f"model missing {secret} Bearer {secret} csk-{secret} "
                    f"api_key={secret} password={secret} "
                    f"at http://10.1.2.3/nope?token={query_secret}"
                ).encode(),
            )
        if body.get("stream"):
            return httpx.Response(
                200,
                content=_llm_sse("好，全都要。").encode("utf-8"),
                headers={"Content-Type": "text/event-stream"},
            )
        return httpx.Response(200, json=_llm_reply("好，全都要。"))

    respx.get(CSP_AGENTS_URL).mock(return_value=httpx.Response(200, json={"data": []}))
    respx.post(CSP_URL).mock(side_effect=csp_handler)

    client = TestClient(create_router_app(session_db_path=str(db_path)))
    iid = _pause_router_ask(client, "s-resume-fail")
    with caplog.at_level("WARNING"):
        resumed = client.post(
            "/v1/sessions/s-resume-fail/answer",
            json={
                "interrupt_id": iid,
                "answer": {"selected": ["全都要"], "other_text": ""},
                "stream": stream,
            },
            headers={"Authorization": "Bearer sk-test"},
        )
    assert resumed.status_code == 200, resumed.text
    text = resumed.text
    assert secret not in text
    assert "10.1.2.3" not in text
    assert "data: [DONE]" not in text
    assert '"finish_reason": "stop"' not in text
    assert "LLM HTTP" not in text
    assert "LLM upstream" not in text
    assert _RESUME_OUTAGE not in _choice_text(text)
    assert secret not in caplog.text
    assert query_secret not in caplog.text
    assert any("404" in rec.message for rec in caplog.records)

    events = _sse_events(text)
    names = [name for name, _data in events]
    assert names[0] == "anila.resumed"
    assert names[-1] == "anila.error"
    traces = [json.loads(data) for name, data in events if name == "anila.trace"]
    assert traces
    assert all(step.get("detail") == _RESUME_OUTAGE for step in traces)
    assert all(step.get("status") == "error" for step in traces)
    metas = [json.loads(data) for name, data in events if name == "anila.meta"]
    assert metas == [{"classified": False}]
    meta_i = names.index("anila.meta")
    error_i = names.index("anila.error")
    assert meta_i < error_i
    assert json.loads(events[-1][1]) == {"message": _RESUME_OUTAGE}

    failed_state = client.get(
        "/v1/sessions/s-resume-fail/state",
        headers={"Authorization": "Bearer sk-test"},
    )
    assert failed_state.status_code == 200, failed_state.text
    failed_body = failed_state.json()
    assert [item["id"] for item in failed_body["pending_interrupts"]] == [iid]
    assert "已選擇" not in json.dumps(failed_body["messages"], ensure_ascii=False)
    # The failed call still showed the answer to the model, once. It was
    # not written to the session, so the retry does not see a second copy.
    assert json.dumps(seen[1], ensure_ascii=False).count("已選擇：全都要") == 1

    retried = client.post(
        "/v1/sessions/s-resume-fail/answer",
        json={
            "interrupt_id": iid,
            "answer": {"selected": ["全都要"], "other_text": ""},
            "stream": stream,
        },
        headers={"Authorization": "Bearer sk-test"},
    )
    assert retried.status_code == 200, retried.text
    assert "好，全都要。" in retried.text
    assert "anila.resumed" in retried.text
    assert "event: anila.error" not in retried.text
    assert json.dumps(seen[2], ensure_ascii=False).count("已選擇：全都要") == 1
    done_state = client.get(
        "/v1/sessions/s-resume-fail/state",
        headers={"Authorization": "Bearer sk-test"},
    )
    assert done_state.status_code == 200, done_state.text
    done_body = done_state.json()
    assert done_body["pending_interrupts"] == []
    assert json.dumps(done_body["messages"], ensure_ascii=False).count("已選擇：全都要") == 1
    assert secret not in caplog.text
    assert query_secret not in caplog.text


@respx.mock
@pytest.mark.parametrize("stream", [True, False])
def test_router_ask_resume_resolve_failure_keeps_interrupt(
    db_path: Path, stream: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A model-resolution failure must not consume the pending ASK.

    Resolution runs before the interrupt is claimed. The Shell can post
    the same interrupt_id again once resolution succeeds.
    """
    from fastapi import HTTPException

    from anila_core.api import router_server

    resolve_calls = {"n": 0}

    async def resolve(request: object, caller_api_key: str, body: dict | None) -> None:
        resolve_calls["n"] += 1
        if resolve_calls["n"] == 2:
            raise HTTPException(status_code=503, detail="無法解析對話模型")
        return None

    monkeypatch.setattr(router_server, "_csp_resolve_router_model", resolve)
    calls = {"n": 0}

    def csp_handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        body = json.loads(request.content.decode())
        if calls["n"] == 1:
            return httpx.Response(
                200, json=_llm_reply("ASK:要挑哪幾個？|一|全都要")
            )
        if body.get("stream"):
            return httpx.Response(
                200,
                content=_llm_sse("好，全都要。").encode("utf-8"),
                headers={"Content-Type": "text/event-stream"},
            )
        return httpx.Response(200, json=_llm_reply("好，全都要。"))

    respx.get(CSP_AGENTS_URL).mock(return_value=httpx.Response(200, json={"data": []}))
    respx.post(CSP_URL).mock(side_effect=csp_handler)

    client = TestClient(create_router_app(session_db_path=str(db_path)))
    iid = _pause_router_ask(client, "s-resume-resolve")
    failed = client.post(
        "/v1/sessions/s-resume-resolve/answer",
        json={
            "interrupt_id": iid,
            "answer": {"selected": ["全都要"], "other_text": ""},
            "stream": stream,
        },
        headers={"Authorization": "Bearer sk-test"},
    )
    assert failed.status_code == 503, failed.text
    state = client.get(
        "/v1/sessions/s-resume-resolve/state",
        headers={"Authorization": "Bearer sk-test"},
    )
    assert state.status_code == 200, state.text
    assert [item["id"] for item in state.json()["pending_interrupts"]] == [iid]
    assert "已選擇" not in json.dumps(state.json()["messages"], ensure_ascii=False)

    retried = client.post(
        "/v1/sessions/s-resume-resolve/answer",
        json={
            "interrupt_id": iid,
            "answer": {"selected": ["全都要"], "other_text": ""},
            "stream": stream,
        },
        headers={"Authorization": "Bearer sk-test"},
    )
    assert retried.status_code == 200, retried.text
    assert "好，全都要。" in retried.text
    assert "event: anila.error" not in retried.text


@pytest.mark.parametrize(
    "raw",
    [
        "Bearer eyJ.resume-secret",
        "prefix sk-live-RESUME-ASK-DO-NOT-LEAK suffix",
        "prefix csk-RESUME-ASK-DO-NOT-LEAK suffix",
        "api_key=RESUME-ASK-DO-NOT-LEAK",
        '"api_key": "RESUME-ASK-DO-NOT-LEAK"',
        "password=RESUME-ASK-DO-NOT-LEAK",
        '"password": "RESUME-ASK-DO-NOT-LEAK"',
        "see http://10.1.2.3/nope?token=RESUME-ASK-DO-NOT-LEAK&x=1",
        "token=RESUME-ASK-DO-NOT-LEAK",
        '"access_token": "RESUME-ASK-DO-NOT-LEAK"',
        "bearer RESUME-ASK-DO-NOT-LEAK",
    ],
)
def test_scrub_diagnostic_value_masks_credentials_and_query_strings(raw: str) -> None:
    scrubbed = str(_scrub_diagnostic_value(raw))
    assert "RESUME-ASK-DO-NOT-LEAK" not in scrubbed
    assert "eyJ.resume-secret" not in scrubbed
    assert "token=" not in scrubbed


def test_scrub_diagnostic_value_keeps_benign_text() -> None:
    assert _scrub_diagnostic_value("模型名稱不存在") == "模型名稱不存在"
    assert _scrub_diagnostic_value("token count is 3") == "token count is 3"


def _resume_request() -> Request:
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/sessions/s/answer",
            "raw_path": b"/v1/sessions/s/answer",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 123),
            "server": ("test", 80),
        }
    )


@respx.mock
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("fail_at", ["persist", "push"])
def test_followup_ask_persist_failure_restores_original_interrupt(
    db_path: Path,
    stream: bool,
    fail_at: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A follow-up ASK that fails to save must leave the original pause pending."""
    calls = {"n": 0}

    def csp_handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        body = json.loads(request.content.decode())
        if calls["n"] == 1:
            return httpx.Response(200, json=_llm_reply("ASK:要挑哪幾個？|一|全都要"))
        text = "ASK:第二次問？|C|D" if calls["n"] == 2 else "好，第二次。"
        if body.get("stream"):
            return httpx.Response(
                200,
                content=_llm_sse(text).encode("utf-8"),
                headers={"Content-Type": "text/event-stream"},
            )
        return httpx.Response(200, json=_llm_reply(text))

    respx.get(CSP_AGENTS_URL).mock(return_value=httpx.Response(200, json={"data": []}))
    respx.post(CSP_URL).mock(side_effect=csp_handler)

    from anila_core.api import router_server

    client = TestClient(create_router_app(session_db_path=str(db_path)))
    iid = _pause_router_ask(client, "s-follow-fail")
    original_persist = router_server._persist_router_ask
    if fail_at == "persist":
        async def _boom(session, *, ask, user_message):
            raise RuntimeError("follow-up persist failed")

        monkeypatch.setattr(router_server, "_persist_router_ask", _boom)
    else:
        original_push = SqliteSession.push_interrupt

        async def _boom_push(self, record):
            if record.id != iid:
                raise RuntimeError("follow-up push failed")
            await original_push(self, record)

        monkeypatch.setattr(SqliteSession, "push_interrupt", _boom_push)

    try:
        resumed = client.post(
            "/v1/sessions/s-follow-fail/answer",
            json={
                "interrupt_id": iid,
                "answer": {"selected": ["全都要"], "other_text": ""},
                "stream": stream,
            },
            headers={"Authorization": "Bearer sk-test"},
        )
    except Exception as exc:
        assert "follow-up" in str(exc)
    else:
        assert resumed.status_code == 200, resumed.text
        assert "event: anila.error" in resumed.text

    state = client.get(
        "/v1/sessions/s-follow-fail/state",
        headers={"Authorization": "Bearer sk-test"},
    )
    assert state.status_code == 200, state.text
    body = state.json()
    assert [item["id"] for item in body["pending_interrupts"]] == [iid]
    stored = json.dumps(body["messages"], ensure_ascii=False)
    assert "已選擇" not in stored
    assert "第二次問" not in stored

    if fail_at == "persist":
        monkeypatch.setattr(router_server, "_persist_router_ask", original_persist)
    retried = client.post(
        "/v1/sessions/s-follow-fail/answer",
        json={
            "interrupt_id": iid,
            "answer": {"selected": ["全都要"], "other_text": ""},
            "stream": stream,
        },
        headers={"Authorization": "Bearer sk-test"},
    )
    assert retried.status_code == 200, retried.text
    assert "好，第二次。" in retried.text
    assert "event: anila.error" not in retried.text


@respx.mock
async def test_cancel_during_restore_still_repends_interrupt(db_path: Path) -> None:
    """Cancelling the request while the interrupt is being put back still restores it."""
    record = to_record(
        InterruptItem(
            kind="ask_user",
            payload={
                "question": "要挑哪幾個？",
                "options": ["一", "全都要"],
                "multi_select": False,
                "allow_other": True,
            },
        ),
        tool_call=ToolCall(id="call-cancel", name="ask_user", input={}),
        sibling_results=[],
    )
    session = SqliteSession(str(db_path), "s-cancel")
    await session.add_items(
        [
            UserMessage(content="挑功能"),
            AssistantMessage(content="要挑哪幾個？"),
        ]
    )
    await session.push_interrupt(record)
    iid = record.id

    started = asyncio.Event()
    release = asyncio.Event()
    original_push = session.push_interrupt

    async def slow_push(item):
        if item.id == iid:
            started.set()
            await release.wait()
        await original_push(item)

    session.push_interrupt = slow_push  # type: ignore[method-assign]
    reset_http_client()
    respx.post(CSP_URL).mock(return_value=httpx.Response(404, content=b"missing"))

    async def consume() -> None:
        response = await _resume_router_ask(
            "s-cancel",
            session,
            {
                "interrupt_id": iid,
                "answer": {"selected": ["全都要"], "other_text": ""},
                "stream": False,
            },
            caller_api_key="sk-test",
            request=_resume_request(),
        )
        async for _chunk in response.body_iterator:
            pass

    consumer = asyncio.create_task(consume())
    await asyncio.wait_for(started.wait(), timeout=5)
    consumer.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await consumer

    pending = await session.pending_interrupts()
    assert [item.id for item in pending] == [iid]


_ASK_STAR = "ASK*:要挑哪幾個來拆成三種版本？|一 環境感測器|六 電網天線|全都要"


def _ask_star_payload(body: str, *, stream: bool) -> dict:
    if stream:
        events = _sse_events(body)
        names = [name for name, _data in events]
        assert "anila.interrupt_requested" in names
        event = json.loads(events[names.index("anila.interrupt_requested")][1])
        assert _visible_sse_text(body) == ""
        assert "ASK*:" not in _visible_sse_text(body)
        return event["payload"]
    parsed = json.loads(body)
    assert parsed["choices"][0]["message"]["content"] == ""
    assert "ASK*:" not in parsed["choices"][0]["message"]["content"]
    return parsed["anila_meta"]["interrupt"]["payload"]


@respx.mock
@pytest.mark.parametrize("stream", [False, True])
def test_ask_star_payload_multi_true_survives_reload(
    db_path: Path, stream: bool
) -> None:
    """``ASK*:`` pauses like ``ASK:`` and the stored interrupt keeps ``multi``."""
    respx.get(CSP_AGENTS_URL).mock(return_value=httpx.Response(200, json={"data": []}))
    if stream:
        upstream = httpx.Response(
            200,
            content=_llm_sse(_ASK_STAR + "\n").encode("utf-8"),
            headers={"Content-Type": "text/event-stream"},
        )
    else:
        upstream = httpx.Response(200, json=_bare_completion(_ASK_STAR))
    respx.post(CSP_URL).mock(return_value=upstream)

    client = TestClient(create_router_app(session_db_path=str(db_path)))
    resp = client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "挑功能"}],
            "stream": stream,
            "session_id": "s-ask-star",
        },
        headers={"Authorization": "Bearer sk-test"},
    )
    assert resp.status_code == 200, resp.text
    payload = _ask_star_payload(resp.text, stream=stream)
    assert payload["multi"] is True
    assert payload["question"] == "要挑哪幾個來拆成三種版本？"
    assert [item["value"] for item in payload["options"]] == [
        "一 環境感測器",
        "六 電網天線",
        "全都要",
    ]

    reopened = TestClient(create_router_app(session_db_path=str(db_path)))
    state = reopened.get(
        "/v1/sessions/s-ask-star/state",
        headers={"Authorization": "Bearer sk-test"},
    )
    assert state.status_code == 200, state.text
    pending = state.json()["pending_interrupts"]
    assert len(pending) == 1
    assert pending[0]["payload"]["multi"] is True
    assert pending[0]["payload"]["question"] == "要挑哪幾個來拆成三種版本？"


@respx.mock
def test_streaming_ask_star_split_between_ask_and_star(db_path: Path) -> None:
    """A delta that ends between ``ASK`` and ``*`` still pauses as multi."""
    respx.get(CSP_AGENTS_URL).mock(return_value=httpx.Response(200, json={"data": []}))
    respx.post(CSP_URL).mock(
        return_value=httpx.Response(
            200,
            content=_llm_sse("ASK", "*:要挑哪幾個？|甲|乙\n").encode("utf-8"),
            headers={"Content-Type": "text/event-stream"},
        )
    )

    client = TestClient(create_router_app(session_db_path=str(db_path)))
    resp = client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "挑幾個"}],
            "stream": True,
            "session_id": "s-ask-star-split",
        },
        headers={"Authorization": "Bearer sk-test"},
    )
    assert resp.status_code == 200, resp.text
    events = _sse_events(resp.text)
    names = [name for name, _data in events]
    assert "anila.interrupt_requested" in names
    payload = json.loads(events[names.index("anila.interrupt_requested")][1])
    assert payload["payload"]["multi"] is True
    assert payload["payload"]["question"] == "要挑哪幾個？"
    assert [item["value"] for item in payload["payload"]["options"]] == ["甲", "乙"]
    assert "ASK*:" not in _visible_sse_text(resp.text)
    assert _visible_sse_text(resp.text) == ""


def test_router_answer_resume_message_lists_every_selection() -> None:
    """Single and multi answers both name every pick, plus free text."""
    record = to_record(
        InterruptItem(kind="ask_user", payload={"question": "要挑哪幾個？"}),
        tool_call=ToolCall(id="c-ask", name="ask_user", input={}),
        sibling_results=[],
    )
    both = _router_answer_resume_message(
        record,
        {
            "selected": ["一 環境感測器", "六 電網天線"],
            "other_text": "外加說明",
        },
    )
    assert _resume_content_text(both) == (
        "已選擇：一 環境感測器、六 電網天線；補充：外加說明"
    )
    one = _router_answer_resume_message(
        record, {"selected": ["晴天"], "other_text": "備註"}
    )
    assert _resume_content_text(one) == "已選擇：晴天；補充：備註"
    only = _router_answer_resume_message(
        record, {"selected": ["2025"], "other_text": ""}
    )
    assert _resume_content_text(only) == "已選擇：2025"
