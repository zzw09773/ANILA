"""One caller's broken token must not contaminate another caller's trace.

OPT-4 runs identity resolution and the agent-registry refresh in
parallel. Before it, a malformed/revoked JWT failed at
``_resolve_session_owner_hash`` and the registry refresh was never
reached; with the gather, the rejected credential *does* hit
``GET /v1/agents``, fails, and used to write a single process-wide
``last_refresh_error``. Every other caller's next turn then rendered
"registry refresh 失敗：…" — including CSP's rejection text for a
different user — into their own operator-visible trace. On a classified
platform that is a privacy defect, not a cosmetic one.

Mutation notes are on each test: the exact one-line production edit that
turns it red.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
import respx
from fastapi.testclient import TestClient

from anila_core.api.router_server import create_router_app
from anila_core.config import settings
from anila_core.memory import close_all_connections
from anila_core.registry.remote_agent_manifest import RemoteAgentRegistry

CSP_BASE = settings.csp_base_url
CSP_URL = f"{CSP_BASE}/v1/chat/completions"
CSP_AGENTS_URL = f"{CSP_BASE}/v1/agents"
CSP_ME_URL = f"{CSP_BASE}/api/auth/me"

GOOD_KEY = "sk-good-caller"
BAD_JWT = "jwt-header.revoked-caller.jwt-signature"
# Distinctive so we can assert it never reaches the innocent caller.
CSP_REJECTION = "csp-rejected-the-revoked-caller"


@pytest_asyncio.fixture
async def db_path(tmp_path: Path):
    db = tmp_path / "router-registry-isolation.db"
    yield db
    await close_all_connections()


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


def _ask(client: TestClient, credential: str) -> httpx.Response:
    return client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hi"}], "stream": False},
        headers={"Authorization": f"Bearer {credential}"},
    )


def _registry_steps(response: httpx.Response) -> list[dict]:
    trace = response.json()["anila_meta"]["trace"]
    return [step for step in trace if step.get("kind") == "registry"]


# ---------------------------------------------------------------------------
# End-to-end: two callers through one Router app
# ---------------------------------------------------------------------------


@respx.mock
def test_bad_jwt_does_not_poison_another_callers_trace(db_path: Path) -> None:
    """Peak-load interleaving: good turn, rejected turn, good turn.

    Step 1 warms the good caller's registry slot (TTL 60 s) so step 3
    performs no refresh of its own — exactly the production shape, and
    the only shape in which contaminated *shared* state is observable.

    Mutant (revert): in ``router_server.chat_completions`` change
    ``registry_error = registry.refresh_error_for(caller_api_key)`` back
    to ``registry_error = registry.last_refresh_error`` — step 3's trace
    reports the revoked caller's 401 and this test fails.
    """

    def _agents(request: httpx.Request) -> httpx.Response:
        if request.headers.get("Authorization") == f"Bearer {GOOD_KEY}":
            return httpx.Response(200, json={"data": []})
        return httpx.Response(401, json={"detail": CSP_REJECTION})

    respx.get(CSP_AGENTS_URL).mock(side_effect=_agents)
    respx.get(CSP_ME_URL).mock(
        return_value=httpx.Response(401, json={"detail": CSP_REJECTION})
    )
    respx.post(CSP_URL).mock(
        return_value=httpx.Response(200, json=_llm_reply("好的"))
    )

    app = create_router_app(session_db_path=str(db_path))
    client = TestClient(app)

    # 1. Innocent caller succeeds; their registry slot is now fresh.
    first = _ask(client, GOOD_KEY)
    assert first.status_code == 200
    assert [s["status"] for s in _registry_steps(first)] == ["ok"]

    # 2. A revoked/malformed JWT arrives. CSP rejects it on both hops.
    bad = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hi"}], "stream": False},
        headers={"Authorization": f"Bearer {BAD_JWT}"},
    )
    assert bad.status_code == 401

    # 3. The innocent caller's very next turn must be untouched.
    second = _ask(client, GOOD_KEY)
    assert second.status_code == 200

    steps = _registry_steps(second)
    assert steps, "registry trace step disappeared"
    assert [s["status"] for s in steps] == ["ok"], steps
    rendered = json.dumps(second.json(), ensure_ascii=False)
    assert "registry refresh 失敗" not in rendered, steps
    assert CSP_REJECTION not in rendered, (
        "another caller's CSP rejection text leaked into this user's trace"
    )


@respx.mock
def test_rejected_caller_does_not_leave_the_refresh_running_after_the_401(
    db_path: Path,
) -> None:
    """A 401'd turn must finish its own side effects before it returns.

    Bare ``asyncio.gather`` raises as soon as identity resolution fails
    and leaves the registry refresh in flight, so the rejected caller's
    failure lands at an unpredictable point — possibly inside the next
    user's turn.

    Mutant (revert): in ``router_server.chat_completions`` drop
    ``return_exceptions=True`` from the OPT-4 gather (and the two
    ``isinstance(..., BaseException)`` re-raises, restoring
    ``owner_key_hash, _ = await asyncio.gather(...)``) — the response
    comes back long before ``SLOW_S`` and this test fails.
    """
    SLOW_S = 0.5

    async def _slow_agents(request: httpx.Request) -> httpx.Response:
        import asyncio

        await asyncio.sleep(SLOW_S)
        return httpx.Response(401, json={"detail": CSP_REJECTION})

    respx.get(CSP_AGENTS_URL).mock(side_effect=_slow_agents)
    respx.get(CSP_ME_URL).mock(
        return_value=httpx.Response(401, json={"detail": CSP_REJECTION})
    )

    app = create_router_app(session_db_path=str(db_path))
    client = TestClient(app)

    started = time.monotonic()
    bad = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hi"}], "stream": False},
        headers={"Authorization": f"Bearer {BAD_JWT}"},
    )
    elapsed = time.monotonic() - started

    assert bad.status_code == 401
    assert elapsed >= SLOW_S, (
        f"handler returned after {elapsed:.3f}s (< {SLOW_S}s): the registry "
        "refresh was still running orphaned after the caller was rejected"
    )


# ---------------------------------------------------------------------------
# Registry unit level
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_refresh_error_is_scoped_to_the_caller_that_failed() -> None:
    """``refresh_error_for`` answers per caller, not process-wide.

    Mutant (revert): make
    ``RemoteAgentRegistry.refresh_error_for`` ``return
    self._last_refresh_error`` — the good caller inherits the bad
    caller's error and this fails.
    """

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("Authorization") == f"Bearer {GOOD_KEY}":
            return httpx.Response(200, json={"data": []})
        return httpx.Response(401, json={"detail": CSP_REJECTION})

    respx.get(CSP_AGENTS_URL).mock(side_effect=_handler)
    reg = RemoteAgentRegistry(csp_base_url=CSP_BASE, ttl=60.0, max_entries=8)

    await reg.refresh(GOOD_KEY)
    await reg.refresh(BAD_JWT)

    assert reg.refresh_error_for(BAD_JWT) is not None
    assert reg.refresh_error_for(GOOD_KEY) is None
    # /health keeps the process-wide operator view — that part is unchanged.
    assert reg.last_refresh_error is not None


@pytest.mark.asyncio
@respx.mock
async def test_per_caller_errors_are_bounded() -> None:
    """A flood of distinct failing tokens must not grow memory.

    Failed refreshes never reach ``_store``, so this map needs its own
    LRU or it re-opens the leak the registry fix just closed.

    Mutant (revert): delete the ``while len(self._refresh_error_by_key) >
    self._max_entries`` loop in ``RemoteAgentRegistry._record_error`` —
    the map grows to ``n_tokens`` and this fails.
    """
    max_entries = 8
    n_tokens = 300
    respx.get(CSP_AGENTS_URL).mock(
        return_value=httpx.Response(401, json={"detail": CSP_REJECTION})
    )
    reg = RemoteAgentRegistry(csp_base_url=CSP_BASE, ttl=60.0, max_entries=max_entries)

    for i in range(n_tokens):
        await reg.refresh(f"jwt-header.bad{i}.sig{i}")

    assert len(reg._refresh_error_by_key) <= max_entries
    assert len(reg._refresh_error_by_key) < n_tokens


@pytest.mark.asyncio
@respx.mock
async def test_successful_refresh_clears_only_that_callers_error() -> None:
    """Recovery is per caller too.

    Mutant (revert): drop ``self._record_error(cache_key, None)`` from
    the success path of ``RemoteAgentRegistry._do_refresh`` — the good
    caller stays permanently marked as failed and this fails.
    """
    state = {"fail": True}

    def _handler(request: httpx.Request) -> httpx.Response:
        if state["fail"]:
            return httpx.Response(503, json={"detail": "csp down"})
        return httpx.Response(200, json={"data": []})

    respx.get(CSP_AGENTS_URL).mock(side_effect=_handler)
    reg = RemoteAgentRegistry(csp_base_url=CSP_BASE, ttl=60.0, max_entries=8)

    await reg.refresh(GOOD_KEY)
    assert reg.refresh_error_for(GOOD_KEY) is not None

    state["fail"] = False
    await reg.refresh(GOOD_KEY)
    assert reg.refresh_error_for(GOOD_KEY) is None
