"""Optional model /tokenize client — fail closed, URL-guarded."""

from __future__ import annotations

import httpx
import pytest
import respx

from anila_core.api.router_server import count_routing_prompt_tokens
from anila_core.http_pool import reset_http_client
from anila_core.text.model_tokenize import probe_tokenize_url, tokenize_prompt


@pytest.mark.asyncio
async def test_tokenize_prompt_reads_count() -> None:
    url = "https://llm.example.com/tokenize"

    def handler(request: httpx.Request) -> httpx.Response:
        assert b"hello" in request.content
        return httpx.Response(200, json={"count": 3})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        assert await tokenize_prompt(client, url, "hello") == 3


@pytest.mark.asyncio
async def test_tokenize_prompt_falls_back_to_inputs_body() -> None:
    url = "https://llm.example.com/tokenize"
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content)
        seen.append(body)
        if "prompt" in body:
            return httpx.Response(404, json={"detail": "no"})
        return httpx.Response(200, json={"tokens": [1, 2, 3, 4]})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        assert await tokenize_prompt(client, url, "hi") == 4
    assert seen[0] == {"prompt": "hi"}
    assert seen[1] == {"inputs": "hi"}


@pytest.mark.asyncio
async def test_tokenize_rejects_loopback() -> None:
    async with httpx.AsyncClient() as client:
        assert await tokenize_prompt(client, "http://127.0.0.1:8000/tokenize", "x") is None


@pytest.mark.asyncio
async def test_probe_picks_first_working_url(monkeypatch) -> None:
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "glm")

    @respx.mock
    async def _run() -> None:
        respx.post("http://glm:8000/tokenize").mock(
            return_value=httpx.Response(200, json={"count": 1})
        )
        async with httpx.AsyncClient() as client:
            assert await probe_tokenize_url(client, "http://glm:8000/v1") == (
                "http://glm:8000/tokenize"
            )

    await _run()


@pytest.mark.asyncio
async def test_count_routing_uses_heuristic_without_env(monkeypatch) -> None:
    monkeypatch.delenv("ANILA_TOKENIZE_URL", raising=False)
    tokens, source = await count_routing_prompt_tokens(
        [{"role": "user", "content": "中" * 12}]
    )
    assert source == "heuristic"
    assert tokens == 12


@pytest.mark.asyncio
async def test_count_routing_uses_model_when_env_set(monkeypatch) -> None:
    monkeypatch.setenv("ANILA_TOKENIZE_URL", "https://llm.example.com/tokenize")

    @respx.mock
    async def _run() -> None:
        reset_http_client()
        respx.post("https://llm.example.com/tokenize").mock(
            return_value=httpx.Response(200, json={"count": 99})
        )
        tokens, source = await count_routing_prompt_tokens(
            [{"role": "user", "content": "中文"}]
        )
        assert source == "model"
        assert tokens == 99

    await _run()
