from __future__ import annotations

import json
from types import SimpleNamespace

from anila_contracts import Classification, StepEvent
import pytest
from fastapi import HTTPException

import app.api.proxy as proxy_api
import app.services.proxy_service as legacy_proxy_transport
from app.services.proxy import service as proxy_service


class _FakeGoverned:
    def __init__(self, *, fail_complete: bool = False) -> None:
        self.fail_complete = fail_complete
        self.authorizations: list[dict] = []
        self.completions: list[dict] = []
        self.failures: list[object] = []

    def authorize(self, **kwargs):
        self.authorizations.append(kwargs)
        return SimpleNamespace(invocation_id="proxy-test-invocation")

    def complete(self, authorization, usage):
        self.completions.append({"authorization": authorization, "usage": usage})
        if self.fail_complete:
            raise RuntimeError("synthetic usage receipt failure")

    def fail(self, authorization, error):
        self.failures.append(error)


def _model():
    return SimpleNamespace(
        id=7,
        name="gate5-test-model",
        model_type="llm",
        endpoint_url="http://unused.invalid/v1",
    )


_LOW_CLASSIFICATION = Classification.UNCLASSIFIED.to_storage()
_HIGH_CLASSIFICATION = Classification.CONFIDENTIAL.to_storage()


def _install_classification_latch(monkeypatch):
    """Model the writer upgrade between pre-admission and the row lock."""

    observed = {"prelock": [], "lock": [], "concurrent_upgrade": False}

    def prelock_admission(**kwargs):
        observed["prelock"].append(kwargs["admitted_classification_level"])
        # The concurrent transaction raises the durable classification after
        # the user-friendly admission check but before the row lock returns.
        observed["concurrent_upgrade"] = True

    def locked_admission(**kwargs):
        observed["lock"].append(kwargs["admitted_classification_level"])
        assert observed["concurrent_upgrade"] is True
        return _HIGH_CLASSIFICATION

    monkeypatch.setattr(
        proxy_service, "_require_pilot_sink_admission", prelock_admission
    )
    monkeypatch.setattr(
        proxy_service, "_lock_task_run_admission", locked_admission
    )
    return observed


def _step_event_json() -> str:
    return StepEvent(
        event_id="upstream-event",
        sequence=1,
        cursor="1",
        trace_id="forged-trace",
        task_id="forged-task",
        session_id="forged-session",
        invocation_id="upstream-invocation",
        run_id="forged-run",
        step_id="tool:1",
        kind="tool",
        status="running",
        agent_id="forged-agent",
        tool_name="search_documents",
        safe_input_summary="read documents",
        classification=Classification.UNCLASSIFIED,
    ).model_dump_json()


@pytest.mark.asyncio
async def test_proxy_request_model_has_one_central_pre_post_pair(monkeypatch):
    governed = _FakeGoverned()
    monkeypatch.setattr(
        proxy_service,
        "_build_governed_model_invocation",
        lambda **_kwargs: governed,
    )
    calls = []

    async def fake_impl(**kwargs):
        calls.append(kwargs)
        kwargs["usage_capture"]["governance_usage"] = {
            "prompt_tokens": 2,
            "completion_tokens": 3,
            "total_tokens": 5,
        }
        return {"id": "chatcmpl-test", "usage": {"total_tokens": 5}}

    monkeypatch.setattr(proxy_service, "_proxy_request_impl", fake_impl)
    result = await proxy_service.proxy_request(
        model=_model(),
        api_key_id=None,
        user_id=11,
        department_id=None,
        request_body={"model": "gate5-test-model", "messages": []},
        endpoint_path="/v1/chat/completions",
        governance_callsite_id="r7.csp.proxy",
    )

    assert result["id"] == "chatcmpl-test"
    assert len(calls) == 1
    assert len(governed.authorizations) == 1
    assert governed.authorizations[0]["callsite_id"] == "r7.csp.proxy"
    assert [item["usage"]["total_tokens"] for item in governed.completions] == [5]
    assert governed.failures == []


