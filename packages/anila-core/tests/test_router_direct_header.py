"""Router → CSP direct-answer signal (``X-ANILA-Route``).

CSP cannot otherwise tell that the Router is answering out of its own mouth:
the Router replies to the SPA itself, and the only thing CSP ever sees is a
plain ``/v1/chat/completions`` call. This header is what lets CSP decide
whether to attach institutional-regulation retrieval to that call.

⚠ **What the header does NOT mean.** The routing LLM call *precedes* the
routing decision — the decision is parsed out of that very call's output
(``router_server.py:1096`` → ``:1122`` → ``:1133``; streaming ``:2838`` →
mid-stream state machine). So the header cannot report a decision already
made. It marks *the Router's own answer channel*: "if this call's output
carries no DISPATCH line, its text goes straight to the user". The decision
itself is recorded where it always was — ``anila_meta.route.decision`` and
the ``direct`` trace step.

Consequences these tests pin:

* every Router-self LLM call is marked, on all three code paths
  (non-stream, single-shot streaming, multi-turn streaming);
* the calls that shape an **agent's** answer — the dispatch call and the
  recompose call — are never marked, so CSP never grafts regulations onto
  a reply an agent already sourced from its own library;
* a client may ask for retrieval (Task 9's "re-search the regulations"
  button) but may never dress its request up as the Router's own verdict:
  inbound values are normalised to ``forced``, never to ``direct``.
"""

from __future__ import annotations

from pathlib import Path

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


@pytest_asyncio.fixture
async def db_path(tmp_path: Path):
    db = tmp_path / "router-direct-header.db"
    yield db
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


def _completion(content: str, model: str = "router-llm") -> dict:
    return {
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


def _sse(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        content=(
            'data: {"choices":[{"delta":{"content":%s}}]}\n\n' % _json_str(content)
            + "data: [DONE]\n\n"
        ).encode("utf-8"),
        headers={"Content-Type": "text/event-stream"},
    )


def _json_str(text: str) -> str:
    import json

    return json.dumps(text)


class _Downstream:
    """Every request the Router made to CSP, classified by *purpose*.

    The brief sketched a single flat ``_capture_downstream_headers()``
    returning one header dict, but the Router issues several CSP calls per
    turn and they must be asserted separately — "the dispatch path does not
    forward it" is a statement about the *agent-facing* calls, not about the
    routing call, which is identical on both branches because it is what
    produces the branch.
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def record(self, request: httpx.Request) -> None:
        import json

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

    def route_header_of(self, kind: str) -> str | None:
        matches = self.of(kind)
        assert matches, f"no {kind!r} call was made — the fixture script is wrong"
        return matches[0]["headers"].get(ROUTE_HEADER)


def _run_turn(
    db_path: Path,
    *,
    replies: list[dict | httpx.Response],
    stream: bool = False,
    inbound_headers: dict[str, str] | None = None,
    extra_body: dict | None = None,
) -> _Downstream:
    """Drive one Router turn and return every CSP call it produced."""
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
    # Force the streaming generator to run to completion.
    _ = response.content
    return seen


# ---------------------------------------------------------------------------
# The signal itself — all three Router-self LLM paths
# ---------------------------------------------------------------------------


@respx.mock
def test_direct_answer_forwards_the_route_header(db_path: Path) -> None:
    """Router answering without an agent — CSP has to know it is the answerer."""
    seen = _run_turn(db_path, replies=[_completion("差旅費依規定核實報支。")])
    assert seen.route_header_of("router-llm") == "direct"


@respx.mock
def test_streaming_direct_answer_forwards_it_too(db_path: Path) -> None:
    """Two payload paths, and the streaming one is what users actually hit."""
    seen = _run_turn(
        db_path,
        replies=[_sse("差旅費依規定核實報支，請檢附單據。")],
        stream=True,
    )
    assert seen.route_header_of("router-llm") == "direct"


@respx.mock
def test_streaming_multi_turn_direct_answer_forwards_it_too(db_path: Path) -> None:
    """``anila_multi_turn>1`` streams through a third, separate code path."""
    seen = _run_turn(
        db_path,
        replies=[_completion("差旅費依規定核實報支。")],
        stream=True,
        extra_body={"anila_multi_turn": 2},
    )
    assert seen.route_header_of("router-llm") == "direct"


# ---------------------------------------------------------------------------
# The dispatch branch — agent-facing calls stay clean
# ---------------------------------------------------------------------------


@respx.mock
def test_dispatch_path_does_not_forward_it_to_agent_or_recompose(
    db_path: Path,
) -> None:
    """An agent searches its own library; CSP must not graft regulations on.

    The routing call is deliberately *not* asserted here: it is the same
    single call on both branches and it is issued before the DISPATCH line
    exists, so it cannot be conditioned on the outcome. What must stay clean
    are the calls that shape the agent's answer.
    """
    seen = _run_turn(
        db_path,
        replies=[
            _completion("DISPATCH:image-generator:畫一張圖"),
            _completion("here is your image", model="image-generator"),
            _completion("整理後的回覆"),  # recompose
        ],
    )
    assert seen.route_header_of("dispatch") is None
    assert seen.route_header_of("recompose") is None


@respx.mock
def test_a_client_route_header_does_not_leak_onto_the_dispatch_path(
    db_path: Path,
) -> None:
    """The inbound copy at :907 must be stripped, not merely overridden."""
    seen = _run_turn(
        db_path,
        replies=[
            _completion("DISPATCH:image-generator:畫一張圖"),
            _completion("here is your image", model="image-generator"),
            _completion("整理後的回覆"),
        ],
        inbound_headers={ROUTE_HEADER: "forced"},
    )
    assert seen.route_header_of("dispatch") is None
    assert seen.route_header_of("recompose") is None


# ---------------------------------------------------------------------------
# Client-supplied vs Router-decided — these must never be confusable
# ---------------------------------------------------------------------------


@respx.mock
def test_a_client_supplied_route_header_is_distinguishable(db_path: Path) -> None:
    """Task 9's button may force retrieval — but it says so in its own word."""
    seen = _run_turn(
        db_path,
        replies=[_completion("差旅費依規定核實報支。")],
        inbound_headers={ROUTE_HEADER: "forced"},
    )
    assert seen.route_header_of("router-llm") == "forced"


@pytest.mark.parametrize(
    "sent",
    ["direct", "DIRECT", "Direct", "  direct  ", "rubbish", "", "forced-ish"],
)
@respx.mock
def test_a_client_cannot_forge_the_routers_own_verdict(
    db_path: Path, sent: str
) -> None:
    """Anything that is not the forced request collapses to the Router's own
    verdict — a client can never make CSP's audit trail read "the machine
    decided this" when a human did."""
    seen = _run_turn(
        db_path,
        replies=[_completion("差旅費依規定核實報支。")],
        inbound_headers={ROUTE_HEADER: sent},
    )
    assert seen.route_header_of("router-llm") == "direct"


@pytest.mark.parametrize("sent", ["forced", "FORCED", "Forced", " forced "])
@respx.mock
def test_the_forced_request_is_normalised_not_echoed(
    db_path: Path, sent: str
) -> None:
    """CSP reads one exact token, not whatever casing a client happened to
    send — and echoing the raw value back is what mutation C does."""
    seen = _run_turn(
        db_path,
        replies=[_completion("差旅費依規定核實報支。")],
        inbound_headers={ROUTE_HEADER: sent},
    )
    assert seen.route_header_of("router-llm") == "forced"
