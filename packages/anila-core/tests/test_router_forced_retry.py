"""「改用院內規章重查」——a forced turn answers, it never gets dispatched away.

Owner ruling Q40 (2026-08-07): when the reader presses the retry button, *that
turn* is answered directly with institutional-regulation retrieval attached. It
is never handed to an agent. The button's whole point is that the Router already
guessed wrong once; letting the same guess run again would make the button a
placebo — the user presses it, something happens, and they still get the agent
they were trying to get away from. (Lesson 4 of this project: a control that
looks like it worked but did not is worse than no control.)

Two layers, pinned separately because either one alone is a single point of
failure:

* **(a) the prompt** — a forced turn is asked with a variant that has no routing
  machinery in it: no agent list, no DISPATCH rule. A model that is never told
  the syntax mostly will not emit it.
* **(b) the parser hard guard** — ``_parse_dispatch_unless_forced``. Even when a
  model writes something DISPATCH-shaped anyway (they do: gpt-oss quotes the
  routing rules while thinking, and this project has already been bitten by
  prose that merely *mentions* the syntax), a forced turn parses to "no
  dispatch". Every path that can dispatch goes through it. Remove it from any
  one site and a test here goes red.

The paths are enumerated rather than sampled because the marker's own history
(Task 5) is the cautionary tale: the field was wired on the payload path the
brief named, and the streaming path users actually hit was missed.

The third section pins something adjacent but load-bearing: the kb_* fields CSP
stamps have to *reach the client*, or every badge Task 8 drew stays blank no
matter how well retrieval works. Both Router meta exits are asserted — the
non-stream merge and the streaming ``anila.meta`` event.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
import respx
from fastapi.testclient import TestClient

import anila_core.api.router_server as rs
from anila_core.api.router_server import create_router_app
from anila_core.config import settings
from anila_core.memory import close_all_connections


CSP_BASE = settings.csp_base_url
CSP_URL = f"{CSP_BASE}/v1/chat/completions"
CSP_AGENTS_URL = f"{CSP_BASE}/v1/agents"

ROUTE_HEADER = "X-ANILA-Route"
FORCED = {ROUTE_HEADER: "forced"}

AGENT_ID = "image-generator"


@pytest_asyncio.fixture
async def db_path(tmp_path: Path):
    db = tmp_path / "router-forced-retry.db"
    yield db
    await close_all_connections()


# ---------------------------------------------------------------------------
# Fixtures shaped like the real wire
# ---------------------------------------------------------------------------


def _agents_payload() -> dict:
    """A registered agent has to exist, or "did not dispatch" proves nothing."""
    return {
        "data": [
            {
                "id": AGENT_ID,
                "name": "Image Generator",
                "description_for_router": "Draws pictures",
                "endpoint_url": "http://image-generator",
                "requires_encryption": False,
            }
        ]
    }


def _completion(content: str, model: str = "router-llm", meta: dict | None = None) -> dict:
    payload: dict[str, Any] = {
        "id": "chatcmpl-1",
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
    if meta is not None:
        payload["anila_meta"] = meta
    return payload


def _sse(content: str, meta: dict | None = None) -> httpx.Response:
    """One content delta, optionally CSP's ``anila.meta`` frame, then DONE.

    Frame shapes copied from what CSP actually writes: the kb fragment rides a
    **named** ``event: anila.meta`` SSE frame (``proxy.py:_sse_with_kb_meta``),
    not an OpenAI chunk field. That distinction is the whole bug this file
    catches — a parser that only reads ``data:`` lines drops it silently.
    """
    body = 'data: {"choices":[{"delta":{"content":%s}}]}\n\n' % json.dumps(content)
    if meta is not None:
        body += "event: anila.meta\ndata: " + json.dumps(meta, ensure_ascii=False) + "\n\n"
    body += "data: [DONE]\n\n"
    return httpx.Response(
        200,
        content=body.encode("utf-8"),
        headers={"Content-Type": "text/event-stream"},
    )


# Real kb payload shapes (``proxy.py:_kb_meta_fragment``). Two hits on purpose:
# with one hit, "only forwards the first" and "off by one" both survive.
HIT_A = {
    "collection_id": 7,
    "document_id": 21,
    "filename": "人事管理規則.pdf",
    "content": "第三條 差勤一律採線上簽核，紙本不再受理。",
    "score": 0.91,
}
HIT_B = {
    "collection_id": 7,
    "document_id": 34,
    "filename": "差勤作業要點.docx",
    "content": "第五條 出差應於事前完成申請程序。",
    "score": 0.78,
}


def _kb_meta() -> dict:
    """``partial_error`` on purpose: it is the state that carries *both* hits
    and failures, so a merge that keeps only one of the two fields shows up."""
    return {
        "kb_state": "partial_error",
        "kb_hits": [HIT_A, HIT_B],
        "kb_failed_collections": [9],
        "citations": [
            {
                "id": f"kb:7:{HIT_A['document_id']}:1",
                "title": HIT_A["filename"],
                "score": HIT_A["score"],
                "snippet": HIT_A["content"],
            },
            {
                "id": f"kb:7:{HIT_B['document_id']}:2",
                "title": HIT_B["filename"],
                "score": HIT_B["score"],
                "snippet": HIT_B["content"],
            },
        ],
    }


# A DISPATCH line the Router would obey on a normal turn. Kept in one place so
# every "forced did not dispatch" test is provably using bait that works.
DISPATCH_LINE = f"DISPATCH:{AGENT_ID}:畫一張差旅費流程圖"

# Long enough (>= 12 chars, dense CJK) that the streaming state machine commits
# to "answering" mid-stream instead of falling out through the detecting exit.
LONG_ANSWER = (
    "依本院差旅費報支要點第三條規定，出差人員應於事前完成申請程序，"
    "並於返院後十日內檢附相關單據辦理核銷，逾期者須敘明理由。"
)


class _Downstream:
    """Every CSP call the Router made this turn, classified by purpose.

    Same classification as ``test_router_direct_header.py`` (recompose by its
    system prompt, dispatch by a model that is not the router's own). Repeated
    rather than imported because these two files pin different invariants and a
    shared helper would let a change to one silently reshape the other.
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def record(self, request: httpx.Request) -> None:
        payload = json.loads(request.content)
        messages = payload.get("messages") or []
        system = messages[0].get("content", "") if messages else ""
        if system == rs._RECOMPOSE_SYSTEM_PROMPT:
            kind = "recompose"
        elif payload.get("model") != rs.current_router_model():
            kind = "dispatch"
        else:
            kind = "router-llm"
        self.calls.append({"kind": kind, "headers": request.headers, "payload": payload})

    def of(self, kind: str) -> list[dict]:
        return [c for c in self.calls if c["kind"] == kind]

    def system_prompt_of(self, kind: str) -> str:
        matches = self.of(kind)
        assert matches, f"no {kind!r} call was made — the fixture script is wrong"
        messages = matches[0]["payload"]["messages"]
        return messages[0].get("content", "")


def _run_turn(
    db_path: Path,
    *,
    replies: list[dict | httpx.Response],
    stream: bool = False,
    inbound_headers: dict[str, str] | None = None,
    extra_body: dict | None = None,
) -> tuple[_Downstream, str]:
    """Drive one Router turn; return (downstream calls, response body text).

    The body matters here in a way it did not for Task 5: "forced did not
    dispatch" is only half the claim — the other half is that the user still
    got an answer, and the kb pins read the response too.
    """
    seen = _Downstream()
    iterator = iter(replies)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.record(request)
        try:
            nxt = next(iterator)
        except StopIteration:  # pragma: no cover — a script bug, not a fixture
            return httpx.Response(500, json={"detail": "script exhausted"})
        if isinstance(nxt, httpx.Response):
            return nxt
        return httpx.Response(200, json=nxt)

    respx.get(CSP_AGENTS_URL).mock(
        return_value=httpx.Response(200, json=_agents_payload())
    )
    respx.post(CSP_URL).mock(side_effect=handler)

    client = TestClient(create_router_app(session_db_path=str(db_path)))
    headers = {"Authorization": "Bearer sk-test"}
    headers.update(inbound_headers or {})
    response = client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "請問差旅費怎麼報"}],
            "stream": stream,
            **(extra_body or {}),
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return seen, response.text