@pytest.mark.asyncio
async def test_nested_studio_image_call_records_terminal_receipt_without_owning_run(
    monkeypatch,
):
    """Studio runs use the generic CSP receipt identity, never Agent scope."""

    governed = _FakeGoverned()
    monkeypatch.setattr(
        proxy_service,
        "_build_governed_model_invocation",
        lambda **_kwargs: governed,
    )

    async def fake_impl(**kwargs):
        kwargs["usage_capture"]["governance_usage"] = {
            "prompt_tokens": 2,
            "completion_tokens": 0,
            "total_tokens": 2,
        }
        return {"created": 1_720_000_000, "data": [{"b64_json": "synthetic"}]}

    monkeypatch.setattr(proxy_service, "_proxy_request_impl", fake_impl)
    model = _model()
    model.model_type = "image"
    result = await proxy_service.proxy_request(
        model=model,
        api_key_id=None,
        user_id=11,
        department_id=None,
        request_body={"model": model.name, "prompt": "synthetic"},
        endpoint_path="/v1/images/generations",
        task_id=7,
        task_trace_id="studio-trace",
        task_run_id=9,
        inference_callsite_id="csp.image_generation",
        governance_callsite_id="r7.csp.proxy",
        finalize_task_run_on_completion=False,
    )

    assert result["data"][0]["b64_json"] == "synthetic"
    assert governed.authorizations[0]["callsite_id"] == "r7.csp.proxy"
    assert governed.completions[0]["usage"]["total_tokens"] == 2
    assert governed.failures == []


@pytest.mark.asyncio
async def test_nested_studio_image_upstream_failure_records_failure_receipt(
    monkeypatch,
):
    governed = _FakeGoverned()
    monkeypatch.setattr(
        proxy_service,
        "_build_governed_model_invocation",
        lambda **_kwargs: governed,
    )

    async def failed_impl(**_kwargs):
        raise HTTPException(status_code=502, detail="synthetic image upstream failure")

    monkeypatch.setattr(proxy_service, "_proxy_request_impl", failed_impl)
    model = _model()
    model.model_type = "image"
    with pytest.raises(HTTPException, match="synthetic image upstream failure"):
        await proxy_service.proxy_request(
            model=model,
            api_key_id=None,
            user_id=11,
            department_id=None,
            request_body={"model": model.name, "prompt": "synthetic"},
            endpoint_path="/v1/images/generations",
            task_id=7,
            task_trace_id="studio-trace",
            task_run_id=9,
            inference_callsite_id="csp.image_generation",
            governance_callsite_id="r7.csp.proxy",
            finalize_task_run_on_completion=False,
        )

    assert governed.authorizations[0]["callsite_id"] == "r7.csp.proxy"
    assert len(governed.failures) == 1
    assert governed.completions == []


@pytest.mark.asyncio
async def test_governed_authorize_uses_one_atomic_authority_snapshot(monkeypatch, db):
    """The proxy seam must not resolve provider/facts through three reloads."""

    calls = []

    class _AtomicRuntime:
        enabled = True
        gateway_endpoint = "https://csp-model-gateway/v1"

        def resolve_provider_binding(self, *_args, **_kwargs):
            raise AssertionError("provider must be resolved by atomic authorize")

        def invocation_facts(self, *_args, **_kwargs):
            raise AssertionError("observed facts must use atomic authorize")

        def authorize_model_invocation(self, *args, **kwargs):
            calls.append((args, kwargs))
            assert kwargs["registry_model"] is registry_model
            return SimpleNamespace(invocation_id="atomic-invocation")

    registry_model = SimpleNamespace(
        id=7,
        name="atomic-model",
        model_type="llm",
        endpoint_url="model.internal:8000",
    )
    from app.services.model_governance_receipts import (
        GovernedModelInvocation,
        ReceiptSubject,
    )

    governed = GovernedModelInvocation(
        runtime=_AtomicRuntime(),
        db=db,
        subject=ReceiptSubject(user_id=11, model_id=7),
        registry_model=registry_model,
    )
    authorization = governed.authorize(
        callsite_id="r7.csp.proxy",
        classification="無機密",
        invocation_id="atomic-invocation",
    )
    assert authorization.invocation_id == "atomic-invocation"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_proxy_stream_model_has_one_central_pre_post_pair(monkeypatch):
    governed = _FakeGoverned()
    monkeypatch.setattr(
        proxy_service,
        "_build_governed_model_invocation",
        lambda **_kwargs: governed,
    )

    async def fake_stream_impl(**kwargs):
        kwargs["terminal_capture"]["governance_usage"] = {
            "prompt_tokens": 4,
            "completion_tokens": 1,
            "total_tokens": 5,
        }
        yield "data: {\"choices\":[]}\n\n"

    monkeypatch.setattr(proxy_service, "_proxy_stream_impl", fake_stream_impl)
    stream = proxy_service.proxy_stream(
        target_url="http://unused.invalid/v1/chat/completions",
        api_key_id=None,
        user_id=11,
        department_id=None,
        usage_model_id=7,
        request_body={"model": "gate5-test-model", "messages": []},
        model_name="gate5-test-model",
        governance_callsite_id="r7.csp.proxy-service",
    )

    chunks = [chunk async for chunk in stream]
    assert chunks == ['data: {"choices":[]}\n\n']
    assert len(governed.authorizations) == 1
    assert len(governed.completions) == 1
    assert governed.completions[0]["usage"]["total_tokens"] == 5
    assert governed.failures == []


