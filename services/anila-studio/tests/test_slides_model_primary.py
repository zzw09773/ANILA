"""簡報／視覺角色：向 CSP 問 GET /api/models/roles/{role}。

沒設或已停用就失敗，訊息點名角色，不退回環境變數、也不把哨兵送去上游。
明確指定別的模型名稱時不替換。
"""
from __future__ import annotations

import json

import httpx
import pytest
import respx
from fastapi import HTTPException

pytestmark = pytest.mark.real_model_roles

from app.config import settings
from app.services import studio_model_primary as smp
from app.services.studio_config import SLIDES_LLM_MODEL, VISION_LLM_MODEL
from app.services.studio_llm import call_llm_chat

BASE = settings.CSP_BASE_URL


@pytest.fixture(autouse=True)
def _reset():
    smp._reset_for_tests()
    yield
    smp._reset_for_tests()


def _chat_ok(model):
    return httpx.Response(
        200,
        json={"choices": [{"message": {"content": "ok", "role": "assistant"}}], "model": model},
    )


def _role(name="gemma26-nothink"):
    return httpx.Response(200, json={"id": 3, "name": name, "endpoint_url": "http://x/v1"})


@respx.mock
async def test_slides_role_is_used_when_caller_passes_the_sentinel():
    respx.get(f"{BASE}/api/models/roles/slides").mock(return_value=_role())
    route = respx.post(f"{BASE}/v1/chat/completions").mock(return_value=_chat_ok("gemma26-nothink"))
    assert await smp.require_role_model("slides") == "gemma26-nothink"
    await call_llm_chat("t", SLIDES_LLM_MODEL, [{"role": "user", "content": "hi"}])
    assert json.loads(route.calls[0].request.content)["model"] == "gemma26-nothink"
    assert SLIDES_LLM_MODEL not in route.calls[0].request.content.decode()


@respx.mock
async def test_vision_role_is_separate_from_slides():
    respx.get(f"{BASE}/api/models/roles/vision").mock(return_value=_role("vision-llm"))
    route = respx.post(f"{BASE}/v1/chat/completions").mock(return_value=_chat_ok("vision-llm"))
    await call_llm_chat("t", VISION_LLM_MODEL, [{"role": "user", "content": "hi"}])
    assert json.loads(route.calls[0].request.content)["model"] == "vision-llm"


@respx.mock
async def test_explicit_model_name_is_not_replaced():
    route = respx.post(f"{BASE}/v1/chat/completions").mock(return_value=_chat_ok("other"))
    await call_llm_chat("t", "some-other-model", [{"role": "user", "content": "hi"}])
    assert json.loads(route.calls[0].request.content)["model"] == "some-other-model"


@respx.mock
async def test_unset_role_fails_with_the_csp_message_and_does_not_call_the_model():
    respx.get(f"{BASE}/api/models/roles/slides").mock(
        return_value=httpx.Response(404, json={"detail": "簡報模型尚未在治理中心設定"})
    )
    route = respx.post(f"{BASE}/v1/chat/completions").mock(return_value=_chat_ok("x"))
    with pytest.raises(HTTPException) as exc:
        await call_llm_chat("t", SLIDES_LLM_MODEL, [{"role": "user", "content": "hi"}])
    assert exc.value.status_code == 409
    assert exc.value.detail == "簡報模型尚未在治理中心設定"
    assert route.call_count == 0


@respx.mock
async def test_cached_within_ttl_and_survives_connection_error():
    route = respx.get(f"{BASE}/api/models/roles/slides").mock(return_value=_role())
    assert await smp.require_role_model("slides") == "gemma26-nothink"
    assert await smp.require_role_model("slides") == "gemma26-nothink"
    assert route.call_count == 1
    smp._expire_for_tests()
    route.mock(side_effect=httpx.ConnectError("boom"))
    assert await smp.require_role_model("slides") == "gemma26-nothink"


@respx.mock
async def test_connection_failure_retries_before_the_success_ttl(monkeypatch):
    """A down CSP must not pin a 503 for the whole success cache.

    With no model yet, a blip used to stamp the 60s clock. Recovery inside
    that window never reached CSP. Backoff is only long enough to avoid a
    hammer; it is not the success TTL.
    """
    clock = {"now": 1_700_000_000.0}
    monkeypatch.setattr(smp.time, "time", lambda: clock["now"])
    route = respx.get(f"{BASE}/api/models/roles/slides").mock(
        side_effect=httpx.ConnectError("down")
    )
    with pytest.raises(HTTPException) as exc:
        await smp.require_role_model("slides")
    assert exc.value.status_code == 503
    assert route.call_count == 1

    clock["now"] += 1
    with pytest.raises(HTTPException):
        await smp.require_role_model("slides")
    assert route.call_count == 1

    route.mock(return_value=_role("back-online"))
    clock["now"] += 10
    assert await smp.require_role_model("slides") == "back-online"
    assert route.call_count == 2


@respx.mock
async def test_upstream_http_error_retries_before_the_success_ttl(monkeypatch):
    clock = {"now": 1_700_000_000.0}
    monkeypatch.setattr(smp.time, "time", lambda: clock["now"])
    route = respx.get(f"{BASE}/api/models/roles/vision").mock(
        return_value=httpx.Response(503, text="unavailable")
    )
    with pytest.raises(HTTPException) as exc:
        await smp.require_role_model("vision")
    assert exc.value.status_code == 503

    route.mock(return_value=_role("vision-back"))
    clock["now"] += 10
    assert await smp.require_role_model("vision") == "vision-back"


@respx.mock
async def test_refresh_failures_do_not_keep_a_stale_role_forever(monkeypatch):
    """Failures may reuse the last success, but they must not move its clock.

    Ten minutes of repeated refresh failures is past any bounded grace.
    """
    clock = {"now": 1_700_000_000.0}
    monkeypatch.setattr(smp.time, "time", lambda: clock["now"])
    route = respx.get(f"{BASE}/api/models/roles/slides").mock(return_value=_role("old-llm"))
    assert await smp.require_role_model("slides") == "old-llm"
    success_at = clock["now"]

    route.mock(side_effect=httpx.ConnectError("down"))
    clock["now"] = success_at + 90
    assert await smp.require_role_model("slides") == "old-llm"
    clock["now"] = success_at + 120
    assert await smp.require_role_model("slides") == "old-llm"

    clock["now"] = success_at + 600
    with pytest.raises(HTTPException) as exc:
        await smp.require_role_model("slides")
    assert exc.value.status_code == 503


@respx.mock
async def test_studio_calls_are_tagged_as_studio_for_usage_accounting():
    respx.get(f"{BASE}/api/models/roles/slides").mock(return_value=_role())
    route = respx.post(f"{BASE}/v1/chat/completions").mock(return_value=_chat_ok("gemma26-nothink"))
    await call_llm_chat("t", SLIDES_LLM_MODEL, [{"role": "user", "content": "hi"}])
    assert route.calls[0].request.headers.get("X-ANILA-Request-Source") == "studio"