def _stream_text(body: str) -> str:
    """Reassemble what the user actually reads out of the SSE body.

    Not a substring search on the raw body: the Router soft-chunks its answer on
    two of these paths, so the text arrives split across several ``data:``
    frames and a naive ``in body`` check silently fails on a perfectly good
    answer (and would silently pass if the answer were mangled).
    """
    out: list[str] = []
    for block in body.split("\n\n"):
        lines = block.split("\n")
        if any(l.startswith("event:") for l in lines):
            continue
        data = next((l[6:] for l in lines if l.startswith("data: ")), None)
        if not data or data == "[DONE]":
            continue
        chunk = json.loads(data)
        for choice in chunk.get("choices") or []:
            out.append((choice.get("delta") or {}).get("content") or "")
    return "".join(out)


def _meta_events(body: str) -> list[dict]:
    """Every ``anila.meta`` frame the Router emitted, parsed."""
    events: list[dict] = []
    for block in body.split("\n\n"):
        lines = block.split("\n")
        name = next((l[6:].strip() for l in lines if l.startswith("event:")), None)
        data = next((l[5:].strip() for l in lines if l.startswith("data:")), None)
        if name == "anila.meta" and data:
            events.append(json.loads(data))
    return events


# ---------------------------------------------------------------------------
# Layer (b): the hard guard — every path that can dispatch
# ---------------------------------------------------------------------------


