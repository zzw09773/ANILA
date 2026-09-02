"""「主簡報模型」旋鈕（2026-09-02）：studio 執行期向 csp 問 GET /api/models/slides-primary，
拿到就用那顆；沒設（404/409/連不上）退回環境變數 ANILA_STUDIO_SLIDES_MODEL。
接在 ``call_llm_chat`` 上：呼叫端傳的是預設模型名時才替換，明確指定別的模型不動。
"""
from __future__ import annotations

import json

import httpx
import pytest
import respx

from app.config import settings
from app.services import studio_model_primary as smp
from app.services.studio_config import SLIDES_LLM_MODEL
from app.services.studio_llm import call_llm_chat

BASE = settings.CSP_BASE_URL


@pytest.fixture(autouse=True)
def _reset():
    smp._reset_for_tests()
    yield
    smp._reset_for_tests()


def _chat_ok(model):
    return httpx.Response(200, json={"choices": [{"message": {"content": "ok", "role": "assistant"}}], "model": model})


@respx.mock
async def test_primary_from_csp_is_used_when_caller_passes_the_default():
    respx.get(f"{BASE}/api/models/slides-primary").mock(return_value=httpx.Response(200, json={"id": 3, "name": "gemma26-nothink", "endpoint_url": "http://x/v1"}))
    route = respx.post(f"{BASE}/v1/chat/completions").mock(return_value=_chat_ok("gemma26-nothink"))
    assert await smp.get_slides_primary() == "gemma26-nothink"
    await call_llm_chat("t", SLIDES_LLM_MODEL, [{"role": "user", "content": "hi"}])
    assert json.loads(route.calls[0].request.content)["model"] == "gemma26-nothink"


@respx.mock
async def test_explicit_model_name_is_not_replaced():
    respx.get(f"{BASE}/api/models/slides-primary").mock(return_value=httpx.Response(200, json={"id": 3, "name": "gemma26-nothink"}))
    route = respx.post(f"{BASE}/v1/chat/completions").mock(return_value=_chat_ok("other"))
    await call_llm_chat("t", "some-other-model", [{"role": "user", "content": "hi"}])
    assert json.loads(route.calls[0].request.content)["model"] == "some-other-model"


@respx.mock
async def test_falls_back_to_env_default_when_csp_has_none():
    respx.get(f"{BASE}/api/models/slides-primary").mock(return_value=httpx.Response(404, json={"detail": "尚未指定主簡報模型"}))
    route = respx.post(f"{BASE}/v1/chat/completions").mock(return_value=_chat_ok(SLIDES_LLM_MODEL))
    assert await smp.get_slides_primary() is None
    await call_llm_chat("t", SLIDES_LLM_MODEL, [{"role": "user", "content": "hi"}])
    assert json.loads(route.calls[0].request.content)["model"] == SLIDES_LLM_MODEL


@respx.mock
async def test_cached_within_ttl_and_survives_connection_error():
    route = respx.get(f"{BASE}/api/models/slides-primary").mock(return_value=httpx.Response(200, json={"id": 3, "name": "gemma26-nothink"}))
    assert await smp.get_slides_primary() == "gemma26-nothink"
    assert await smp.get_slides_primary() == "gemma26-nothink"
    assert route.call_count == 1
    smp._expire_for_tests()
    route.mock(side_effect=httpx.ConnectError("boom"))
    assert await smp.get_slides_primary() == "gemma26-nothink"  # last good value kept
