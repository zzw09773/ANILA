"""Tests for Sprint 13 PR A2 — Router resume-proxy + ownership pinning.

Covers:

  * dispatching a query persists ``session_id → agent_id`` to the
    Router's SQLite ``session_owners`` table
  * ``GET /v1/sessions/{id}/state`` surfaces the recorded owner
  * ``POST /v1/sessions/{id}/answer`` 404s when no owner is recorded
  * ``POST /v1/sessions/{id}/answer`` proxies through CSP's new
    ``/v1/agents/{agent}/sessions/{id}/answer`` endpoint and the
    response carries the agent's SSE pass-through
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
import respx
from fastapi.testclient import TestClient

from anila_core.api.router_server import create_router_app
from anila_core.api.session_owner import get_session_owner
from anila_core.config import settings
from anila_core.memory import close_all_connections


@pytest.fixture(autouse=True)
def _disable_recompose(monkeypatch):
    """Reply re-composition is an orthogonal personalization pass with its own
    tests; stub it to a passthrough so these dispatch-behaviour tests don't see
    its extra recompose LLM call."""
    import anila_core.api.router_server as _rs

    async def _passthrough(agent_reply, caller_api_key, *, forwarded_headers=None):
        return agent_reply, "skipped"

    monkeypatch.setattr(_rs, "_recompose_reply", _passthrough)


CSP_BASE = settings.csp_base_url
CSP_URL = f"{CSP_BASE}/v1/chat/completions"
CSP_AGENTS_URL = f"{CSP_BASE}/v1/agents"
CSP_ME_URL = f"{CSP_BASE}/api/auth/me"


@pytest_asyncio.fixture
async def db_path(tmp_path: Path):
    db = tmp_path / "router-resume.db"
    yield db
    await close_all_connections()


def _llm_router_response(content: str) -> dict:
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


def _agent_registry_response(agent_id: str) -> dict:
    return {
        "data": [
            {
                "id": agent_id,
                "name": agent_id,
                "description_for_router": "Demo agent",
                "endpoint_url": f"http://{agent_id}",
                "requires_encryption": False,
            }
        ]
    }


def _jwt(label: str) -> str:
    return f"jwt-header.{label}.jwt-signature"


def _mock_me_same_user() -> None:
    respx.get(CSP_ME_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 99,
                "username": "resume-user",
                "email": None,
                "role": "user",
                "department_id": None,
                "department_name": None,
                "is_active": True,
                "is_approved": True,
                "local_password_disabled": False,
                "last_login_at": None,
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
            },
        )
    )


# ---------------------------------------------------------------------------
# Pin owner on dispatch
# ---------------------------------------------------------------------------


@respx.mock
def test_dispatch_pins_owning_agent(db_path: Path) -> None:
    """A successful dispatch writes (session_id, agent_id) to
    session_owners so the resume endpoint can find it later."""

    def csp_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        if body["model"] == "agent-pinme":
            return httpx.Response(
                200, json=_llm_router_response("agent reply")
            )
        return httpx.Response(
            200,
            json=_llm_router_response("DISPATCH:agent-pinme:do thing"),
        )

    respx.post(CSP_URL).mock(side_effect=csp_handler)
    respx.get(CSP_AGENTS_URL).mock(
        return_value=httpx.Response(
            200, json=_agent_registry_response("agent-pinme")
        )
    )

    app = create_router_app(session_db_path=str(db_path))
    client = TestClient(app)
    client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "do thing"}],
            "stream": False,
            "session_id": "s-pin",
        },
        headers={"Authorization": "Bearer sk-test"},
    )

    import asyncio
    owner = asyncio.run(
        get_session_owner(str(Path(db_path).resolve()), "s-pin")
    )
    assert owner == "agent-pinme"


@respx.mock
def test_state_endpoint_surfaces_owning_agent(db_path: Path) -> None:
    """The state response carries owner_agent_id post-dispatch."""

    def csp_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        if body["model"] == "agent-state":
            return httpx.Response(
                200, json=_llm_router_response("ok")
            )
        return httpx.Response(
            200,
            json=_llm_router_response("DISPATCH:agent-state:hi"),
        )

    respx.post(CSP_URL).mock(side_effect=csp_handler)
    respx.get(CSP_AGENTS_URL).mock(
        return_value=httpx.Response(
            200, json=_agent_registry_response("agent-state")
        )
    )

    app = create_router_app(session_db_path=str(db_path))
    client = TestClient(app)
    client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
            "session_id": "s-state-2",
        },
        headers={"Authorization": "Bearer sk-test"},
    )

    state_response = client.get(
        "/v1/sessions/s-state-2/state",
        headers={"Authorization": "Bearer sk-test"},
    )
    assert state_response.status_code == 200, state_response.text
    state = state_response.json()
    assert state["owner_agent_id"] == "agent-state"


# ---------------------------------------------------------------------------
# Resume endpoint
# ---------------------------------------------------------------------------


def test_answer_without_required_fields_400s(db_path: Path) -> None:
    app = create_router_app(session_db_path=str(db_path))
    client = TestClient(app)
    resp = client.post(
        "/v1/sessions/s-x/answer",
        json={"answer": "yes"},  # missing interrupt_id
        headers={"Authorization": "Bearer sk-test"},
    )
    assert resp.status_code == 400
    assert "interrupt_id" in resp.text


def test_answer_unknown_session_404s(db_path: Path) -> None:
    app = create_router_app(session_db_path=str(db_path))
    client = TestClient(app)
    resp = client.post(
        "/v1/sessions/s-unknown/answer",
        json={"interrupt_id": "i-1", "answer": "yes"},
        headers={"Authorization": "Bearer sk-test"},
    )
    assert resp.status_code == 404
    assert "owning agent" in resp.text


def test_answer_with_session_factory_returns_503(db_path: Path) -> None:
    """Custom session_factory paths skip the production owners table; the
    resume proxy must explicitly tell the caller it's unsupported."""
    from anila_core.memory import MemorySession

    app = create_router_app(session_factory=lambda sid: MemorySession(sid))
    client = TestClient(app)
    resp = client.post(
        "/v1/sessions/s-x/answer",
        json={"interrupt_id": "i-1", "answer": "yes"},
        headers={"Authorization": "Bearer sk-test"},
    )
    assert resp.status_code == 503
    assert "session_factory" in resp.text