@respx.mock
def test_the_bait_really_dispatches_when_the_turn_is_not_forced(db_path: Path) -> None:
    """Control. Without this, every test below could pass on a broken fixture.

    If ``DISPATCH_LINE`` ever stopped parsing (agent renamed, regex tightened),
    "no dispatch happened" would become vacuously true everywhere and the guard
    could be deleted with the suite still green.
    """
    seen, _ = _run_turn(
        db_path,
        replies=[
            _completion(DISPATCH_LINE),
            _completion("here is your image", model=AGENT_ID),
            _completion("整理後的回覆"),
        ],
    )
    assert len(seen.of("dispatch")) == 1


@respx.mock
def test_forced_non_stream_answers_instead_of_dispatching(db_path: Path) -> None:
    seen, body = _run_turn(
        db_path,
        replies=[_completion(f"{LONG_ANSWER}\n{DISPATCH_LINE}")],
        inbound_headers=FORCED,
    )
    assert seen.of("dispatch") == []
    assert LONG_ANSWER in json.loads(body)["choices"][0]["message"]["content"]


# The streaming state machine has three separate doors to an agent, and they
# are reached by three different shapes of model output. Guarding one and
# calling it done is exactly the shape Task 5's history warns about, so each is
# driven and each is proved to be a real door by the control test below.
STREAM_BAITS = [
    pytest.param(DISPATCH_LINE + "\n", id="mid-stream-commit"),
    pytest.param(DISPATCH_LINE, id="end-of-stream-final-parse"),
    pytest.param(f"DISPATCH:{AGENT_ID}:", id="query-less-salvage"),
]


@pytest.mark.parametrize("reply_text", STREAM_BAITS)
@respx.mock
def test_the_streaming_bait_really_dispatches_when_not_forced(
    db_path: Path, reply_text: str
) -> None:
    """Control for the three streaming doors. Without it, a guard could be
    deleted and these tests would still pass on bait that never worked."""
    seen, _ = _run_turn(
        db_path,
        replies=[
            _sse(reply_text),
            _completion("here is your image", model=AGENT_ID),
            _completion("整理後的回覆"),
        ],
        stream=True,
    )
    assert len(seen.of("dispatch")) == 1


@pytest.mark.parametrize("reply_text", STREAM_BAITS)
@respx.mock
def test_forced_streaming_answers_instead_of_dispatching(
    db_path: Path, reply_text: str
) -> None:
    """The path users actually hit — the mid-stream three-state machine."""
    seen, _ = _run_turn(
        db_path,
        replies=[_sse(reply_text)],
        stream=True,
        inbound_headers=FORCED,
    )
    assert seen.of("dispatch") == []


