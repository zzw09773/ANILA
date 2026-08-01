"""P2.1 W5: in-task outbound callbacks present the request-scoped dispatch JWT.

Acceptance:
  (a) outbound search carries Authorization: Bearer <this request's JWT>
  (b) two concurrent requests with different JWTs do not cross-contaminate
      — real await interleaving so module-global mutation goes RED
  (c) outbound with no dispatch token in scope fails loudly
  (F1) SSE mid-stream disconnect: no cross-context ValueError; scope clean
  (F2) non-CSP outbound origin does NOT receive the dispatch JWT
  (F3) service_wrapper pins api_key=None on in-task retriever + emitter
"""

from __future__ import annotations

import asyncio
import contextvars
from typing import ClassVar

import httpx
import pytest

from anila_agent.dispatch_token import (
    MissingDispatchTokenError,
    dispatch_bearer_scope,
    get_dispatch_bearer,
    resolve_outbound_bearer,
    same_csp_origin,
)
from anila_agent.retrieval.csp_http import CspHttpRetriever
from anila_agent.tracing import TraceEmitter

pytestmark = pytest.mark.unit


class _FakeResp:
    status_code = 200

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {"results": []}


class _RecordingClient:
    """Records every POST Authorization header (shared sink, per-test reset)."""

    calls: ClassVar[list[dict]] = []

    def __init__(self, **_kw) -> None:
        pass

    async def __aenter__(self) -> _RecordingClient:
        return self

    async def __aexit__(self, *_a) -> bool:
        return False

    async def post(self, url, headers=None, json=None) -> _FakeResp:
        _RecordingClient.calls.append(
            {"url": url, "headers": dict(headers or {}), "json": json}
        )
        return _FakeResp()


@pytest.fixture
def record_http(monkeypatch):
    _RecordingClient.calls = []
    monkeypatch.setattr(httpx, "AsyncClient", _RecordingClient)
    return _RecordingClient


# ---------------------------------------------------------------------------
# (a) in-task search carries this request's dispatch JWT — not a csk-
# ---------------------------------------------------------------------------


async def test_in_task_search_sends_dispatch_jwt_not_csk(record_http):
    retriever = CspHttpRetriever(
        csp_base_url="https://csp.internal",
        collection_id=7,
        api_key=None,  # service_wrapper path: no static csk-
    )
    with dispatch_bearer_scope("eyJ.dispatch.token.A"):
        await retriever.search("what is ANILA?", k=3)

    assert len(record_http.calls) == 1
    auth = record_http.calls[0]["headers"]["Authorization"]
    assert auth == "Bearer eyJ.dispatch.token.A"
    assert "csk-" not in auth
    assert record_http.calls[0]["url"].endswith(
        "/api/ingestion/collections/7/search"
    )