@respx.mock
def test_answer_rejects_different_caller_before_proxy(db_path: Path) -> None:
    """A session pinned by one caller cannot be resumed by another caller."""

    def csp_chat_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        if body["model"] == "agent-resume":
            return httpx.Response(
                200, json=_llm_router_response("ok")
            )
        return httpx.Response(
            200,
            json=_llm_router_response("DISPATCH:agent-resume:hi"),
        )

    respx.post(CSP_URL).mock(side_effect=csp_chat_handler)
    respx.get(CSP_AGENTS_URL).mock(
        return_value=httpx.Response(
            200, json=_agent_registry_response("agent-resume")
        )
    )
    csp_resume_url = (
        f"{CSP_BASE}/v1/agents/agent-resume/sessions/s-resume-owned/answer"
    )
    resume_route = respx.post(csp_resume_url).mock(
        return_value=httpx.Response(500, json={"detail": "must not proxy"})
    )

    app = create_router_app(session_db_path=str(db_path))
    client = TestClient(app)

    pinned = client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
            "session_id": "s-resume-owned",
        },
        headers={"Authorization": "Bearer sk-owner"},
    )
    assert pinned.status_code == 200

    resume_resp = client.post(
        "/v1/sessions/s-resume-owned/answer",
        json={"interrupt_id": "i-7", "answer": "go ahead"},
        headers={"Authorization": "Bearer sk-other"},
    )
    assert resume_resp.status_code == 403
    assert resume_route.call_count == 0