@respx.mock
def test_a_forced_streaming_turn_still_delivers_its_answer(db_path: Path) -> None:
    """Suppressing dispatch must not suppress the reply — a button that leaves
    the bubble empty is the same placebo in a different costume."""
    _seen, body = _run_turn(
        db_path,
        replies=[_sse(LONG_ANSWER)],
        stream=True,
        inbound_headers=FORCED,
    )
    assert LONG_ANSWER in _stream_text(body)


@respx.mock
def test_forced_multi_turn_non_stream_answers_instead_of_dispatching(
    db_path: Path,
) -> None:
    seen, body = _run_turn(
        db_path,
        replies=[_completion(f"{LONG_ANSWER}\n{DISPATCH_LINE}")],
        extra_body={"anila_multi_turn": 2},
        inbound_headers=FORCED,
    )
    assert seen.of("dispatch") == []
    assert LONG_ANSWER in json.loads(body)["choices"][0]["message"]["content"]


@respx.mock
def test_forced_multi_turn_streaming_answers_instead_of_dispatching(
    db_path: Path,
) -> None:
    seen, body = _run_turn(
        db_path,
        replies=[_completion(f"{LONG_ANSWER}\n{DISPATCH_LINE}")],
        stream=True,
        extra_body={"anila_multi_turn": 2},
        inbound_headers=FORCED,
    )
    assert seen.of("dispatch") == []
    assert LONG_ANSWER in _stream_text(body)


# ---------------------------------------------------------------------------
# The multi-turn loop's own guard
#
# Unreachable from the API on a forced turn (the loop is only entered *after* a
# first dispatch, which forced prevents), so an end-to-end test cannot see it —
# delete the guard there and every test above stays green. Q40 names multi-turn
# explicitly and the guard must die red when removed, so the loop is driven
# directly. It is reachable in production the moment any future change lets a
# forced turn reach the loop by another door.
# ---------------------------------------------------------------------------


class _StubRegistry:
    """Minimal ``registry.get`` — the loop only ever asks "is this agent real"."""

    def get(self, caller_api_key: str, agent_id: str) -> Any:
        return object() if agent_id == AGENT_ID else None


@pytest.mark.asyncio
@respx.mock
async def test_the_multi_turn_loop_does_not_dispatch_on_a_forced_turn(
    db_path: Path,
) -> None:
    seen = _Downstream()

    def handler(request: httpx.Request) -> httpx.Response:
        seen.record(request)
        return httpx.Response(200, json=_completion(f"{LONG_ANSWER}\n{DISPATCH_LINE}"))

    respx.post(CSP_URL).mock(side_effect=handler)

    (
        _agent_response,
        _last_agent_id,
        _last_manifest,
        _base_trace,
        final_text,
        _reasoning,
    ) = await rs._multi_turn_dispatch(
        caller_api_key="sk-test",
        router_llm_headers={ROUTE_HEADER: "forced"},
        route_signal="forced",
        routing_messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "請問差旅費怎麼報"},
        ],
        first_llm_text=DISPATCH_LINE,
        first_agent_id=AGENT_ID,
        first_agent_response={"content": "第一輪 agent 的回覆", "error": None},
        first_manifest=object(),
        registry=_StubRegistry(),
        base_trace=[],
        max_iterations=3,
        started_at=0.0,
        session_id="sess-1",
        router_reasoning="",
    )

    assert seen.of("dispatch") == []
    # No dispatch means the loop treated the reply as the final synthesis.
    assert final_text is not None and LONG_ANSWER in final_text


@pytest.mark.asyncio
@respx.mock
async def test_the_multi_turn_loop_still_dispatches_when_not_forced(
    db_path: Path,
) -> None:
    """Control for the test above — the loop's dispatching ability is intact."""
    seen = _Downstream()
    replies = iter(
        [
            _completion(DISPATCH_LINE),
            _completion("second agent reply", model=AGENT_ID),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        seen.record(request)
        return httpx.Response(200, json=next(replies))

    respx.post(CSP_URL).mock(side_effect=handler)

    await rs._multi_turn_dispatch(
        caller_api_key="sk-test",
        router_llm_headers={ROUTE_HEADER: "direct"},
        route_signal="direct",
        routing_messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "請問差旅費怎麼報"},
        ],
        first_llm_text=DISPATCH_LINE,
        first_agent_id=AGENT_ID,
        first_agent_response={"content": "第一輪 agent 的回覆", "error": None},
        first_manifest=object(),
        registry=_StubRegistry(),
        base_trace=[],
        max_iterations=2,
        started_at=0.0,
        session_id="sess-1",
        router_reasoning="",
    )

    assert len(seen.of("dispatch")) == 1


