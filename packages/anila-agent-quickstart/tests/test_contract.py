"""Vertical-slice contract for the quickstart scaffold.

Imports the real modules under packages/anila-agent-quickstart/.
Does not install anila-core. The canonical verifier is loaded from
anila-core only as the bytes the CSP download copies at release time.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

import agent
import llm
import platform_io
import server
from tests.conftest import (
    CANONICAL,
    ROOT,
    dispatch_claims,
    make_key,
    public_jwk,
    sign_jwt,
)

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


def _token(key, *, agent_id: int = 42, exp: float | None = None) -> str:
    return sign_jwt(key, "kid-1", dispatch_claims(agent_id=agent_id, exp=exp))


def _client(app):
    httpx = pytest.importorskip("httpx")
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://agent")


def _ready_app(key, *, respond=None, llm_client=None):
    settings = platform_io.load_settings(_env())
    cache = platform_io.JwksCache(settings)
    cache.install_for_tests({"kid-1": public_jwk(key, "kid-1")})
    return server.create_app(
        settings,
        jwks=cache,
        respond=respond,
        llm_client=llm_client,
        background=False,
    )


async def test_llm_client_uses_platform_ca_when_llm_ca_is_unset(monkeypatch):
    """CSP /v1 is verified with ANILA_CA_FILE. An unset LLM_CA_FILE must not skip it."""
    seen: list[str | None] = []

    class Dummy:
        async def aclose(self):
            return None

    def spy(*, ca_file):
        seen.append(ca_file)
        return Dummy()

    async def quiet(self, *, force=False):
        return True

    monkeypatch.setattr(server.llm_mod, "make_async_client", spy)
    monkeypatch.setattr(platform_io.JwksCache, "refresh", quiet)
    settings = platform_io.load_settings(_env())
    assert settings.llm_ca_file is None
    app = server.create_app(settings, background=True)
    async with app.router.lifespan_context(app):
        assert seen[0] == settings.ca_file
        assert seen[1] == settings.ca_file


async def test_explicit_llm_ca_file_still_wins(monkeypatch):
    seen: list[str | None] = []

    class Dummy:
        async def aclose(self):
            return None

    def spy(*, ca_file):
        seen.append(ca_file)
        return Dummy()

    async def quiet(self, *, force=False):
        return True

    monkeypatch.setattr(server.llm_mod, "make_async_client", spy)
    monkeypatch.setattr(platform_io.JwksCache, "refresh", quiet)
    settings = platform_io.load_settings(_env(LLM_CA_FILE="/other/model-ca.pem"))
    app = server.create_app(settings, background=True)
    async with app.router.lifespan_context(app):
        assert seen[0] == "/other/model-ca.pem"
        assert seen[1] == settings.ca_file


async def test_scaffold_does_not_vendor_verifier():
    assert not (ROOT / "anila_verify.py").exists()
    assert CANONICAL.is_file()


async def test_unregistered_health_is_not_registered():
    settings = platform_io.load_settings(_env(ANILA_AGENT_ID=""))
    assert settings.agent_id is None
    assert settings.readiness_reason() == "not_registered"
    app = server.create_app(settings, background=False)
    async with _client(app) as client:
        health = await client.get("/health")
    body = health.json()
    assert health.status_code == 503
    assert body["status"] == "not_registered"
    assert body["reason"] == "not_registered"
    assert "ANILA_AGENT_ID" in body["hint"]


async def test_missing_llm_key_is_llm_not_configured():
    settings = platform_io.load_settings(_env(LLM_API_KEY=""))
    assert settings.readiness_reason() == "llm_not_configured"
    app = server.create_app(settings, background=False)
    async with _client(app) as client:
        health = await client.get("/health")
        chat = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer not-a-jwt"},
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
    body = health.json()
    assert health.status_code == 503
    assert body["status"] == "llm_not_configured"
    assert body["reason"] == "llm_not_configured"
    assert "LLM_API_KEY" in body["hint"]
    assert chat.status_code == 503
    assert chat.json()["error"]["code"] == "llm_not_configured"


async def test_missing_llm_model_is_llm_not_configured():
    settings = platform_io.load_settings(_env(LLM_MODEL=""))
    assert "LLM_MODEL" in settings.gaps
    assert settings.readiness_reason() == "llm_not_configured"
    app = server.create_app(settings, background=False)
    async with _client(app) as client:
        health = await client.get("/health")
    assert health.status_code == 503
    assert health.json()["reason"] == "llm_not_configured"
    assert "LLM_MODEL" in health.json()["hint"]


async def test_llm_gap_is_reported_before_registration():
    settings = platform_io.load_settings(_env(ANILA_AGENT_ID="", LLM_API_KEY=""))
    assert settings.readiness_reason() == "llm_not_configured"


async def test_health_ready_only_with_jwks():
    key = make_key()
    app = _ready_app(key)
    async with _client(app) as client:
        health = await client.get("/health")
        assert health.status_code == 200
        assert health.json() == {"status": "ok", "model": agent.AGENT_NAME, "rag": False}
        missing = await client.get("/v1/models")
        assert missing.status_code == 401
        assert missing.headers["www-authenticate"].lower().startswith("bearer")
        ok = await client.get(
            "/v1/models",
            headers={"Authorization": f"Bearer {_token(key)}"},
        )
    assert ok.status_code == 200
    data = ok.json()
    assert data["object"] == "list"
    assert data["data"][0]["id"] == agent.AGENT_NAME
    assert data["data"][0]["object"] == "model"


async def test_wrong_agent_id_is_403_before_llm():
    key = make_key()
    seen = {"called": False}

    async def boom(*_a, **_k):
        seen["called"] = True
        yield "no"

    app = _ready_app(key, respond=boom)
    async with _client(app) as client:
        denied = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {_token(key, agent_id=99)}"},
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "agent_mismatch"
    assert seen["called"] is False


class FakeLLM:
    outbound_auth = "Bearer outbound-only"

    def __init__(self):
        self.calls = []
        self.result = llm.LlmResult()

    def bind(self, deadline: float):
        self.deadline = deadline
        self.result.finish_reason = "stop"
        self.result.usage = {
            "prompt_tokens": 3,
            "completion_tokens": 2,
            "total_tokens": 5,
        }
        self.result.saw_text = False
        return self

    async def complete(self, messages, *, instructions, context):
        self.calls.append(
            {
                "messages": messages,
                "instructions": instructions,
                "context": context,
                "auth": self.outbound_auth,
            }
        )

        async def chunks():
            yield "一、"
            yield "議程"

        self.result.saw_text = True
        return llm.LlmStream(chunks(), result=self.result)


async def test_nonstream_and_stream_round_trip():
    key = make_key()

    async def respond(messages, context, llm_call):
        assert messages[-1]["content"] == "院內會議"
        assert context == ""
        async for piece in llm_call(messages, instructions="三個重點", context=context):
            yield piece

    fake = FakeLLM()
    app = _ready_app(key, respond=respond, llm_client=fake)
    headers = {"Authorization": f"Bearer {_token(key)}"}
    async with _client(app) as client:
        plain = await client.post(
            "/v1/chat/completions",
            headers=headers,
            json={"model": "ignored", "messages": [{"role": "user", "content": "院內會議"}]},
        )
        streamed = await client.post(
            "/v1/chat/completions",
            headers=headers,
            json={"messages": [{"role": "user", "content": "院內會議"}], "stream": True},
        )
        body = streamed.text
    assert plain.status_code == 200
    payload = plain.json()
    assert payload["object"] == "chat.completion"
    assert payload["model"] == agent.AGENT_NAME
    assert payload["choices"][0]["message"]["content"] == "一、議程"
    assert payload["choices"][0]["finish_reason"] == "stop"
    assert payload["usage"]["total_tokens"] == 5
    assert streamed.headers["content-type"].startswith("text/event-stream")
    frames = [line for line in body.split("\n\n") if line.startswith("data: ")]
    first = json.loads(frames[0].removeprefix("data: "))
    assert first["choices"][0]["delta"]["role"] == "assistant"
    contents = [
        json.loads(frame.removeprefix("data: "))["choices"][0]["delta"]["content"]
        for frame in frames[1:-2]
    ]
    assert "".join(contents) == "一、議程"
    finish = json.loads(frames[-2].removeprefix("data: "))
    assert finish["choices"][0]["finish_reason"] == "stop"
    assert frames[-1] == "data: [DONE]"
    assert body.count("data: [DONE]") == 1
    assert fake.calls[0]["auth"] == "Bearer outbound-only"


async def test_upstream_error_frame_is_not_a_successful_stop():
    key = make_key()

    async def respond(messages, context, llm_call):
        async for piece in llm_call(messages, instructions="", context=context):
            yield piece

    class Broken:
        outbound_auth = None

        def bind(self, deadline: float):
            return self

        async def complete(self, messages, *, instructions, context):
            async def chunks():
                yield "半句"
                raise llm.UpstreamError("upstream broke")

            return llm.LlmStream(chunks(), finish_reason="stop")

    app = _ready_app(key, respond=respond, llm_client=Broken())
    async with _client(app) as client:
        streamed = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {_token(key)}"},
            json={"messages": [{"role": "user", "content": "x"}], "stream": True},
        )
    compact = streamed.text.replace(" ", "")
    assert 'finish_reason":"stop"' not in compact
    assert '"error"' in streamed.text
    assert streamed.text.rstrip().endswith("data: [DONE]")


async def test_parse_openai_sse_fragments():
    raw = (
        'data: {"choices":[{"delta":{"content":"你"}}]}\r\n\r\n'
        ": keep-alive\n\n"
        'data: {"choices":[{"delta":{},"finish_reason":"length"}]}\n\n'
        "data: [DONE]\n\n"
    )
    events = list(llm.iter_sse_bytes([raw[:20].encode(), raw[20:].encode()]))
    assert events[0]["choices"][0]["delta"]["content"] == "你"
    assert events[1]["choices"][0]["finish_reason"] == "length"
    assert events[-1] == "[DONE]"


async def test_expired_token_and_non_text_are_rejected():
    key = make_key()
    app = _ready_app(key)
    headers = {"Authorization": f"Bearer {_token(key, exp=time.time() - 10)}"}
    async with _client(app) as client:
        expired = await client.get("/v1/models", headers=headers)
        image = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {_token(key)}"},
            json={
                "messages": [
                    {
                        "role": "user",
                        "content": [{"type": "image_url", "image_url": {"url": "http://x"}}],
                    }
                ]
            },
        )
    assert expired.status_code == 401
    assert image.status_code == 400
    assert image.json()["error"]["code"] == "invalid_request"


async def test_cached_next_key_is_not_accepted_for_dispatch():
    import anila_verify

    key = make_key()
    jwk = public_jwk(key, "next-kid")
    jwk["anila_key_state"] = "next"
    jwk["anila_accept_missing_iat"] = False
    settings = platform_io.load_settings(_env())
    cache = platform_io.JwksCache(settings)
    cache._keys = anila_verify.parse_jwks({"keys": [jwk]})
    cache._fetched_at = time.monotonic()
    token = sign_jwt(key, "next-kid", dispatch_claims())
    with pytest.raises(anila_verify.AnilaVerifyError):
        await cache.verify(f"Bearer {token}")


async def test_unknown_kid_force_refresh_backs_off_after_failure():
    settings = platform_io.load_settings(_env())
    cache = platform_io.JwksCache(settings)
    calls = {"n": 0}

    def _boom(self):
        calls["n"] += 1
        raise RuntimeError("jwks down")

    cache._fetch = _boom.__get__(cache, platform_io.JwksCache)
    assert await cache.refresh(force=True) is False
    assert await cache.refresh(force=True) is False
    assert calls["n"] == 1


async def test_cold_jwks_failure_stays_unavailable(monkeypatch):
    calls = {"n": 0}

    def broken(*_a, **_k):
        calls["n"] += 1
        raise RuntimeError("no network")

    monkeypatch.setattr(platform_io.JwksCache, "_fetch", broken)
    settings = platform_io.load_settings(_env())
    app = server.create_app(settings, background=False)
    async with _client(app) as client:
        health = await client.get("/health")
        after_health = calls["n"]
        chat = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer not-a-jwt"},
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
    assert after_health >= 1
    assert calls["n"] > after_health
    assert health.status_code == 503
    assert health.json()["status"] == "jwks_unavailable"
    assert chat.status_code == 503
    assert chat.json()["error"]["code"] == "jwks_unavailable"
    lock = Path(ROOT / "requirements.lock").read_text(encoding="utf-8")
    assert "fastapi==0.136.1" not in lock
    assert "--hash=sha256:" in lock
    assert "未驗證" not in lock
