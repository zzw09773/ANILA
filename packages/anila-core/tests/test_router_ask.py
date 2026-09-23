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
* a reply that contains both a real ``DISPATCH:`` and an ``ASK:`` still dispatches
* prose that merely quotes ``ASK:`` is not a pause

The directive is only recognised on the FIRST line; anything later is prose the
reader sees unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest_asyncio
import respx
from fastapi.testclient import TestClient

import pytest

from anila_core.api.router_server import create_router_app
from anila_core.config import settings
from anila_core.memory import close_all_connections


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
    assert body["choices"][0]["message"]["content"] == "要查哪一年的規章？"
    assert "ASK:" not in body["choices"][0]["message"]["content"]
    assert body["anila_meta"]["route"]["decision"] == "ask"
    interrupt = body["anila_meta"]["interrupt"]
    assert interrupt["kind"] == "ask_user"
    assert interrupt["payload"]["question"] == "要查哪一年的規章？"
    assert [o["value"] for o in interrupt["payload"]["options"]] == ["2024", "2025"]


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
def test_dispatch_wins_when_reply_also_contains_ask(
    db_path: Path, _no_recompose: None
) -> None:
    """A real ``DISPATCH:`` line beats an ``ASK:`` in the same reply."""
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
    assert body["anila_meta"]["route"]["decision"] == "dispatch"
    assert "interrupt" not in body["anila_meta"]


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
    assert body["choices"][0]["message"]["content"] == "要查哪一年？"
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
    assert body["choices"][0]["message"]["content"] == "第一行是問題？"
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
    assert body["choices"][0]["message"]["content"] == "合成後要問？"
    assert "ASK:" not in body["choices"][0]["message"]["content"]