@respx.mock
def test_answer_proxies_to_csp_resume_endpoint(db_path: Path) -> None:
    """Owner is pinned via dispatch, then a follow-up answer should
    POST to CSP's /v1/agents/{agent}/sessions/{id}/answer."""

    # 1) First, dispatch to pin owner = "agent-resume".
    def csp_chat_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        if body["model"] == "agent-resume":
            return httpx.Response(
                200, json=_llm_router_response("ok")
            )
        return httpx.Response(
            200,
            json=_llm_router_response("DISPATCH:agent-resume:hi"),
        )

    respx.post(CSP_URL).mock(side_effect=csp_chat_handler)
    respx.get(CSP_AGENTS_URL).mock(
        return_value=httpx.Response(
            200, json=_agent_registry_response("agent-resume")
        )
    )

    # 2) Mock the new CSP resume endpoint to stream a tiny SSE body.
    csp_resume_url = (
        f"{CSP_BASE}/v1/agents/agent-resume/sessions/s-resume/answer"
    )
    captured_resume_bodies: list[dict] = []

    def csp_resume_handler(request: httpx.Request) -> httpx.Response:
        captured_resume_bodies.append(json.loads(request.content.decode()))
        sse_body = (
            "event: anila.resumed\n"
            'data: {"interrupt_id":"i-7"}\n\n'
            'data: {"choices":[{"delta":{"content":"resumed text"}}]}\n\n'
            "data: [DONE]\n\n"
        )
        return httpx.Response(
            200,
            content=sse_body.encode("utf-8"),
            headers={"Content-Type": "text/event-stream"},
        )

    respx.post(csp_resume_url).mock(side_effect=csp_resume_handler)

    app = create_router_app(session_db_path=str(db_path))
    client = TestClient(app)

    # Pin owner.
    client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
            "session_id": "s-resume",
        },
        headers={"Authorization": "Bearer sk-test"},
    )

    # Resume.
    resume_resp = client.post(
        "/v1/sessions/s-resume/answer",
        json={"interrupt_id": "i-7", "answer": "go ahead"},
        headers={"Authorization": "Bearer sk-test"},
    )
    assert resume_resp.status_code == 200
    assert resume_resp.headers.get("X-Anila-Owner-Agent") == "agent-resume"
    assert resume_resp.headers.get("X-Anila-Session-Id") == "s-resume"

    body = resume_resp.text
    # Router emits its own anila.resumed marker first, then the agent's
    # SSE body passes through verbatim (which itself contains another
    # anila.resumed from the agent — both are valid).
    assert "event: anila.resumed" in body
    assert "resumed text" in body

    # CSP saw the user's payload verbatim.
    assert len(captured_resume_bodies) == 1
    assert captured_resume_bodies[0] == {
        "interrupt_id": "i-7",
        "answer": "go ahead",
    }


@respx.mock
def test_answer_accepts_refreshed_jwt_for_same_user(db_path: Path) -> None:
    """ask_user -> resume may cross access-token TTL; new JWT must still
    map to the same CSP user owner.
    """
    _mock_me_same_user()

    def csp_chat_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        if body["model"] == "agent-resume":
            return httpx.Response(200, json=_llm_router_response("ok"))
        return httpx.Response(
            200,
            json=_llm_router_response("DISPATCH:agent-resume:hi"),
        )

    respx.post(CSP_URL).mock(side_effect=csp_chat_handler)
    respx.get(CSP_AGENTS_URL).mock(
        return_value=httpx.Response(
            200, json=_agent_registry_response("agent-resume")
        )
    )

    csp_resume_url = (
        f"{CSP_BASE}/v1/agents/agent-resume/sessions/s-resume-jwt/answer"
    )
    resume_route = respx.post(csp_resume_url).mock(
        return_value=httpx.Response(
            200,
            content=b"event: anila.resumed\ndata: {}\n\n",
            headers={"Content-Type": "text/event-stream"},
        )
    )

    app = create_router_app(session_db_path=str(db_path))
    client = TestClient(app)

    pinned = client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
            "session_id": "s-resume-jwt",
        },
        headers={"Authorization": f"Bearer {_jwt('access-v1')}"},
    )
    assert pinned.status_code == 200, pinned.text

    resume_resp = client.post(
        "/v1/sessions/s-resume-jwt/answer",
        json={"interrupt_id": "i-7", "answer": "go ahead"},
        headers={"Authorization": f"Bearer {_jwt('access-v2')}"},
    )

    assert resume_resp.status_code == 200, resume_resp.text
    assert resume_route.call_count == 1


