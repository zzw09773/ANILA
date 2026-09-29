"""同步模型呼叫：讀取逾時不重試，總牆鐘受 deadline 限制。"""
from __future__ import annotations

import dataclasses
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from app.services.proxy.service import ProxyTuning, proxy_request


class _Client:
    timeouts: list[float] = []
    posts = 0
    error: Exception = httpx.ReadTimeout("stalled")

    def __init__(self, *args, timeout=None, **kwargs):
        type(self).timeouts.append(float(timeout))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, headers=None):
        type(self).posts += 1
        raise self.error


def _model() -> SimpleNamespace:
    return SimpleNamespace(
        id=9,
        name="budget-model",
        display_name="budget-model",
        model_type="llm",
        endpoint_url="http://mock-llm:8080/v1",
        api_version="v1",
        api_key_secret_ref=None,
        protocol="openai_compatible",
        max_concurrent=None,
    )


def _tuning(**overrides) -> ProxyTuning:
    return dataclasses.replace(ProxyTuning.from_registry_defaults(), **overrides)


@pytest.fixture(autouse=True)
def _trust_mock(monkeypatch):
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "mock-llm")
    _Client.timeouts = []
    _Client.posts = 0
    monkeypatch.setattr(httpx, "AsyncClient", _Client)


@pytest.mark.asyncio
async def test_read_timeout_is_not_retried():
    _Client.error = httpx.ReadTimeout("stalled")
    with pytest.raises(HTTPException) as exc:
        await proxy_request(
            _model(),
            api_key_id=1,
            user_id=1,
            department_id=None,
            request_body={"messages": []},
            endpoint_path="/chat/completions",
            record_usage=False,
            tuning=_tuning(max_retries=3, retry_base_delay=0),
        )
    assert exc.value.status_code == 502
    assert _Client.posts == 1


@pytest.mark.asyncio
async def test_sync_deadline_clips_a_large_configured_timeout():
    _Client.error = httpx.ConnectError("refused")
    started = time.monotonic()
    deadline = started + 0.2
    with pytest.raises(HTTPException):
        await proxy_request(
            _model(),
            api_key_id=1,
            user_id=1,
            department_id=None,
            request_body={"messages": []},
            endpoint_path="/chat/completions",
            record_usage=False,
            tuning=_tuning(
                llm_timeout=3600,
                max_retries=3,
                retry_base_delay=10,
            ),
            deadline=deadline,
        )
    elapsed = time.monotonic() - started
    assert elapsed < 1
    assert _Client.timeouts
    assert max(_Client.timeouts) < 1
    assert all(item <= 0.25 for item in _Client.timeouts)


class _TricklingClient(_Client):
    """每一段都在 httpx 的逾時內送到，但整個回應拖過 deadline。"""

    async def post(self, url, json=None, headers=None):
        import asyncio

        type(self).posts += 1
        await asyncio.sleep(5)
        raise AssertionError("deadline should have cut this off")


@pytest.mark.asyncio
async def test_sync_deadline_is_a_wall_clock_not_a_per_read_timeout(monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", _TricklingClient)
    _TricklingClient.posts = 0
    started = time.monotonic()
    with pytest.raises(HTTPException) as exc:
        await proxy_request(
            _model(),
            api_key_id=1,
            user_id=1,
            department_id=None,
            request_body={"messages": []},
            endpoint_path="/chat/completions",
            record_usage=False,
            tuning=_tuning(llm_timeout=3600, max_retries=3, retry_base_delay=0),
            deadline=started + 0.3,
        )
    assert time.monotonic() - started < 2
    assert _TricklingClient.posts == 1
    assert exc.value.status_code == 504
    assert "已嘗試 1 次" in exc.value.detail


@pytest.mark.asyncio
async def test_spent_deadline_answers_504_not_502():
    _Client.error = httpx.ConnectError("refused")
    with pytest.raises(HTTPException) as exc:
        await proxy_request(
            _model(),
            api_key_id=1,
            user_id=1,
            department_id=None,
            request_body={"messages": []},
            endpoint_path="/chat/completions",
            record_usage=False,
            tuning=_tuning(max_retries=3, retry_base_delay=0),
            deadline=time.monotonic() - 1,
        )
    assert exc.value.status_code == 504
    assert _Client.posts == 0
