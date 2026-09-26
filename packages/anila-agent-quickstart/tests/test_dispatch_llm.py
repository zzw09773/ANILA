"""派工時用請求上的 JWT 呼叫 CSP，lab 沒有派工 JWT 才用開發者金鑰。"""

from __future__ import annotations

from pathlib import Path

import pytest

import llm
import platform_io
import server
from tests.conftest import ROOT, dispatch_claims, make_key, public_jwk, sign_jwt

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _env(**overrides: str) -> dict[str, str]:
    base = {
        "CSP_BASE_URL": "https://csp.example",
        "ANILA_CA_FILE": "/app/ca.pem",
        "ANILA_AGENT_ID": "42",
        "LLM_BASE_URL": "https://llm.example/v1",
        "LLM_MODEL": "dept-model",
        "LLM_AUTH_REQUIRED": "true",
        "LLM_API_KEY": "outbound-only",
    }
    base.update(overrides)
    return base


class _Response:
    status_code = 200

    async def aiter_bytes(self):
        payload = (
            'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\n'
            "data: [DONE]\n\n"
        )
        yield payload.encode()

    async def aclose(self):
        return None


class RecordingHttp:
    def __init__(self) -> None:
        self.built: list[dict] = []

    def build_request(self, method, url, headers=None, json=None):
        request = {"method": method, "url": url, "headers": dict(headers or {}), "json": json}
        self.built.append(request)
        return request

    async def send(self, request, stream=True):
        return _Response()


def _app(key, http):
    settings = platform_io.load_settings(_env())
    cache = platform_io.JwksCache(settings)
    cache.install_for_tests({"kid-1": public_jwk(key, "kid-1")})
    client = llm.LlmClient(settings, http)
    return server.create_app(settings, jwks=cache, llm_client=client, background=False)


def _asgi():
    httpx = pytest.importorskip("httpx")
    return httpx


async def test_dispatched_request_uses_dispatch_jwt_and_registered_model():
    key = make_key()
    http = RecordingHttp()
    app = _app(key, http)
    token = sign_jwt(key, "kid-1", dispatch_claims(agent_id=42))
    httpx = _asgi()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://agent"
    ) as client:
        resp = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "model": "not-the-registered-model",
                "messages": [{"role": "user", "content": "院內會議"}],
            },
        )
    assert resp.status_code == 200, resp.text
    assert http.built, "應該打到 LLM_BASE_URL"
    sent = http.built[0]
    assert sent["url"] == "https://llm.example/v1/chat/completions"
    assert sent["headers"]["Authorization"] == f"Bearer {token}"
    assert "outbound-only" not in sent["headers"]["Authorization"]
    assert sent["json"]["model"] == "dept-model"


async def test_overlapping_dispatches_keep_their_own_jwt():
    """兩個並行派工在打模型前交會時，各自的 JWT 不能被對方蓋掉。"""
    import asyncio

    key = make_key()
    http = RecordingHttp()
    settings = platform_io.load_settings(_env())
    cache = platform_io.JwksCache(settings)
    cache.install_for_tests({"kid-1": public_jwk(key, "kid-1")})
    shared = llm.LlmClient(settings, http)
    release = asyncio.Event()
    arrived = 0

    async def respond(messages, context, llm_call):
        nonlocal arrived
        arrived += 1
        if arrived < 2:
            await release.wait()
        else:
            release.set()
        async for piece in llm_call(messages, instructions="同時", context=context):
            yield piece

    app = server.create_app(
        settings, jwks=cache, respond=respond, llm_client=shared, background=False
    )
    token_a = sign_jwt(key, "kid-1", dispatch_claims(agent_id=42, user_id=1))
    token_b = sign_jwt(key, "kid-1", dispatch_claims(agent_id=42, user_id=2))
    httpx = _asgi()

    async def once(token: str):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://agent"
        ) as client:
            resp = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": f"Bearer {token}"},
                json={"messages": [{"role": "user", "content": "同時"}]},
            )
        assert resp.status_code == 200, resp.text

    await asyncio.gather(once(token_a), once(token_b))
    sent = [item["headers"]["Authorization"] for item in http.built]
    assert sorted(sent) == sorted([f"Bearer {token_a}", f"Bearer {token_b}"])


async def test_lab_call_without_dispatch_jwt_uses_developer_key():
    settings = platform_io.load_settings(_env())
    http = RecordingHttp()
    client = llm.LlmClient(settings, http)
    stream = await client.bind(0).complete(
        [{"role": "user", "content": "hi"}],
        instructions="回答",
        context="",
    )
    async for _piece in stream.chunks:
        pass
    assert http.built[0]["headers"]["Authorization"] == "Bearer outbound-only"
    assert http.built[0]["json"]["model"] == "dept-model"


def test_readme_bills_the_asker_and_keeps_the_key_for_lab():
    text = (Path(ROOT) / "README.md").read_text(encoding="utf-8")
    assert "提問者" in text
    assert "測試" in text
    assert "LLM_API_KEY" in text