def _sse_frames(body: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for block in body.split("\n\n"):
        name = ""
        data = ""
        for line in block.splitlines():
            if line.startswith("event:"):
                name = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data = line.split(":", 1)[1].strip()
        if data:
            out.append((name, data))
    return out


@pytest.mark.parametrize("mode", ["http", "connect"])
@respx.mock
def test_resume_upstream_failure_is_safe_terminal_error(
    db_path: Path, mode: str
) -> None:
    """Resume HTTP bodies and connection errors stay in the operator log.

    The caller sees a fixed ``anila.error`` and a trace that repeats that
    sentence. The turn does not end with ``finish=stop`` or ``[DONE]``.
    """
    secret = "sk-live-RESUME-DO-NOT-LEAK"

    def csp_chat_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        if body.get("model") == "agent-resume":
            return httpx.Response(200, json=_llm_router_response("ok"))
        return httpx.Response(
            200, json=_llm_router_response("DISPATCH:agent-resume:hi")
        )

    respx.post(CSP_URL).mock(side_effect=csp_chat_handler)
    agents = _agent_registry_response("agent-resume")
    agents["data"][0]["requires_encryption"] = True
    respx.get(CSP_AGENTS_URL).mock(return_value=httpx.Response(200, json=agents))
    csp_resume_url = (
        f"{CSP_BASE}/v1/agents/agent-resume/sessions/s-resume-fail/answer"
    )

    def resume_handler(request: httpx.Request) -> httpx.Response:
        if mode == "connect":
            raise httpx.ConnectError(
                f"dial tcp {secret} at http://10.9.9.9/internal"
            )
        return httpx.Response(
            502,
            content=f"traceback {secret} at http://10.9.9.9/internal".encode(),
        )

    respx.post(csp_resume_url).mock(side_effect=resume_handler)

    app = create_router_app(session_db_path=str(db_path))
    client = TestClient(app)
    pinned = client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
            "session_id": "s-resume-fail",
        },
        headers={"Authorization": "Bearer sk-test"},
    )
    assert pinned.status_code == 200, pinned.text

    resume_resp = client.post(
        "/v1/sessions/s-resume-fail/answer",
        json={"interrupt_id": "i-fail", "answer": "go ahead"},
        headers={"Authorization": "Bearer sk-test"},
    )
    assert resume_resp.status_code == 200, resume_resp.text
    text = resume_resp.text
    assert secret not in text
    assert "10.9.9.9" not in text
    assert "traceback" not in text
    assert "dial tcp" not in text
    assert "data: [DONE]" not in text
    assert '"finish_reason": "stop"' not in text

    frames = _sse_frames(text)
    safe = "agent「agent-resume」暫時無法使用，請稍後再試。"
    traces = [json.loads(data) for name, data in frames if name == "anila.trace"]
    assert traces
    assert all(step.get("detail") == safe for step in traces)
    assert secret not in json.dumps(traces)
    errors = [json.loads(data) for name, data in frames if name == "anila.error"]
    assert errors == [{"message": safe}]
    metas = [
        (i, json.loads(data))
        for i, (name, data) in enumerate(frames)
        if name == "anila.meta"
    ]
    assert len(metas) == 1
    meta_i, meta = metas[0]
    assert meta == {"classified": True}
    error_i = next(i for i, (name, _) in enumerate(frames) if name == "anila.error")
    assert meta_i < error_i
    assert frames[-1] == ("anila.error", json.dumps({"message": safe}, ensure_ascii=False))