# ---------------------------------------------------------------------------
# Layer (a): the prompt a forced turn is asked with
# ---------------------------------------------------------------------------


@respx.mock
def test_a_forced_turn_is_asked_with_a_prompt_that_cannot_dispatch(
    db_path: Path,
) -> None:
    """No syntax, no agent list — nothing for the model to route with."""
    seen, _ = _run_turn(
        db_path, replies=[_completion(LONG_ANSWER)], inbound_headers=FORCED
    )
    prompt = seen.system_prompt_of("router-llm")
    assert "DISPATCH" not in prompt
    assert AGENT_ID not in prompt


@respx.mock
def test_an_ordinary_turn_keeps_the_routing_prompt(db_path: Path) -> None:
    """Control: the forced prompt swap must not leak onto every other turn."""
    seen, _ = _run_turn(db_path, replies=[_completion(LONG_ANSWER)])
    prompt = seen.system_prompt_of("router-llm")
    assert "DISPATCH" in prompt
    assert AGENT_ID in prompt


@respx.mock
def test_the_forced_prompt_swap_does_not_disturb_the_marker(db_path: Path) -> None:
    """Task 5's mechanism must keep working — that is how retrieval is attached
    in the first place. A forced turn with no marker is a button that does
    nothing at all."""
    seen, _ = _run_turn(
        db_path, replies=[_completion(LONG_ANSWER)], inbound_headers=FORCED
    )
    assert seen.of("router-llm")[0]["headers"].get(ROUTE_HEADER) == "forced"


# ---------------------------------------------------------------------------
# The kb fields CSP stamps have to reach the client — both meta exits
# ---------------------------------------------------------------------------


def _assert_kb_survived(meta: dict) -> None:
    assert meta.get("kb_state") == "partial_error"
    assert meta.get("kb_hits") == [HIT_A, HIT_B]
    assert meta.get("kb_failed_collections") == [9]
    assert [c["id"] for c in meta.get("citations") or []] == [
        f"kb:7:{HIT_A['document_id']}:1",
        f"kb:7:{HIT_B['document_id']}:2",
    ]


# Every exit is asserted on a forced turn **and an ordinary one**. The
# pass-through is not a feature of the retry button: Task 8's badges are drawn
# for every Router-answered turn, and most turns that carry regulations were
# never forced — CSP attaches retrieval whenever the answer-channel marker is
# present, which is always. Asserting only the forced case would leave "capture
# the meta only when route_signal is forced" alive: one line, every ordinary
# turn silently loses its badges, whole suite green.
KB_TURNS = [pytest.param(None, id="ordinary-turn"), pytest.param(FORCED, id="forced-turn")]


@pytest.mark.parametrize("inbound", KB_TURNS)
@respx.mock
def test_kb_fields_survive_the_non_stream_meta_exit(
    db_path: Path, inbound: dict[str, str] | None
) -> None:
    _seen, body = _run_turn(
        db_path,
        replies=[_completion(LONG_ANSWER, meta=_kb_meta())],
        inbound_headers=inbound,
    )
    _assert_kb_survived(json.loads(body)["anila_meta"])


@pytest.mark.parametrize("inbound", KB_TURNS)
@respx.mock
def test_kb_fields_survive_the_streaming_meta_exit(
    db_path: Path, inbound: dict[str, str] | None
) -> None:
    """The exit no one had evidence for. The SPA streams, so if this drops the
    fields, every badge Task 8 drew is blank in production while retrieval works
    perfectly — the failure mode with no error message."""
    _seen, body = _run_turn(
        db_path,
        replies=[_sse(LONG_ANSWER, meta=_kb_meta())],
        stream=True,
        inbound_headers=inbound,
    )
    events = _meta_events(body)
    assert events, "the Router emitted no anila.meta frame at all"
    _assert_kb_survived(events[-1])