@pytest.mark.asyncio
async def test_sync_proxy_uses_locked_snapshot_after_receipt_commit(monkeypatch):
    original = _model()
    locked = SimpleNamespace(
        id=original.id,
        name=original.name,
        model_type=original.model_type,
        endpoint_url=original.endpoint_url,
        api_version="v1",
        api_key_secret_ref=None,
    )
    observed = []

    class _AuthorizeAndCommit:
        def authorize(self, **_kwargs):
            # Simulate the SQLAlchemy pre-audit commit expiring ``original``
            # while the outbound input must remain the locked value.
            original.endpoint_url = "http://attacker-replacement.invalid/v1"
            return SimpleNamespace(invocation_id="snapshot-invocation")

        def complete(self, *_args):
            return None

        def fail(self, *_args):
            return None

    monkeypatch.setattr(
        proxy_service,
        "_lock_registry_admission",
        lambda **_kwargs: locked,
    )
    monkeypatch.setattr(
        proxy_service,
        "_build_governed_model_invocation",
        lambda **_kwargs: _AuthorizeAndCommit(),
    )

    async def fake_impl(**kwargs):
        observed.append(kwargs["model"])
        return {"id": "snapshot-result"}

    monkeypatch.setattr(proxy_service, "_proxy_request_impl", fake_impl)
    result = await proxy_service.proxy_request(
        model=original,
        api_key_id=None,
        user_id=11,
        department_id=None,
        request_body={"model": original.name, "messages": []},
        endpoint_path="/v1/chat/completions",
        governance_db=SimpleNamespace(),
        governance_callsite_id="r7.csp.proxy",
    )

    assert result["id"] == "snapshot-result"
    assert observed == [locked]


@pytest.mark.asyncio
async def test_formal_missing_binding_rejects_before_proxy_or_http(monkeypatch):
    monkeypatch.setattr(
        proxy_service,
        "resolve_model_governance_runtime",
        lambda: SimpleNamespace(enabled=True),
    )

    async def must_not_run(**_kwargs):
        raise AssertionError("formal missing binding must be zero outbound")

    monkeypatch.setattr(proxy_service, "_proxy_request_impl", must_not_run)
    http_calls = []

    def forbidden_client(*_args, **_kwargs):
        http_calls.append(True)
        raise AssertionError("httpx client must not be created")

    monkeypatch.setattr(proxy_service.httpx, "AsyncClient", forbidden_client)
    with pytest.raises(HTTPException) as raised:
        await proxy_service.proxy_request(
            model=_model(),
            api_key_id=None,
            user_id=11,
            department_id=None,
            request_body={"model": "gate5-test-model", "messages": []},
            endpoint_path="/v1/chat/completions",
            governance_callsite_id=None,
        )

    assert raised.value.status_code == 503
    assert http_calls == []


@pytest.mark.asyncio
async def test_post_usage_failure_does_not_trigger_duplicate_failure_close(monkeypatch):
    governed = _FakeGoverned(fail_complete=True)
    monkeypatch.setattr(
        proxy_service,
        "_build_governed_model_invocation",
        lambda **_kwargs: governed,
    )

    async def fake_impl(**kwargs):
        kwargs["usage_capture"]["governance_usage"] = {
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "total_tokens": 2,
        }
        return {"usage": {"total_tokens": 2}}

    monkeypatch.setattr(proxy_service, "_proxy_request_impl", fake_impl)
    with pytest.raises(HTTPException) as raised:
        await proxy_service.proxy_request(
            model=_model(),
            api_key_id=None,
            user_id=11,
            department_id=None,
            request_body={"model": "gate5-test-model", "messages": []},
            endpoint_path="/v1/chat/completions",
            governance_callsite_id="r7.csp.proxy",
        )

    assert raised.value.status_code == 503
    assert len(governed.completions) == 1
    # The real GovernedModelInvocation marks a failed post receipt closed and
    # commits its failure audit; the proxy must not submit a second fail.
    assert governed.failures == []


