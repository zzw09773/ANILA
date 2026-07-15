"""Focused transport-contract tests for the CSP-owned Agent sink."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest
from anila_contracts import Classification, StepEvent
from anila_contracts.events import StepKind, StepStatus
from fastapi import HTTPException
from starlette.requests import Request

from app.api.agent_dispatch import _grant_header
import app.api.proxy as proxy_api
import app.services.agent_dispatch_service as dispatch_service
from app.services.agent_dispatch_service import (
    DispatchAuthority,
    DispatchBinding,
    _normalize_agent_block,
    dispatch_nonstream,
    dispatch_resume,
    dispatch_stream,
)
from app.services.proxy.stream_bridge import (
    BridgeContext,
    InMemorySessionEventStore,
    StreamBridge,
)


def _request(*headers: tuple[str, str]) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/internal/v1/agents/dispatch",
            "headers": [(name.lower().encode(), value.encode()) for name, value in headers],
        }
    )


def test_dispatch_accepts_only_canonical_signed_grant_header() -> None:
    assert _grant_header(_request(("X-ANILA-Execution-Grant", "signed-token"))) == "signed-token"


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param((), id="missing-canonical"),
        (("X-ANILA-Execution-Grant-Token", "signed-token"),),
        (
            ("X-ANILA-Execution-Grant", "canonical-token"),
            ("X-ANILA-Execution-Grant-Token", "alternate-token"),
        ),
        (
            ("X-ANILA-Execution-Grant", "first-token"),
            ("X-ANILA-Execution-Grant", "second-token"),
        ),
    ],
)
def test_dispatch_rejects_missing_or_alternate_grant_header(
    headers: tuple[tuple[str, str], ...],
) -> None:
    with pytest.raises(HTTPException) as caught:
        _grant_header(_request(*headers))
    assert caught.value.status_code == 400


def test_invocation_guards_share_one_lock_and_cleanup_refcount() -> None:
    invocation_id = "guard-contract-unique"

    async def scenario() -> None:
        lock_refs: list[asyncio.Lock] = []
        owner_entered = asyncio.Event()
        release_owner = asyncio.Event()

        async def worker() -> None:
            async with dispatch_service._invocation_guard(invocation_id) as lock:
                lock_refs.append(lock)
                if len(lock_refs) == 1:
                    owner_entered.set()
                    await release_owner.wait()
                else:
                    await asyncio.sleep(0)

        tasks = [asyncio.create_task(worker()) for _ in range(3)]
        await owner_entered.wait()
        users = 0
        for _ in range(20):
            async with dispatch_service._INVOCATION_LOCKS_GUARD:
                entry = dispatch_service._INVOCATION_LOCKS.get(invocation_id)
                users = 0 if entry is None else entry.users
            if users == 3:
                break
            await asyncio.sleep(0)
        assert users == 3
        release_owner.set()
        await asyncio.gather(*tasks)
        async with dispatch_service._INVOCATION_LOCKS_GUARD:
            assert invocation_id not in dispatch_service._INVOCATION_LOCKS
        assert len(lock_refs) == 3
        assert len({id(lock) for lock in lock_refs}) == 1

    asyncio.run(scenario())


def _bridge(*, max_events_per_run: int = 500) -> StreamBridge:
    return StreamBridge(
        BridgeContext(
            task_id="1",
            trace_id="trace-1",
            agent_id="research-agent",
            session_id="session-1",
            invocation_id="invocation-1",
            run_id="1",
            classification=Classification.UNCLASSIFIED,
        ),
        store=InMemorySessionEventStore(),
        max_events_per_run=max_events_per_run,
    )


def _authority(*, invocation_id: str = "invocation-test", run_status: str | None = None) -> DispatchAuthority:
    return DispatchAuthority(
        caller=object(),
        user=object(),
        agent=SimpleNamespace(name="research-agent", endpoint_url="http://agent.test"),
        grant=object(),
        binding=DispatchBinding(
            caller_user_id=1,
            owner_id=1,
            task_id=1,
            run_id=1,
            source_snapshot_id=1,
            trace_id="trace-1",
            invocation_id=invocation_id,
            session_id="session-1",
            agent_id="research-agent",
            registry_snapshot_id="registry-1",
            registry_snapshot_revision="registry-1",
            registry_snapshot_hash="registry-1",
            manifest_revision="sha256:manifest-1",
            manifest_sha256="manifest-1",
            grant_id="grant-1",
            route_decision_id="route-1",
            policy_decision_id="policy-1",
            classification=Classification.UNCLASSIFIED,
        ),
        endpoint_url="http://agent.test/v1/chat/completions",
        run_status=run_status,
    )


def _blocked_event(*, event_id: str = "blocked-1") -> dict[str, object]:
    return StepEvent(
        event_id=event_id,
        sequence=1,
        cursor="forged-cursor",
        trace_id="forged-trace",
        task_id="999",
        session_id="forged-session",
        invocation_id="forged-invocation",
        run_id="999",
        step_id="agent:research-agent",
        kind=StepKind.AGENT,
        status=StepStatus.BLOCKED,
        safe_output_summary="等待 CSP 核准",
        classification=Classification.UNCLASSIFIED,
    ).model_dump(mode="json")


def test_agent_openai_content_is_rebuilt_only_after_bridge_validation() -> None:
    bridge = _bridge()
    frames, _ = _normalize_agent_block(
        bridge,
        block='data: {"choices":[{"delta":{"content":"safe answer"}}]}',
        model="research-agent",
        source_sequence=1,
    )
    assert len(frames) == 2
    assert frames[0].startswith("event: anila.step\n")
    assert '"content":"safe answer"' in frames[1]


@pytest.mark.parametrize(
    "block",
    [
        pytest.param(
            'data: {"choices":[{"delta":{"content":"api_key=sk-abcdefghijklmnopqrst"}}]}',
            id="secret-content",
        ),
        pytest.param(
            'event: anila.meta\ndata: {"safe":"no", "secret":"sk-abcdefghijklmnopqrst"}',
            id="fake-meta",
        ),
        pytest.param(
            'event: attacker\ndata: {"choices":[{"delta":{"content":"spoof"}}]}',
            id="unknown-event",
        ),
        pytest.param("data: " + ("x" * 40_000), id="oversize"),
    ],
)
def test_untrusted_agent_frames_never_reach_client(block: str) -> None:
    bridge = _bridge()
    frames, _ = _normalize_agent_block(
        bridge,
        block=block,
        model="research-agent",
        source_sequence=1,
    )
    assert frames == []
    assert bridge.replay_events() == ()


def test_stream_bridge_budget_drops_flooded_agent_content() -> None:
    bridge = _bridge(max_events_per_run=1)
    first, next_sequence = _normalize_agent_block(
        bridge,
        block='data: {"choices":[{"delta":{"content":"one"}}]}',
        model="research-agent",
        source_sequence=1,
    )
    second, _ = _normalize_agent_block(
        bridge,
        block='data: {"choices":[{"delta":{"content":"two"}}]}',
        model="research-agent",
        source_sequence=next_sequence,
    )
    assert first and second == []
    assert len(bridge.replay_events()) == 1


def test_nonstream_rebuilds_minimal_csp_response_and_drops_agent_extras(monkeypatch) -> None:
    payload = {
        "id": "agent-secret-id",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "safe answer", "secret": "drop"},
                "finish_reason": "length",
            }
        ],
        "usage": {"prompt_tokens": 99},
        "anila_meta": {"forged": True},
    }
    bridge = _bridge()
    authority = _authority(invocation_id="nonstream-extra")
    calls = 0

    class Response:
        headers = {"content-type": "application/json"}
        text = json.dumps(payload)

        def raise_for_status(self) -> None:
            return None

        def json(self) -> object:
            return payload

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, *_args, **_kwargs):
            nonlocal calls
            calls += 1
            return Response()

    monkeypatch.setattr(dispatch_service, "_bridge", lambda _authority, _db: bridge)
    monkeypatch.setattr(
        dispatch_service,
        "build_agent_outbound_headers",
        lambda _db, _authority, grant_token: {"X-CSP-Service-Token": "csk-test"},
    )
    monkeypatch.setattr(dispatch_service.httpx, "AsyncClient", lambda **_kwargs: Client())
    result = asyncio.run(
        dispatch_nonstream(
            db=SimpleNamespace(rollback=lambda: None),
            authority=authority,
            messages=[{"role": "user", "content": "hello"}],
            grant_token="signed-grant",
        )
    )
    assert calls == 1
    assert set(result) == {"id", "object", "model", "choices", "anila_meta"}
    assert result["object"] == "chat.completion"
    assert result["choices"][0]["message"] == {"role": "assistant", "content": "safe answer"}
    assert "usage" not in result
    assert result["anila_meta"]["status"] == "completed"


def test_nonstream_blocked_202_is_durable_pause_without_synthetic_completion(monkeypatch) -> None:
    bridge = _bridge()
    authority = _authority(invocation_id="nonstream-blocked")
    blocked = _blocked_event()
    calls = 0

    class Response:
        status_code = 202

        def json(self) -> object:
            return {"status": "paused", "anila_events": [blocked]}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, *_args, **_kwargs):
            nonlocal calls
            calls += 1
            return Response()

    monkeypatch.setattr(dispatch_service, "_bridge", lambda _authority, _db: bridge)
    monkeypatch.setattr(
        dispatch_service,
        "build_agent_outbound_headers",
        lambda _db, _authority, grant_token: {"X-CSP-Service-Token": "csk-test"},
    )
    monkeypatch.setattr(dispatch_service.httpx, "AsyncClient", lambda **_kwargs: Client())
    result = asyncio.run(
        dispatch_nonstream(
            db=SimpleNamespace(rollback=lambda: None),
            authority=authority,
            messages=[{"role": "user", "content": "hello"}],
            grant_token="signed-grant",
        )
    )
    assert calls == 1
    assert result["status"] == "paused"
    assert bridge.terminal_event() is None
    events = bridge.replay_events()
    assert events[-1].status is StepStatus.BLOCKED
    # Agent-provided identity/cursor was rebound to the CSP authority.
    assert events[-1].task_id == "1"
    assert events[-1].trace_id == "trace-1"


def test_stream_blocked_event_does_not_append_completed(monkeypatch) -> None:
    bridge = _bridge()
    authority = _authority(invocation_id="stream-blocked")
    blocked = json.dumps(_blocked_event(), ensure_ascii=False, separators=(",", ":"))

    class Response:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def aiter_lines(self):
            yield "event: anila.step"
            yield f"data: {blocked}"
            yield ""
            yield "data: [DONE]"
            yield ""

        async def aread(self):
            return b""

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def stream(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr(dispatch_service, "_bridge", lambda _authority, _db: bridge)
    monkeypatch.setattr(
        dispatch_service,
        "build_agent_outbound_headers",
        lambda _db, _authority, grant_token: {},
    )
    monkeypatch.setattr(dispatch_service.httpx, "AsyncClient", lambda **_kwargs: Client())
    frames = asyncio.run(
        _collect_stream(
            dispatch_stream(
                db=SimpleNamespace(rollback=lambda: None),
                authority=authority,
                messages=[{"role": "user", "content": "hello"}],
                grant_token="signed-grant",
            )
        )
    )
    assert frames[-1] == "data: [DONE]\n\n"
    assert bridge.terminal_event() is None
    assert bridge.replay_events()[-1].status is StepStatus.BLOCKED


async def _collect_stream(stream: AsyncIterator[str]) -> list[str]:
    return [frame async for frame in stream]


def test_resume_rebinds_agent_history_and_replays_terminal_once(monkeypatch) -> None:
    bridge = _bridge()
    blocked = _blocked_event()
    bridge.append("anila.step", json.dumps(blocked, ensure_ascii=False))
    authority = _authority(invocation_id="resume-success")
    completed = dict(blocked)
    completed.update(
        {
            "event_id": "completed-1",
            "sequence": 2,
            "status": StepStatus.COMPLETED.value,
            "safe_output_summary": "resumed answer",
        }
    )
    calls = 0

    class Response:
        status_code = 200

        def json(self) -> object:
            return {
                "choices": [{"message": {"content": "resumed answer"}}],
                "anila_events": [completed],
            }

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, url, **_kwargs):
            nonlocal calls
            calls += 1
            assert url.endswith("/v1/tasks/1/approve")
            return Response()

    monkeypatch.setattr(dispatch_service, "_bridge", lambda _authority, _db: bridge)
    monkeypatch.setattr(
        dispatch_service,
        "build_agent_outbound_headers",
        lambda _db, _authority, *, grant_token, idempotency_key=None: {
            "X-CSP-Service-Token": "csk-test",
            "X-ANILA-Idempotency-Key": idempotency_key or "",
        },
    )
    monkeypatch.setattr(dispatch_service.httpx, "AsyncClient", lambda **_kwargs: Client())
    db = SimpleNamespace(rollback=lambda: None)
    first = asyncio.run(
        dispatch_resume(
            db=db,
            authority=authority,
            grant_token="signed-grant",
            idempotency_key="resume-key-1",
        )
    )
    second = asyncio.run(
        dispatch_resume(
            db=db,
            authority=authority,
            grant_token="signed-grant",
            idempotency_key="resume-key-1",
        )
    )
    assert calls == 1
    assert first["choices"][0]["message"]["content"] == "resumed answer"
    assert second["choices"][0]["message"]["content"] == first["choices"][0]["message"]["content"]
    assert second["anila_meta"]["status"] == "completed"
    assert bridge.terminal_event() is not None
    assert bridge.terminal_event().status is StepStatus.COMPLETED


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(
            {"choices": [{"message": {"content": "api_key=sk-abcdefghijklmnopqrst"}}]},
            id="secret",
        ),
        pytest.param(
            {"choices": [{"message": {"content": "x" * 40_000}}]},
            id="oversize",
        ),
        pytest.param({"choices": [{"delta": {"content": "missing-message"}}]}, id="malformed"),
    ],
)
def test_nonstream_rejects_secret_oversize_and_malformed_agent_payload(monkeypatch, payload) -> None:
    bridge = _bridge()
    authority = _authority(invocation_id=f"nonstream-reject-{id(payload)}")

    class Response:
        headers = {"content-type": "application/json"}
        text = json.dumps(payload)

        def raise_for_status(self) -> None:
            return None

        def json(self) -> object:
            return payload

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr(dispatch_service, "_bridge", lambda _authority, _db: bridge)
    monkeypatch.setattr(
        dispatch_service,
        "build_agent_outbound_headers",
        lambda _db, _authority, grant_token: {"X-CSP-Service-Token": "csk-test"},
    )
    monkeypatch.setattr(dispatch_service.httpx, "AsyncClient", lambda **_kwargs: Client())
    with pytest.raises(HTTPException) as caught:
        asyncio.run(
            dispatch_nonstream(
                db=SimpleNamespace(rollback=lambda: None),
                authority=authority,
                messages=[{"role": "user", "content": "hello"}],
                grant_token="signed-grant",
            )
        )
    assert caught.value.status_code == 502
    terminal = bridge.terminal_event()
    assert terminal is not None
    assert terminal.status.value == "failed"


def test_completed_run_without_terminal_fails_before_agent_call(monkeypatch) -> None:
    bridge = _bridge()
    authority = _authority(invocation_id="completed-without-terminal", run_status="completed")
    calls = 0

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, *_args, **_kwargs):
            nonlocal calls
            calls += 1
            raise AssertionError("completed run must not reach Agent")

    monkeypatch.setattr(dispatch_service, "_bridge", lambda _authority, _db: bridge)
    monkeypatch.setattr(dispatch_service.httpx, "AsyncClient", lambda **_kwargs: Client())
    with pytest.raises(HTTPException) as caught:
        asyncio.run(
            dispatch_nonstream(
                db=SimpleNamespace(rollback=lambda: None),
                authority=authority,
                messages=[{"role": "user", "content": "hello"}],
                grant_token="signed-grant",
            )
        )
    assert caught.value.status_code == 409
    assert calls == 0


@pytest.mark.parametrize("stream", [False, True])
def test_formal_public_agent_chat_fails_before_any_downstream_branch(
    monkeypatch, stream: bool
) -> None:
    class RequestStub:
        headers = {}
        state = SimpleNamespace()

        async def json(self):
            return {
                "model": "legacy-agent",
                "stream": stream,
                "messages": [{"role": "user", "content": "hi"}],
            }

    fake_agent = SimpleNamespace(name="legacy-agent")
    monkeypatch.setattr(proxy_api.settings, "GATE5_MODEL_GOVERNANCE_ENABLED", True)
    monkeypatch.setattr(proxy_api.settings, "ANILA_DEPLOYMENT_PROFILE", "development")
    monkeypatch.setattr(proxy_api, "_resolve_agent", lambda _db, _caller, _name: fake_agent)
    monkeypatch.setattr(
        proxy_api,
        "_resolve_model",
        lambda *_args, **_kwargs: pytest.fail("formal direct Agent must stop before model resolution"),
    )
    caller = SimpleNamespace(user=SimpleNamespace())
    with pytest.raises(HTTPException) as caught:
        asyncio.run(
            proxy_api._chat_completions_impl(
                RequestStub(), caller=caller, db=SimpleNamespace(), internal_router=False
            )
        )
    assert caught.value.status_code == 409
    assert "Router signed ExecutionGrant" in str(caught.value.detail)


def test_formal_agent_resume_fails_before_body_or_agent_lookup(monkeypatch) -> None:
    class RequestStub:
        headers = {}

        async def json(self):
            raise AssertionError("formal resume must stop before reading the body")

    monkeypatch.setattr(proxy_api.settings, "GATE5_MODEL_GOVERNANCE_ENABLED", True)
    monkeypatch.setattr(proxy_api.settings, "ANILA_DEPLOYMENT_PROFILE", "development")
    monkeypatch.setattr(proxy_api.settings, "ALLOW_LEGACY_AGENT_DISPATCH", True)
    monkeypatch.setattr(
        proxy_api,
        "_resolve_agent",
        lambda *_args, **_kwargs: pytest.fail("formal resume must stop before Agent lookup"),
    )
    with pytest.raises(HTTPException) as caught:
        asyncio.run(
            proxy_api.resume_agent_session(
                "legacy-agent", "session-1", RequestStub(), caller=None, db=None
            )
        )
    assert caught.value.status_code == 409
    assert "Router signed ExecutionGrant" in str(caught.value.detail)


def test_development_keeps_legacy_agent_guard_disabled(monkeypatch) -> None:
    monkeypatch.setattr(proxy_api.settings, "GATE5_MODEL_GOVERNANCE_ENABLED", False)
    monkeypatch.setattr(proxy_api.settings, "ANILA_DEPLOYMENT_PROFILE", "development")
    # The helper is intentionally a no-op outside formal Gate 5 posture;
    # existing development legacy dispatch remains governed by its own path.
    proxy_api._reject_legacy_agent_dispatch_in_formal()


def test_concurrent_stream_duplicate_waits_replays_and_calls_agent_once(monkeypatch) -> None:
    bridge = _bridge()
    authority = _authority(invocation_id="stream-duplicate")
    downstream_started = asyncio.Event()
    release_downstream = asyncio.Event()
    calls = 0

    class Response:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def aiter_lines(self):
            downstream_started.set()
            await release_downstream.wait()
            yield 'data: {"choices":[{"delta":{"content":"winner"}}]}'
            yield ""

        async def aread(self):
            return b""

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def stream(self, *_args, **_kwargs):
            nonlocal calls
            calls += 1
            return Response()

    monkeypatch.setattr(dispatch_service, "_bridge", lambda _authority, _db: bridge)
    monkeypatch.setattr(
        dispatch_service,
        "build_agent_outbound_headers",
        lambda _db, _authority, grant_token: {},
    )
    monkeypatch.setattr(dispatch_service.httpx, "AsyncClient", lambda **_kwargs: Client())

    async def collect() -> list[str]:
        return [frame async for frame in dispatch_stream(
            db=SimpleNamespace(rollback=lambda: None),
            authority=authority,
            messages=[{"role": "user", "content": "hello"}],
            grant_token="signed-grant",
        )]

    async def scenario() -> tuple[list[str], list[str]]:
        winner = asyncio.create_task(collect())
        await downstream_started.wait()
        duplicate = asyncio.create_task(collect())
        users = 0
        for _ in range(20):
            async with dispatch_service._INVOCATION_LOCKS_GUARD:
                entry = dispatch_service._INVOCATION_LOCKS.get("stream-duplicate")
                users = 0 if entry is None else entry.users
            if users == 2:
                break
            await asyncio.sleep(0)
        assert users == 2
        release_downstream.set()
        return await winner, await duplicate

    first, second = asyncio.run(scenario())
    assert calls == 1
    assert any('"content":"winner"' in frame for frame in first)
    assert any('"content":"winner"' in frame for frame in second)
    assert first[-1] == second[-1] == "data: [DONE]\n\n"
    async def assert_clean() -> None:
        async with dispatch_service._INVOCATION_LOCKS_GUARD:
            assert "stream-duplicate" not in dispatch_service._INVOCATION_LOCKS
    asyncio.run(assert_clean())