@pytest.mark.parametrize("inbound", KB_TURNS)
@respx.mock
def test_kb_fields_survive_the_short_answer_streaming_exit(
    db_path: Path, inbound: dict[str, str] | None
) -> None:
    """A second streaming exit. Short answers never commit mid-stream and leave
    through the end-of-stream sanitizer instead — a fix applied to only one of
    the two would look complete."""
    _seen, body = _run_turn(
        db_path,
        replies=[_sse("好。", meta=_kb_meta())],
        stream=True,
        inbound_headers=inbound,
    )
    events = _meta_events(body)
    assert events, "the Router emitted no anila.meta frame at all"
    _assert_kb_survived(events[-1])


@pytest.mark.parametrize("inbound", KB_TURNS)
@respx.mock
def test_kb_fields_survive_the_multi_turn_streaming_meta_exit(
    db_path: Path, inbound: dict[str, str] | None
) -> None:
    _seen, body = _run_turn(
        db_path,
        replies=[_completion(LONG_ANSWER, meta=_kb_meta())],
        stream=True,
        extra_body={"anila_multi_turn": 2},
        inbound_headers=inbound,
    )
    events = _meta_events(body)
    assert events, "the Router emitted no anila.meta frame at all"
    _assert_kb_survived(events[-1])


# ---------------------------------------------------------------------------
# Fix round 1 / I1 — a dispatched turn must not wear the routing call's badge
#
# The Router issues its routing call to CSP *before* it knows whether the turn
# will be answered here or handed to an agent, so CSP attaches regulation
# retrieval to a call whose output is sometimes thrown away (Task 5 §1, an
# accepted cost). What must never happen is the opposite of this whole feature:
# the agent's answer arriving decorated with regulations it never consulted.
# "An answer wearing the wrong sources" is worse than "an answer with no
# sources" — the citation drawer would invite the reader to verify a claim
# against a document that had nothing to do with it.
#
# The behaviour is correct today, held by one line in the dispatch branch of
# ``_router_streaming`` that resets ``downstream_meta`` before the agent's own
# stream is read. Nothing pinned it. Worse, Task 9 made deleting that line
# *silent*: before the function-scope ``downstream_meta`` existed, removing the
# reset raised NameError on the spot; now it quietly swaps the source of every
# dispatched answer's metadata. That widened blast radius is this package's
# doing, so the pin belongs here.
# ---------------------------------------------------------------------------


def _dispatch_leak_replies() -> list:
    """Routing call retrieves regulations, then the turn dispatches anyway.

    The DISPATCH line carries **no terminator**, which is what makes this
    reachable: mid-stream detection refuses to commit without one, so the loop
    keeps reading, consumes CSP's ``anila.meta`` frame, and only dispatches at
    the end-of-stream final parse. A trailing newline would dispatch before the
    meta frame is ever seen and the test would pass vacuously.
    """
    return [
        _sse(DISPATCH_LINE, meta=_kb_meta()),
        _sse("這是 agent 畫好的圖說明。"),
        _completion("整理後的回覆"),
    ]


@respx.mock
def test_a_dispatched_streaming_turn_does_not_wear_the_routing_calls_kb_meta(
    db_path: Path,
) -> None:
    seen, body = _run_turn(db_path, replies=_dispatch_leak_replies(), stream=True)

    # Not vacuous: the turn really did end in a dispatch.
    assert len(seen.of("dispatch")) == 1
    events = _meta_events(body)
    assert events, "the Router emitted no anila.meta frame at all"
    final = events[-1]
    assert final.get("answering_agent_id") == AGENT_ID
    # The routing call's regulations must not have followed the answer out.
    assert final.get("kb_state") is None
    assert final.get("kb_hits") in (None, [])
    assert not final.get("citations")


@respx.mock
def test_a_dispatched_non_stream_turn_does_not_wear_the_routing_calls_kb_meta(
    db_path: Path,
) -> None:
    """Same invariant on the payload path, where the two metas live in separate
    locals rather than one reused variable — cheap to assert, and it stops a
    future refactor from importing the streaming path's shape."""
    seen, body = _run_turn(
        db_path,
        replies=[
            _completion(DISPATCH_LINE, meta=_kb_meta()),
            _completion("here is your image", model=AGENT_ID),
            _completion("整理後的回覆"),
        ],
    )
    assert len(seen.of("dispatch")) == 1
    final = json.loads(body)["anila_meta"]
    assert final.get("answering_agent_id") == AGENT_ID
    assert final.get("kb_state") is None
    assert final.get("kb_hits") in (None, [])
    assert not final.get("citations")