@pytest.mark.asyncio
async def test_locked_agent_typed_registry_row_cannot_bypass_provider_authority(
    monkeypatch,
):
    events = []
    locked = SimpleNamespace(id=7, name="locked-agent-model", model_type="agent")

    class _DeniedGoverned:
        def authorize(self, **_kwargs):
            events.append("authorize")
            raise RuntimeError("provider authority denied")

    monkeypatch.setattr(
        proxy_service,
        "_lock_registry_admission",
        lambda **_kwargs: events.append("lock") or locked,
    )

    def build(**kwargs):
        events.append("build")
        assert kwargs["model_type"] == "agent"
        assert kwargs["target_agent_id"] is None
        assert kwargs["registry_model"] is locked
        return _DeniedGoverned()

    monkeypatch.setattr(proxy_service, "_build_governed_model_invocation", build)

    async def forbidden_network(**_kwargs):
        events.append("network")
        raise AssertionError("provider denial must be zero outbound")

    monkeypatch.setattr(proxy_service, "_proxy_request_impl", forbidden_network)
    model = _model()
    model.model_type = "agent"

    with pytest.raises(HTTPException) as raised:
        await proxy_service.proxy_request(
            model=model,
            api_key_id=None,
            user_id=11,
            department_id=None,
            request_body={"model": model.name, "messages": []},
            endpoint_path="/v1/chat/completions",
            governance_db=SimpleNamespace(),
            governance_callsite_id="r7.csp.proxy",
        )
    assert raised.value.status_code == 503
    assert events == ["lock", "build", "authorize"]


