"""Circuit breaker: refuse models explicitly marked unhealthy by health_checker.

Acceptance: unhealthy → 503 before any upstream HTTP; unknown / skipped /
never-probed / flag-off / recovered-healthy still forward past the breaker.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.config import settings
from app.services import proxy_service
from app.services.health_checker import is_explicitly_unhealthy
from app.services.proxy.service import _proxy_stream_impl, _reject_unhealthy_model
from app.services.proxy_service import proxy_request, proxy_stream


def _model(*, name: str = "glm-5.2", health_status: str | None = "unknown"):
    return SimpleNamespace(
        id=42,
        name=name,
        model_type="llm",
        api_version="v1",
        endpoint_url="http://mock-llm:8080",
        health_status=health_status,
        api_key_secret_ref=None,
    )


def _detail_text(detail) -> str:
    if isinstance(detail, dict):
        return str(detail.get("message", detail))
    return str(detail)


@pytest.fixture(autouse=True)
def _allow_mock_upstream(monkeypatch):
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "mock-llm")
    monkeypatch.setattr(settings, "ANILA_REJECT_UNHEALTHY_MODELS", True)


class _BoomClient:
    """httpx.AsyncClient stand-in that proves no upstream I/O occurred."""

    constructed = 0
    posts = 0
    streams = 0

    def __init__(self, *args, **kwargs):
        type(self).constructed += 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, *args, **kwargs):
        type(self).posts += 1
        raise AssertionError("upstream HTTP must not run on 503 circuit-breaker path")

    def stream(self, *args, **kwargs):
        type(self).streams += 1
        raise AssertionError("upstream stream must not run on 503 circuit-breaker path")


class _OkClient:
    """Minimal successful upstream for allow-through cases."""

    constructed = 0

    def __init__(self, *args, **kwargs):
        type(self).constructed += 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, *args, **kwargs):
        return SimpleNamespace(
            status_code=200,
            headers={"content-type": "application/json"},
            text=(
                '{"choices":[{"message":{"role":"assistant","content":"ok"}}],'
                '"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}'
            ),
            json=lambda: {
                "choices": [
                    {"message": {"role": "assistant", "content": "ok"}}
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            },
        )


def _patch_boom(monkeypatch):
    _BoomClient.constructed = 0
    _BoomClient.posts = 0
    _BoomClient.streams = 0
    monkeypatch.setattr(proxy_service.httpx, "AsyncClient", _BoomClient)


def _patch_ok(monkeypatch):
    _OkClient.constructed = 0
    monkeypatch.setattr(proxy_service.httpx, "AsyncClient", _OkClient)


async def _proxy_ok(model):
    return await proxy_request(
        model=model,
        api_key_id=1,
        user_id=1,
        department_id=None,
        request_body={"messages": [{"role": "user", "content": "hi"}]},
        endpoint_path="/v1/chat/completions",
        suppress_usage_accounting=True,
    )


@pytest.mark.parametrize(
    "raw, expect",
    [
        ("unhealthy", True),
        ("offline", True),
        ("unknown", False),
        (None, False),
        ("healthy", False),
        ("degraded", False),
        ("connecting", False),
    ],
)
def test_is_explicitly_unhealthy_predicate(raw, expect):
    assert is_explicitly_unhealthy(raw) is expect


def test_reject_helper_detail_has_model_and_probe_words_no_endpoint():
    with pytest.raises(HTTPException) as exc:
        _reject_unhealthy_model(
            name="z-ai/glm-5.2",
            health_status="unhealthy",
            health_status_source="unit_test",
        )
    assert exc.value.status_code == 503
    detail = exc.value.detail
    assert isinstance(detail, dict)
    assert detail["code"] == "model_unhealthy"
    message = detail["message"]
    assert "z-ai/glm-5.2" in message
    assert "health probe" in message
    assert "unhealthy" in message
    assert "admin" in message
    assert "模型健康" in message
    assert "http" not in message
    assert "mock-llm" not in message
    assert "8080" not in message


def test_proxy_request_unhealthy_returns_503_without_upstream_http(monkeypatch):
    _patch_boom(monkeypatch)
    model = _model(health_status="unhealthy")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            proxy_request(
                model=model,
                api_key_id=1,
                user_id=1,
                department_id=None,
                request_body={"messages": [{"role": "user", "content": "hi"}]},
                endpoint_path="/v1/chat/completions",
            )
        )
    assert exc.value.status_code == 503
    assert isinstance(exc.value.detail, dict)
    assert exc.value.detail["code"] == "model_unhealthy"
    text = _detail_text(exc.value.detail)
    assert "health probe" in text
    assert "unhealthy" in text
    assert model.name in text
    assert "http://mock-llm:8080" not in text
    assert _BoomClient.constructed == 0
    assert _BoomClient.posts == 0


@pytest.mark.parametrize("status", ["unknown", None])
def test_proxy_request_unknown_or_never_probed_forwards(monkeypatch, status):
    _patch_ok(monkeypatch)
    model = _model(health_status=status)
    result = asyncio.run(_proxy_ok(model))
    assert _OkClient.constructed >= 1
    assert result["choices"][0]["message"]["content"] == "ok"


def test_proxy_request_skipped_probe_status_forwards(monkeypatch):
    """Skipped probes leave status unknown — must not trip the breaker."""
    _patch_ok(monkeypatch)
    model = _model(health_status="unknown")
    model.health_checked_at = None
    asyncio.run(_proxy_ok(model))
    assert _OkClient.constructed >= 1


def test_proxy_request_flag_false_allows_unhealthy(monkeypatch):
    monkeypatch.setattr(settings, "ANILA_REJECT_UNHEALTHY_MODELS", False)
    _patch_ok(monkeypatch)
    model = _model(health_status="unhealthy")
    result = asyncio.run(_proxy_ok(model))
    assert result["choices"][0]["message"]["content"] == "ok"
    assert _OkClient.constructed >= 1


def test_proxy_request_recovered_healthy_forwards(monkeypatch):
    _patch_ok(monkeypatch)
    model = _model(health_status="healthy")
    result = asyncio.run(_proxy_ok(model))
    assert result["choices"][0]["message"]["content"] == "ok"
    assert _OkClient.constructed >= 1


def test_proxy_stream_unhealthy_returns_503_without_upstream_http(monkeypatch):
    _patch_boom(monkeypatch)

    async def _drive():
        agen = _proxy_stream_impl(
            target_url="http://mock-llm:8080/v1/chat/completions",
            api_key_id=1,
            user_id=1,
            department_id=None,
            usage_model_id=42,
            request_body={"messages": [{"role": "user", "content": "hi"}]},
            model_name="glm-5.2",
            model_health_status="unhealthy",
            suppress_usage_accounting=True,
        )
        async for _ in agen:
            pass

    with pytest.raises(HTTPException) as exc:
        asyncio.run(_drive())
    assert exc.value.status_code == 503
    assert isinstance(exc.value.detail, dict)
    assert exc.value.detail["code"] == "model_unhealthy"
    text = _detail_text(exc.value.detail)
    assert "health probe" in text
    assert "glm-5.2" in text
    assert "http://mock-llm:8080" not in text
    assert _BoomClient.constructed == 0
    assert _BoomClient.streams == 0


def test_proxy_stream_public_governance_db_none_unhealthy_no_upstream(monkeypatch):
    """H2: legacy stream via proxy_stream (not _proxy_stream_impl) must trip.

    When governance_db is None the locked-registry path is unavailable; health
    must come from the caller-supplied model_health_status fallback.
    """
    _patch_boom(monkeypatch)

    async def _drive():
        agen = proxy_stream(
            target_url="http://mock-llm:8080/v1/chat/completions",
            api_key_id=1,
            user_id=1,
            department_id=None,
            usage_model_id=42,
            request_body={"messages": [{"role": "user", "content": "hi"}]},
            model_name="glm-5.2",
            model_health_status="unhealthy",
            governance_db=None,
            suppress_usage_accounting=True,
        )
        async for _ in agen:
            pass

    with pytest.raises(HTTPException) as exc:
        asyncio.run(_drive())
    assert exc.value.status_code == 503
    assert isinstance(exc.value.detail, dict)
    assert exc.value.detail["code"] == "model_unhealthy"
    assert "glm-5.2" in _detail_text(exc.value.detail)
    assert _BoomClient.constructed == 0
    assert _BoomClient.posts == 0
    assert _BoomClient.streams == 0


def test_proxy_stream_unknown_forwards_past_breaker(monkeypatch):
    """unknown health reaches AsyncClient construction (past the breaker)."""
    _patch_boom(monkeypatch)

    async def _drive():
        agen = _proxy_stream_impl(
            target_url="http://mock-llm:8080/v1/chat/completions",
            api_key_id=1,
            user_id=1,
            department_id=None,
            usage_model_id=42,
            request_body={"messages": [{"role": "user", "content": "hi"}]},
            model_name="glm-5.2",
            model_health_status="unknown",
            suppress_usage_accounting=True,
        )
        async for _ in agen:
            pass

    with pytest.raises(AssertionError, match="upstream stream must not run"):
        asyncio.run(_drive())
    assert _BoomClient.constructed >= 1
    assert _BoomClient.streams >= 1