# ---------------------------------------------------------------------------
# Fix round 1 / I2 — a forced turn's bubble never shows machine syntax
#
# Proven on unmutated code by the reviewer: a forced turn whose model still
# emits a compliant DISPATCH line renders a bubble whose *entire contents* are
# ``DISPATCH:image-generator:…``. The dispatch is correctly suppressed — but the
# reader pressed「改用院內規章重查」and got a protocol string back. That is this
# package's own target shape in its purest form.
#
# The cause is a deliberate skip: when content carries a DISPATCH directive the
# Router leaves it untouched so the parser downstream can see it. On a forced
# turn there is no parser left to serve — Q40 already guaranteed no dispatch —
# so the skip has only its cost. Cleaning is scoped to forced turns; the
# ordinary dispatch flow must keep seeing the raw text, and the controls above
# (``test_the_bait_really_dispatches…``, the three streaming bait controls)
# fail loudly if it stops.
# ---------------------------------------------------------------------------


FORCED_BUBBLE_CASES = [
    pytest.param(f"DISPATCH:{AGENT_ID}:", id="query-less"),
    pytest.param(DISPATCH_LINE, id="complete-no-terminator"),
    pytest.param(DISPATCH_LINE + "\n", id="complete-with-terminator"),
    pytest.param(f"{LONG_ANSWER}\n{DISPATCH_LINE}", id="answer-then-dispatch"),
]


@pytest.mark.parametrize("model_text", FORCED_BUBBLE_CASES)
@respx.mock
def test_a_forced_streaming_bubble_never_shows_dispatch_syntax(
    db_path: Path, model_text: str
) -> None:
    _seen, body = _run_turn(
        db_path, replies=[_sse(model_text)], stream=True, inbound_headers=FORCED
    )
    visible = _stream_text(body)
    assert "DISPATCH" not in visible
    assert AGENT_ID not in visible
    # …and the bubble is not merely empty instead. A blank answer is the same
    # "I pressed it and nothing happened" the button exists to cure.
    assert visible.strip()


@pytest.mark.parametrize("model_text", FORCED_BUBBLE_CASES)
@respx.mock
def test_a_forced_non_stream_bubble_never_shows_dispatch_syntax(
    db_path: Path, model_text: str
) -> None:
    _seen, body = _run_turn(
        db_path, replies=[_completion(model_text)], inbound_headers=FORCED
    )
    visible = json.loads(body)["choices"][0]["message"]["content"]
    assert "DISPATCH" not in visible
    assert AGENT_ID not in visible
    assert visible.strip()


@respx.mock
def test_a_forced_turn_keeps_the_answer_that_came_with_the_stray_directive(
    db_path: Path,
) -> None:
    """Cleaning removes the directive, not the reply that surrounded it."""
    _seen, body = _run_turn(
        db_path,
        replies=[_completion(f"{LONG_ANSWER}\n{DISPATCH_LINE}")],
        inbound_headers=FORCED,
    )
    assert LONG_ANSWER in json.loads(body)["choices"][0]["message"]["content"]


@pytest.mark.parametrize("stream", [False, True])
@respx.mock
def test_an_ordinary_turn_answer_is_not_rewritten(db_path: Path, stream: bool) -> None:
    """The cleaning is scoped to forced turns. An ordinary direct answer must
    come through exactly as the model wrote it."""
    _seen, body = _run_turn(
        db_path, replies=[_sse(LONG_ANSWER) if stream else _completion(LONG_ANSWER)],
        stream=stream,
    )
    visible = (
        _stream_text(body) if stream
        else json.loads(body)["choices"][0]["message"]["content"]
    )
    assert visible == LONG_ANSWER