@pytest.mark.asyncio
async def test_stream_authority_denial_precedes_lock_release_and_network(monkeypatch):
    events = []
    locked = SimpleNamespace(id=7, name="locked-stream-model", model_type="llm")

    class _DeniedGoverned:
        def authorize(self, **_kwargs):
            events.append("authorize")
            raise RuntimeError("provider authority denied")

    monkeypatch.setattr(
        proxy_service,
        "_lock_registry_admission",
        lambda **_kwargs: events.append("lock") or locked,
    )
    def build(**kwargs):
        events.append("build")
        assert kwargs["registry_model"] is locked
        return _DeniedGoverned()

    monkeypatch.setattr(proxy_service, "_build_governed_model_invocation", build)
    monkeypatch.setattr(
        proxy_service,
        "_commit_stream_admission",
        lambda _db: events.append("commit"),
    )

    async def forbidden_stream(**_kwargs):
        events.append("network")
        yield "never"

    monkeypatch.setattr(proxy_service, "_proxy_stream_impl", forbidden_stream)
    stream = proxy_service.proxy_stream(
        target_url="http://unused.invalid/v1/chat/completions",
        api_key_id=None,
        user_id=11,
        department_id=None,
        usage_model_id=7,
        request_body={"model": "gate5-test-model", "messages": []},
        model_name="gate5-test-model",
        governance_db=SimpleNamespace(),
        governance_callsite_id="r7.csp.proxy-service",
    )

    with pytest.raises(HTTPException) as raised:
        async for _ in stream:
            pass
    assert raised.value.status_code == 503
    assert events == ["lock", "build", "authorize"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target_agent_id",
    [None, 42],
    ids=["model", "agent"],
)
async def test_proxy_request_latched_classification_reaches_all_post_lock_consumers(
    monkeypatch, target_agent_id
):
    """A concurrent raise must survive governance, forwarding, and closure."""

    observed = _install_classification_latch(monkeypatch)
    governed = _FakeGoverned()
    downstream_levels = []
    registry_levels = []
    closures = []
    monkeypatch.setattr(
        proxy_service,
        "_build_governed_model_invocation",
        lambda **_kwargs: governed,
    )
    monkeypatch.setattr(
        proxy_service,
        "_lock_registry_admission",
        lambda **kwargs: registry_levels.append(
            kwargs["admitted_classification_level"]
        )
        or None,
    )
    monkeypatch.setattr(
        proxy_service,
        "persist_task_call_closure",
        lambda _db, closure: closures.append(closure),
    )

    async def fake_impl(**kwargs):
        downstream_levels.append(kwargs["admitted_classification_level"])
        kwargs["usage_capture"]["governance_usage"] = {"total_tokens": 1}
        return {"id": "latched-request"}

    monkeypatch.setattr(proxy_service, "_proxy_request_impl", fake_impl)
    model = _model()
    if target_agent_id is not None:
        model.model_type = "agent"

    result = await proxy_service.proxy_request(
        model=model,
        api_key_id=None,
        user_id=11,
        department_id=None,
        request_body={"model": model.name, "messages": []},
        endpoint_path="/v1/chat/completions",
        target_agent_id=target_agent_id,
        task_id=10,
        task_trace_id="latched-trace",
        task_run_id=11,
        inference_callsite_id="csp.latch.test",
        governance_callsite_id="r7.csp.proxy",
        governance_db=SimpleNamespace(),
        admitted_classification_level=_LOW_CLASSIFICATION,
    )

    assert result["id"] == "latched-request"
    assert observed["prelock"] == [_LOW_CLASSIFICATION]
    assert observed["lock"] == [_LOW_CLASSIFICATION]
    assert registry_levels == [_HIGH_CLASSIFICATION]
    assert downstream_levels == [_HIGH_CLASSIFICATION]
    assert governed.authorizations[0]["classification"] is Classification.CONFIDENTIAL
    assert closures and closures[-1].classification_level == _HIGH_CLASSIFICATION


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target_agent_id",
    [None, 42],
    ids=["model", "agent"],
)
async def test_proxy_stream_latched_classification_reaches_headers_validator_and_audit(
    monkeypatch, target_agent_id
):
    """The stream trust boundary must bind to the post-lock classification."""

    observed = _install_classification_latch(monkeypatch)
    governed = _FakeGoverned()
    downstream_levels = []
    registry_levels = []
    validator_contexts = []
    captured = {}
    closures = []
    monkeypatch.setattr(
        proxy_service,
        "_build_governed_model_invocation",
        lambda **_kwargs: governed,
    )
    monkeypatch.setattr(
        proxy_service,
        "_lock_registry_admission",
        lambda **kwargs: registry_levels.append(
            kwargs["admitted_classification_level"]
        )
        or None,
    )
    monkeypatch.setattr(
        proxy_service,
        "_commit_stream_admission",
        lambda _db: None,
    )
    monkeypatch.setattr(
        proxy_service,
        "persist_task_call_closure",
        lambda _db, closure: closures.append(closure),
    )
    monkeypatch.setattr(proxy_service, "_guard_outbound", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        proxy_service, "_apply_gateway_auth", lambda _headers, _key: None
    )
    monkeypatch.setattr(
        proxy_service,
        "build_agent_headers",
        lambda *_a, **_kw: {"Content-Type": "application/json"},
    )

    payload = (
        "event: anila.step\n"
        f"data: {_step_event_json()}\n\n"
        "data: [DONE]\n\n"
    ).encode()

    class _Response:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def aiter_bytes(self, chunk_size=None):
            assert chunk_size == 64 * 1024
            yield payload

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        def stream(self, _method, _url, *, json, headers):
            captured["body"] = json
            captured["headers"] = dict(headers)
            return _Response()

    monkeypatch.setattr(
        proxy_service.httpx,
        "AsyncClient",
        lambda *_a, **_kw: _Client(),
    )
    original_validator = proxy_service.StreamValidator

    def capture_validator(context, **kwargs):
        validator_contexts.append(context)
        return original_validator(context, **kwargs)

    monkeypatch.setattr(proxy_service, "StreamValidator", capture_validator)
    original_impl = proxy_service._proxy_stream_impl

    async def observe_impl(**kwargs):
        downstream_levels.append(kwargs["admitted_classification_level"])
        async for chunk in original_impl(**kwargs):
            yield chunk

    monkeypatch.setattr(proxy_service, "_proxy_stream_impl", observe_impl)

    stream = proxy_service.proxy_stream(
        target_url="http://unused.invalid/v1/chat/completions",
        api_key_id=None,
        user_id=11,
        department_id=None,
        usage_model_id=7,
        request_body={"model": "latched-model", "messages": []},
        model_name="latched-model",
        target_agent_id=target_agent_id,
        task_id=10,
        task_trace_id="latched-stream-trace",
        task_run_id=11,
        inference_callsite_id="csp.latch.stream",
        governance_callsite_id="r7.csp.proxy-service",
        governance_db=SimpleNamespace(),
        admitted_classification_level=_LOW_CLASSIFICATION,
    )
    chunks = [chunk async for chunk in stream]

    assert observed["prelock"] == [_LOW_CLASSIFICATION, _HIGH_CLASSIFICATION]
    assert observed["lock"] == [_LOW_CLASSIFICATION]
    assert registry_levels == [_HIGH_CLASSIFICATION]
    assert downstream_levels == [_HIGH_CLASSIFICATION]
    assert governed.authorizations[0]["classification"] is Classification.CONFIDENTIAL
    assert closures and closures[-1].classification_level == _HIGH_CLASSIFICATION
    if target_agent_id is None:
        assert validator_contexts == []
    else:
        assert captured["headers"]["X-ANILA-Classification-Level"] == _HIGH_CLASSIFICATION
        assert validator_contexts[0].classification is Classification.CONFIDENTIAL
        step_chunk = next(chunk for chunk in chunks if chunk.startswith("event: anila.step"))
        step_data = next(
            line[6:] for line in step_chunk.splitlines() if line.startswith("data: ")
        )
        assert json.loads(step_data)["classification"] == _HIGH_CLASSIFICATION


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "is_stream",
    [False, True],
    ids=["non-stream", "stream"],
)
@pytest.mark.parametrize(
    "target_agent_id",
    [None, 42],
    ids=["model", "agent"],
)
async def test_latched_high_classification_denies_unauthorized_callsite(
    monkeypatch, is_stream, target_agent_id
):
    """A callsite allowed at the stale floor cannot pass the raised level."""

    observed = _install_classification_latch(monkeypatch)

    class _DeniedAtHigherClassification(_FakeGoverned):
        def authorize(self, **kwargs):
            result = super().authorize(**kwargs)
            if kwargs["classification"] is Classification.CONFIDENTIAL:
                raise HTTPException(
                    status_code=403,
                    detail="callsite is not authorized at effective classification",
                )
            return result

    governed = _DeniedAtHigherClassification()
    downstream_called = []
    monkeypatch.setattr(
        proxy_service,
        "_build_governed_model_invocation",
        lambda **_kwargs: governed,
    )
    monkeypatch.setattr(
        proxy_service,
        "_lock_registry_admission",
        lambda **_kwargs: None,
    )

    async def forbidden_request(**_kwargs):
        downstream_called.append("request")
        raise AssertionError("authorization must deny before non-stream I/O")

    async def forbidden_stream(**_kwargs):
        downstream_called.append("stream")
        raise AssertionError("authorization must deny before stream I/O")
        yield "unreachable"

    monkeypatch.setattr(proxy_service, "_proxy_request_impl", forbidden_request)
    monkeypatch.setattr(proxy_service, "_proxy_stream_impl", forbidden_stream)
    model = _model()
    if target_agent_id is not None:
        model.model_type = "agent"

    if is_stream:
        stream = proxy_service.proxy_stream(
            target_url="http://unused.invalid/v1/chat/completions",
            api_key_id=None,
            user_id=11,
            department_id=None,
            usage_model_id=7,
            request_body={"model": model.name, "messages": []},
            model_name=model.name,
            target_agent_id=target_agent_id,
            governance_callsite_id="r7.csp.proxy-service",
            governance_db=SimpleNamespace(),
            admitted_classification_level=_LOW_CLASSIFICATION,
        )
        with pytest.raises(HTTPException) as raised:
            async for _chunk in stream:
                pass
    else:
        with pytest.raises(HTTPException) as raised:
            await proxy_service.proxy_request(
                model=model,
                api_key_id=None,
                user_id=11,
                department_id=None,
                request_body={"model": model.name, "messages": []},
                endpoint_path="/v1/chat/completions",
                target_agent_id=target_agent_id,
                governance_callsite_id="r7.csp.proxy",
                governance_db=SimpleNamespace(),
                admitted_classification_level=_LOW_CLASSIFICATION,
            )

    assert raised.value.status_code == 403
    assert observed["prelock"] == [_LOW_CLASSIFICATION]
    assert observed["lock"] == [_LOW_CLASSIFICATION]
    assert governed.authorizations[0]["classification"] is Classification.CONFIDENTIAL
    assert downstream_called == []