@pytest.mark.parametrize("frame", ["error", "anila.error"])
@respx.mock
def test_resume_http_200_upstream_error_frame_is_redacted(
    db_path: Path, frame: str
) -> None:
    """CSP resume stays HTTP 200 when the agent fails. The body is
    ``event: error`` (agent 500) or a raw ``anila.error``. Neither the
    secret nor a following success trailer may reach the caller."""
    secret = "sk-live-INLINE-DO-NOT-LEAK"

    def csp_chat_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        if body.get("model") == "agent-resume":
            return httpx.Response(200, json=_llm_router_response("ok"))
        return httpx.Response(
            200, json=_llm_router_response("DISPATCH:agent-resume:hi")
        )

    respx.post(CSP_URL).mock(side_effect=csp_chat_handler)
    agents = _agent_registry_response("agent-resume")
    agents["data"][0]["requires_encryption"] = True
    respx.get(CSP_AGENTS_URL).mock(return_value=httpx.Response(200, json=agents))
    session_id = "s-resume-inline"
    csp_resume_url = (
        f"{CSP_BASE}/v1/agents/agent-resume/sessions/{session_id}/answer"
    )
    if frame == "error":
        bad = (
            "event: error\ndata: "
            + json.dumps(
                {"status": 500, "detail": f"traceback {secret} at http://10.8.8.8/x"},
                ensure_ascii=False,
            )
            + "\n\n"
        )
    else:
        bad = (
            "event: anila.error\ndata: "
            + json.dumps({"message": f"{secret} at http://10.8.8.8/x"}, ensure_ascii=False)
            + "\n\n"
        )
    sse_body = (
        'data: {"choices":[{"delta":{"content":"半截"}}]}\n\n'
        + bad
        + 'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
        + "data: [DONE]\n\n"
    )
    respx.post(csp_resume_url).mock(
        return_value=httpx.Response(
            200,
            content=sse_body.encode("utf-8"),
            headers={"Content-Type": "text/event-stream"},
        )
    )

    app = create_router_app(session_db_path=str(db_path))
    client = TestClient(app)
    pinned = client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
            "session_id": session_id,
        },
        headers={"Authorization": "Bearer sk-test"},
    )
    assert pinned.status_code == 200, pinned.text
    resume_resp = client.post(
        f"/v1/sessions/{session_id}/answer",
        json={"interrupt_id": "i-inline", "answer": "go ahead"},
        headers={"Authorization": "Bearer sk-test"},
    )
    assert resume_resp.status_code == 200, resume_resp.text
    text = resume_resp.text
    assert "半截" in text
    assert secret not in text
    assert "10.8.8.8" not in text
    assert "traceback" not in text
    assert "data: [DONE]" not in text
    assert '"finish_reason": "stop"' not in text

    frames = _sse_frames(text)
    safe = "agent「agent-resume」暫時無法使用，請稍後再試。"
    metas = [
        (i, json.loads(data))
        for i, (name, data) in enumerate(frames)
        if name == "anila.meta"
    ]
    assert len(metas) == 1
    meta_i, meta = metas[0]
    assert meta["classified"] is True
    assert "citations" not in meta
    error_i = next(i for i, (name, _) in enumerate(frames) if name == "anila.error")
    assert meta_i < error_i
    assert frames[-1][0] == "anila.error"
    assert json.loads(frames[-1][1]) == {"message": safe}


class _MetaThenReadError(httpx.AsyncByteStream):
    """Deliver one SSE payload, then fail the next read."""

    def __init__(self, payload: bytes, message: str) -> None:
        self._payload = payload
        self._message = message
        self._sent = False

    async def __aiter__(self):
        if not self._sent:
            self._sent = True
            yield self._payload
        raise httpx.ReadError(self._message)

    async def aclose(self) -> None:
        return None


