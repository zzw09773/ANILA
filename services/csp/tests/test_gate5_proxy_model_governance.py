from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

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