@pytest.mark.asyncio
async def test_legacy_nonstream_agent_latch_authorizes_and_closes_at_effective_level(
    monkeypatch,
):
    """The hand-rolled Agent branch must bind registry auth and closure to the lock."""

    observed = {
        "prelock": [],
        "task_lock": False,
        "registry_levels": [],
        "dispatch": False,
    }
    agent = SimpleNamespace(
        id=42,
        name="latched-agent",
        endpoint_url="http://agent.invalid",
        base_model_id=7,
        requires_encryption=False,
    )
    user = SimpleNamespace(
        id=11,
        username="latched-user",
        email=None,
        department_id=None,
    )
    caller = SimpleNamespace(user=user, api_key_id=None)
    task_ctx = SimpleNamespace(
        task_id=10,
        task_run_id=11,
        trace_id="latched-agent-trace",
        owns_lifecycle=True,
        started_at=None,
    )

    class RequestStub:
        headers = {}
        state = SimpleNamespace()

        async def json(self):
            return {
                "model": agent.name,
                "stream": False,
                "messages": [{"role": "user", "content": "hello"}],
            }

    def prelock_ceiling(_db, **_kwargs):
        observed["prelock"].append(_LOW_CLASSIFICATION)
        return _LOW_CLASSIFICATION

    def locked_task_run(**_kwargs):
        assert observed["prelock"]
        assert observed["prelock"][-1] == _LOW_CLASSIFICATION
        observed["task_lock"] = True
        # Simulate the concurrent writer's raise observed by the row lock.
        return _HIGH_CLASSIFICATION

    def locked_agent_registry(**kwargs):
        assert observed["task_lock"] is True
        observed["registry_levels"].append(
            kwargs["admitted_classification_level"]
        )
        assert kwargs["admitted_classification_level"] == _HIGH_CLASSIFICATION
        raise HTTPException(
            status_code=403,
            detail="synthetic higher-level agent ceiling denial",
        )

    closures = []
    monkeypatch.setattr(proxy_api.settings, "ANILA_PILOT_MODE", False)
    monkeypatch.setattr(
        proxy_api, "_verified_proxy_agent_context", lambda *_args: None
    )
    monkeypatch.setattr(proxy_api, "_reject_legacy_agent_dispatch_in_formal", lambda: None)
    monkeypatch.setattr(proxy_api, "_resolve_agent", lambda *_args: agent)
    monkeypatch.setattr(proxy_api, "_agent_policy_level", lambda *_args: Classification.UNCLASSIFIED)
    monkeypatch.setattr(proxy_api, "begin_task_run", lambda *_args, **_kwargs: task_ctx)
    monkeypatch.setattr(proxy_api, "enforce_agent_ceiling", prelock_ceiling)

    async def no_retrieval(*_args, **_kwargs):
        return None

    async def no_memory(*_args, **_kwargs):
        return None

    monkeypatch.setattr(proxy_api, "_prepare_server_retrieval", no_retrieval)
    monkeypatch.setattr(proxy_api, "_inject_memory", no_memory)
    monkeypatch.setattr(
        proxy_service, "lock_task_run_admission", locked_task_run
    )
    monkeypatch.setattr(
        proxy_service, "lock_agent_registry_admission", locked_agent_registry
    )
    monkeypatch.setattr(
        proxy_api,
        "persist_task_call_closure",
        lambda _db, closure: closures.append(closure),
    )

    def forbidden_client(*_args, **_kwargs):
        observed["dispatch"] = True
        raise AssertionError("effective-level denial must precede Agent I/O")

    monkeypatch.setattr(
        legacy_proxy_transport.httpx, "AsyncClient", forbidden_client
    )

    with pytest.raises(HTTPException) as raised:
        await proxy_api._chat_completions_impl(
            RequestStub(), caller=caller, db=SimpleNamespace(), internal_router=False
        )

    assert raised.value.status_code == 403
    assert observed["prelock"] == [_LOW_CLASSIFICATION, _LOW_CLASSIFICATION]
    assert observed["task_lock"] is True
    assert observed["registry_levels"] == [_HIGH_CLASSIFICATION]
    assert observed["dispatch"] is False
    assert closures and closures[-1].classification_level == _HIGH_CLASSIFICATION