@respx.mock
def test_resume_read_error_keeps_meta_classified_and_usage(db_path: Path) -> None:
    """Meta already on the wire must survive a later ``RequestError``.

    The failure meta used to look only at the manifest, so a classified
    flag and usage received before the drop were lost.
    """
    secret = "http://10.7.7.7/dropped"
    usage = {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}

    def csp_chat_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        if body.get("model") == "agent-resume":
            return httpx.Response(200, json=_llm_router_response("ok"))
        return httpx.Response(
            200, json=_llm_router_response("DISPATCH:agent-resume:hi")
        )

    respx.post(CSP_URL).mock(side_effect=csp_chat_handler)
    agents = _agent_registry_response("agent-resume")
    agents["data"][0]["requires_encryption"] = False
    respx.get(CSP_AGENTS_URL).mock(return_value=httpx.Response(200, json=agents))
    session_id = "s-resume-drop"
    meta = {
        "classified": True,
        "usage": usage,
        "citations": ["do-not-copy"],
    }
    payload = (
        "event: anila.meta\ndata: "
        + json.dumps(meta, ensure_ascii=False)
        + "\n\n"
    ).encode()
    respx.post(
        f"{CSP_BASE}/v1/agents/agent-resume/sessions/{session_id}/answer"
    ).mock(
        return_value=httpx.Response(
            200,
            stream=_MetaThenReadError(payload, f"reset at {secret}"),
            headers={"Content-Type": "text/event-stream"},
        )
    )

    app = create_router_app(session_db_path=str(db_path))
    client = TestClient(app)
    pinned = client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
            "session_id": session_id,
        },
        headers={"Authorization": "Bearer sk-test"},
    )
    assert pinned.status_code == 200, pinned.text
    resume_resp = client.post(
        f"/v1/sessions/{session_id}/answer",
        json={"interrupt_id": "i-drop", "answer": "go"},
        headers={"Authorization": "Bearer sk-test"},
    )
    assert resume_resp.status_code == 200, resume_resp.text
    text = resume_resp.text
    assert secret not in text
    assert "10.7.7.7" not in text
    assert "data: [DONE]" not in text
    frames = _sse_frames(text)
    metas = [json.loads(data) for name, data in frames if name == "anila.meta"]
    assert metas
    failure = metas[-1]
    assert failure["classified"] is True
    assert failure["usage"] == usage
    assert "citations" not in failure
    assert frames[-1][0] == "anila.error"
    safe = "agent「agent-resume」暫時無法使用，請稍後再試。"
    assert json.loads(frames[-1][1]) == {"message": safe}


@respx.mock
def test_resume_success_forwards_finish_reason_and_usage(db_path: Path) -> None:
    """A finished resume chunk keeps ``finish_reason`` and ``usage``.

    Rebuilding it as text-only dropped ``length`` (so the Shell never
    calls ``onFinishReason``) and left usage off the wire.
    """
    usage = {"prompt_tokens": 8, "completion_tokens": 9, "total_tokens": 17}

    def csp_chat_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        if body.get("model") == "agent-resume":
            return httpx.Response(200, json=_llm_router_response("ok"))
        return httpx.Response(
            200, json=_llm_router_response("DISPATCH:agent-resume:hi")
        )

    respx.post(CSP_URL).mock(side_effect=csp_chat_handler)
    respx.get(CSP_AGENTS_URL).mock(
        return_value=httpx.Response(
            200, json=_agent_registry_response("agent-resume")
        )
    )
    session_id = "s-resume-length"
    sse_body = (
        "data: "
        + json.dumps(
            {
                "choices": [
                    {"index": 0, "delta": {"content": "續答"}, "finish_reason": None}
                ],
                "usage": usage,
            },
            ensure_ascii=False,
        )
        + "\n\n"
        + "data: "
        + json.dumps(
            {
                "choices": [
                    {"index": 0, "delta": {}, "finish_reason": "length"}
                ]
            }
        )
        + "\n\n"
        + "data: [DONE]\n\n"
    )
    respx.post(
        f"{CSP_BASE}/v1/agents/agent-resume/sessions/{session_id}/answer"
    ).mock(
        return_value=httpx.Response(
            200,
            content=sse_body.encode("utf-8"),
            headers={"Content-Type": "text/event-stream"},
        )
    )
    app = create_router_app(session_db_path=str(db_path))
    client = TestClient(app)
    pinned = client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
            "session_id": session_id,
        },
        headers={"Authorization": "Bearer sk-test"},
    )
    assert pinned.status_code == 200, pinned.text
    resume_resp = client.post(
        f"/v1/sessions/{session_id}/answer",
        json={"interrupt_id": "i-len", "answer": "go"},
        headers={"Authorization": "Bearer sk-test"},
    )
    assert resume_resp.status_code == 200, resume_resp.text
    text = resume_resp.text
    assert "續答" in text
    assert "data: [DONE]" in text
    assert "event: anila.error" not in text
    frames = _sse_frames(text)
    finishes = []
    saw_usage = False
    for name, data in frames:
        if name:
            continue
        if data == "[DONE]":
            continue
        chunk = json.loads(data)
        if chunk.get("usage") == usage:
            saw_usage = True
        choice = (chunk.get("choices") or [{}])[0]
        reason = choice.get("finish_reason")
        if reason:
            finishes.append(reason)
    assert finishes == ["length"]
    assert saw_usage