async def test_service_wrapper_search_uses_inbound_bearer(record_http, monkeypatch):
    """End-to-end through chat_completions: inbound Bearer == outbound Bearer."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from anila_agent.serving import service_wrapper

    built: dict = {}

    class _Result:
        final_output = "ok"

    def _fake_build_agent(*_a, **kw):
        built["retriever"] = kw.get("retriever")
        return object()

    async def _run_with_search(*_a, **_k):
        retriever = built["retriever"]
        # May be TracingRetriever when trace header present; unwrap _inner.
        inner = getattr(retriever, "_inner", retriever)
        await inner.search("probe")
        return _Result()

    async def _fake_claims(_authorization=None, **_kwargs):
        return {"user_id": 42, "department": 1, "agent_id": 9}

    monkeypatch.setattr(service_wrapper, "COLLECTION_ID", 12)
    monkeypatch.setattr(
        service_wrapper, "verify_dispatch_authorization", _fake_claims
    )
    monkeypatch.setattr(service_wrapper, "build_model", lambda *a, **k: object())
    monkeypatch.setattr(service_wrapper, "build_agent", _fake_build_agent)
    monkeypatch.setattr(service_wrapper, "run_once", _run_with_search)

    with TestClient(service_wrapper.app) as client:
        resp = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer eyJ.in.flight.dispatch"},
            json={
                "model": "anila-agent",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
    assert resp.status_code == 200
    assert record_http.calls, "search was not invoked"
    assert (
        record_http.calls[0]["headers"]["Authorization"]
        == "Bearer eyJ.in.flight.dispatch"
    )


# ---------------------------------------------------------------------------
# (F3) pin api_key=None on in-task retriever + emitter
# ---------------------------------------------------------------------------


async def test_service_wrapper_passes_api_key_none(monkeypatch):
    """Restoring CSP_SERVICE_TOKEN as constructor api_key must turn this RED."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from anila_agent.serving import service_wrapper

    built: dict = {}

    class _Result:
        final_output = "ok"

    real_build_emitter = service_wrapper._build_emitter
    real_csp = service_wrapper.CspHttpRetriever

    def _capture_emitter(*a, **k):
        em = real_build_emitter(*a, **k)
        built["emitter"] = em
        return em

    class _CapturingRetriever(real_csp):
        def __init__(self, *a, **kw):
            built["retriever_api_key"] = kw.get("api_key", "MISSING")
            super().__init__(*a, **kw)

    async def _fake_claims(_authorization=None, **_kwargs):
        return {"user_id": 1, "department": 1, "agent_id": 9}

    async def _run(*_a, **_k):
        return _Result()

    monkeypatch.setattr(service_wrapper, "COLLECTION_ID", 12)
    monkeypatch.setattr(service_wrapper, "CSP_BASE_URL", "https://csp.internal")
    monkeypatch.setattr(service_wrapper, "TRACE_ENDPOINT", "https://csp.internal")
    monkeypatch.setattr(
        service_wrapper, "verify_dispatch_authorization", _fake_claims
    )
    monkeypatch.setattr(service_wrapper, "build_model", lambda *a, **k: object())
    monkeypatch.setattr(service_wrapper, "build_agent", lambda *a, **k: object())
    monkeypatch.setattr(service_wrapper, "run_once", _run)
    monkeypatch.setattr(service_wrapper, "_build_emitter", _capture_emitter)
    monkeypatch.setattr(service_wrapper, "CspHttpRetriever", _CapturingRetriever)

    with TestClient(service_wrapper.app) as client:
        resp = client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": "Bearer eyJ.pin.none",
                "X-ANILA-Trace-Id": "trace-pin",
            },
            json={
                "model": "anila-agent",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
    assert resp.status_code == 200
    assert built["retriever_api_key"] is None
    assert built["emitter"].api_key is None


# ---------------------------------------------------------------------------
# (b) concurrent requests — real interleaving (THE important test)
# ---------------------------------------------------------------------------


async def test_concurrent_dispatch_tokens_do_not_cross_contaminate(record_http):
    """Two overlapping searches MUST each send their own JWT.

    Ordering pin: A's outbound runs after B has entered its scope and before
    B leaves. A faithful module-global (save/restore) then makes A read B's
    JWT → this test goes RED. A barrier-only test without that pin stays
    green under the same mutation (no real interleaving).
    """
    retriever = CspHttpRetriever(
        csp_base_url="https://csp", collection_id=3, api_key=None
    )
    b_entered = asyncio.Event()
    a_outbound_done = asyncio.Event()
    seen_during_overlap: dict[str, str] = {}

    async def _worker_a() -> None:
        with dispatch_bearer_scope("jwt-AAA"):
            await b_entered.wait()  # B is inside its scope (would overwrite a global)
            seen_during_overlap["A"] = get_dispatch_bearer() or ""
            await retriever.search("A")
            a_outbound_done.set()

    async def _worker_b() -> None:
        with dispatch_bearer_scope("jwt-BBB"):
            b_entered.set()
            # Hold B's scope open for the entire duration of A's outbound.
            await a_outbound_done.wait()
            seen_during_overlap["B"] = get_dispatch_bearer() or ""
            await retriever.search("B")

    await asyncio.gather(_worker_a(), _worker_b())

    assert seen_during_overlap == {"A": "jwt-AAA", "B": "jwt-BBB"}
    auths = sorted(c["headers"]["Authorization"] for c in record_http.calls)
    assert auths == ["Bearer jwt-AAA", "Bearer jwt-BBB"]


# ---------------------------------------------------------------------------
# (c) no token in scope → fail loudly (no silent unauthenticated POST)
# ---------------------------------------------------------------------------


async def test_outbound_without_scope_fails_loudly(record_http):
    retriever = CspHttpRetriever(
        csp_base_url="https://csp", collection_id=3, api_key=None
    )
    with pytest.raises(MissingDispatchTokenError, match="no dispatch JWT"):
        await retriever.search("orphan")
    assert record_http.calls == []


def test_resolve_outbound_bearer_fails_loudly_with_no_fallback():
    with pytest.raises(MissingDispatchTokenError):
        resolve_outbound_bearer(fallback=None)
    with pytest.raises(MissingDispatchTokenError):
        resolve_outbound_bearer(fallback="   ")


async def test_trace_flush_without_creds_does_not_post(record_http, caplog):
    em = TraceEmitter(
        trace_id="t1",
        endpoint="https://csp.local",
        api_key=None,
        csp_base_url="https://csp.local",
        enabled=True,
    )
    async with em.run_span("agent"):
        pass
    with caplog.at_level("ERROR"):
        await em.flush()
    assert record_http.calls == []
    assert any("trace ship aborted" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# (F2) non-CSP outbound origin must NOT receive the dispatch JWT
# ---------------------------------------------------------------------------


def test_same_csp_origin_helper():
    assert same_csp_origin("https://csp.internal/v1", "https://csp.internal")
    assert same_csp_origin("https://CSP.INTERNAL", "https://csp.internal/")
    assert not same_csp_origin("https://evil.collector", "https://csp.internal")
    assert not same_csp_origin("http://csp.internal", "https://csp.internal")


async def test_non_csp_trace_endpoint_does_not_receive_dispatch_jwt(
    record_http, caplog
):
    """ANILA_TRACE_ENDPOINT → foreign host must not get the user JWT."""
    em = TraceEmitter(
        trace_id="t-foreign",
        endpoint="https://evil.collector",
        api_key=None,
        csp_base_url="https://csp.internal",
        enabled=True,
    )
    async with em.run_span("agent"):
        pass
    with dispatch_bearer_scope("eyJ.user.dispatch.jwt"):
        with caplog.at_level("ERROR"):
            await em.flush()
    assert record_http.calls == [], "must not POST user JWT to foreign collector"
    assert any("non-CSP origin" in r.message for r in caplog.records)


async def test_non_csp_trace_falls_back_to_static_credential(record_http):
    em = TraceEmitter(
        trace_id="t-foreign",
        endpoint="https://evil.collector",
        api_key="csk-collector-only",
        csp_base_url="https://csp.internal",
        enabled=True,
    )
    async with em.run_span("agent"):
        pass
    with dispatch_bearer_scope("eyJ.user.dispatch.jwt"):
        await em.flush()
    assert len(record_http.calls) == 1
    auth = record_http.calls[0]["headers"]["Authorization"]
    assert auth == "Bearer csk-collector-only"
    assert "eyJ.user.dispatch.jwt" not in auth


async def test_non_csp_retriever_origin_does_not_receive_dispatch_jwt(record_http):
    """Retriever pointed at a foreign host must not paste the user JWT."""
    retriever = CspHttpRetriever(
        csp_base_url="https://evil.search",
        collection_id=3,
        api_key=None,
        trusted_csp_base_url="https://csp.internal",
    )
    with dispatch_bearer_scope("eyJ.user.dispatch.jwt"):
        with pytest.raises(MissingDispatchTokenError, match="non-CSP origin"):
            await retriever.search("leak?")
    assert record_http.calls == []


# ---------------------------------------------------------------------------
# (F1) SSE disconnect mid-stream — no cross-context ValueError; scope clean
# ---------------------------------------------------------------------------


async def test_sse_disconnect_mid_stream_resets_scope_cleanly(monkeypatch):
    """aclose() mid-stream must not leak ValueError or leave the bearer set."""
    from agents.stream_events import RawResponsesStreamEvent
    from openai.types.responses import ResponseTextDeltaEvent

    from anila_agent.serving import service_wrapper

    park = asyncio.Event()
    released = asyncio.Event()

    class _FakeUsage:
        input_tokens = 1
        output_tokens = 1
        total_tokens = 2

    class _FakeCtx:
        usage = _FakeUsage()

    class _ParkingStream:
        context_wrapper = _FakeCtx()

        async def stream_events(self):
            data = ResponseTextDeltaEvent(
                content_index=0,
                delta="partial",
                item_id="item-1",
                logprobs=[],
                output_index=0,
                sequence_number=0,
                type="response.output_text.delta",
            )
            yield RawResponsesStreamEvent(data=data, type="raw_response_event")
            park.set()
            await released.wait()  # hang until consumer disconnects

    monkeypatch.setattr(
        service_wrapper, "run_streamed", lambda *a, **k: _ParkingStream()
    )

    loop = asyncio.get_running_loop()
    loop_errors: list[BaseException] = []

    def _handler(_loop, context):
        exc = context.get("exception")
        if exc is not None:
            loop_errors.append(exc)

    prev_handler = loop.get_exception_handler()
    loop.set_exception_handler(_handler)
    try:
        agen = service_wrapper._sse_stream(
            None, "hi", hooks=None, dispatch_bearer="JWT-DISC"
        )
        # Drain role + content chunks until producer parks inside the stream body.
        while not park.is_set():
            chunk = await asyncio.wait_for(agen.__anext__(), timeout=2.0)
            assert chunk.startswith("data:")
        await asyncio.wait_for(park.wait(), timeout=2.0)

        # Client disconnect: finalize the generator (producer Task cancelled;
        # its dispatch_bearer_scope finally runs in the same Task Context).
        await agen.aclose()
        released.set()
        await asyncio.sleep(0.05)
    finally:
        loop.set_exception_handler(prev_handler)
        released.set()

    assert get_dispatch_bearer() is None
    for exc in loop_errors:
        assert not (
            isinstance(exc, ValueError) and "different Context" in str(exc)
        ), f"cross-context reset leaked: {exc!r}"


async def test_reset_suppresses_cross_context_valueerror():
    """Token created in Context A, reset attempted from Context B → no raise."""
    from anila_agent import dispatch_token as dt

    token_box: list = []

    def _set_in_ctx() -> None:
        token_box.append(dt._set_dispatch_bearer("JWT-CROSS"))

    ctx = contextvars.copy_context()
    ctx.run(_set_in_ctx)
    # Reset from *this* context (different from where set ran).
    dt._reset_dispatch_bearer(token_box[0])  # must not raise
    # Bearer lives in the other Context's copy; ours stays clean.
    assert get_dispatch_bearer() is None