@respx.mock
def test_an_ordinary_turn_keeps_even_a_stray_query_less_directive(
    db_path: Path,
) -> None:
    """The scoping pin, and the only shape that can show it.

    A complete directive on an ordinary turn never reaches a bubble — it
    dispatches. A *query-less* one on the payload path does: ``_parse_dispatch``
    does not match it (no query), the non-stream path has no salvage door, so it
    falls through to a direct answer carrying the stray text. That makes it the
    one observable difference between "clean on forced turns" and "clean
    always", and without it the scope could be widened with the suite green.

    Pinning the status quo rather than improving it is deliberate: the fix round
    authorised a forced-turn-only change, and the ordinary skip exists to serve
    the real dispatch flow.
    """
    _seen, body = _run_turn(
        db_path,
        replies=[_completion(f"{LONG_ANSWER}\nDISPATCH:{AGENT_ID}:")],
    )
    visible = json.loads(body)["choices"][0]["message"]["content"]
    assert f"DISPATCH:{AGENT_ID}:" in visible


@pytest.mark.parametrize("model_text", FORCED_BUBBLE_CASES)
@respx.mock
def test_a_forced_multi_turn_streaming_bubble_never_shows_dispatch_syntax(
    db_path: Path, model_text: str
) -> None:
    """The third cleaned exit. ``anila_multi_turn>1`` streams through its own
    generator with its own direct-answer branch, so the cleaning is wired there
    separately — and an exit that is cleaned but not pinned is precisely the
    shape that let the un-cleaned bubble ship in the first place."""
    _seen, body = _run_turn(
        db_path,
        replies=[_completion(model_text)],
        stream=True,
        extra_body={"anila_multi_turn": 2},
        inbound_headers=FORCED,
    )
    visible = _stream_text(body)
    assert "DISPATCH" not in visible
    assert AGENT_ID not in visible
    assert visible.strip()


# ---------------------------------------------------------------------------
# Fix round 2 / I2 remainder — the sixth presentation exit
#
# Re-review found a door the five-exit census missed: the *thought-prefixed*
# commit. A model that leaks its analysis and then emits a directive slips every
# earlier gate on a forced turn —
#
#   * ``_has_dispatch_signal`` → None (forced, and the directive has no
#     terminator anyway);
#   * ``startswith("DISPATCH:")`` → False, the buffer starts with the thought;
#   * the cleaned commit is **skipped**, because it is guarded by
#     ``not _THOUGHT_PREFIX_RE.match(buf)`` and the prefix matches.
#
# …and lands on the density-boundary yield, which had no forced guard at all.
# Proven on unmutated HEAD before this fix, one SSE delta, all three prefix
# spellings. This is the same class as the exits already cleaned, at the same
# zero cost — the full buffer is in hand at that instant, so no line-buffering
# and no loss of streaming cadence is involved.
# ---------------------------------------------------------------------------


THOUGHT_PREFIXES = [
    pytest.param("Thought:", id="thought"),
    pytest.param("Thinking:", id="thinking"),
    pytest.param("**Thought**", id="bold-thought"),
]


@pytest.mark.parametrize("prefix", THOUGHT_PREFIXES)
@respx.mock
def test_a_forced_thought_prefixed_bubble_never_shows_dispatch_syntax(
    db_path: Path, prefix: str
) -> None:
    """The leaked-thought door. The answer must survive; the directive must not."""
    _seen, body = _run_turn(
        db_path,
        replies=[_sse(f"{prefix}\n{LONG_ANSWER}\n{DISPATCH_LINE}")],
        stream=True,
        inbound_headers=FORCED,
    )
    visible = _stream_text(body)
    assert "DISPATCH" not in visible
    assert AGENT_ID not in visible
    # The reply the thought was hiding still has to arrive — cleaning removes
    # the directive, never the answer.
    #
    # Asserted on the tail plus a length floor rather than on the whole string,
    # deliberately. Where the answer *begins* is decided by
    # ``_find_answer_split``, which ignores CJK before index 10 — so a
    # 9-character prefix (``Thought:\n``) costs the answer its first character
    # while a 10-character one (``Thinking:\n``) does not. That is pre-existing
    # behaviour of the thought splitter, untouched by this fix and outside its
    # scope. Pinning the exact string here would quietly promote a quirk of that
    # function into a contract of this one, and the next person to correct the
    # splitter would get a red test for doing the right thing.
    assert LONG_ANSWER[-40:] in visible
    assert len(visible.strip()) >= len(LONG_ANSWER) - 1
