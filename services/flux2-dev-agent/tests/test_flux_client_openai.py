"""FluxClient — OpenAI Images API 遷移合約測試。

底層打圖從自訂 ``POST /generate`` 改成標準 OpenAI Images API:

    POST {base}/v1/images/generations
    request : {model, prompt, n, size, response_format:"b64_json"}
              (+ 可選 Authorization: Bearer)
    response: {created, data:[{b64_json}]}

對 CSP 曝露的 /v1/chat/completions 假串流介面不變(平台契約),
只換底層打圖方式。
"""
from __future__ import annotations

import base64
import json

import httpx
import pytest
import respx

from app.flux_client import FluxBackendError, FluxClient

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
_B64 = base64.b64encode(_PNG).decode()
_OPENAI_BODY = {"created": 1_720_000_000, "data": [{"b64_json": _B64}]}


def _client(
    base_url: str = "http://flux-images:8000",
    *,
    model: str = "flux.2-dev",
    api_key: str = "",
) -> FluxClient:
    return FluxClient(base_url=base_url, timeout=10.0, model=model, api_key=api_key)


# ── URL / /v1 正規化 ──────────────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_bare_host_base_gets_v1_appended():
    route = respx.post("http://flux-images:8000/v1/images/generations").mock(
        return_value=httpx.Response(200, json=_OPENAI_BODY)
    )
    async with _client("http://flux-images:8000") as client:
        await client.generate("a tank", aspect_ratio="16:9")
    assert route.called


@pytest.mark.asyncio
@respx.mock
async def test_base_already_with_v1_not_doubled():
    route = respx.post("http://flux-images:8000/v1/images/generations").mock(
        return_value=httpx.Response(200, json=_OPENAI_BODY)
    )
    async with _client("http://flux-images:8000/v1") as client:
        await client.generate("a tank", aspect_ratio="16:9")
    assert route.called
    assert "/v1/v1/" not in str(route.calls.last.request.url)


# ── Request body ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_body_is_standard_openai_images_request():
    route = respx.post("http://flux-images:8000/v1/images/generations").mock(
        return_value=httpx.Response(200, json=_OPENAI_BODY)
    )
    async with _client() as client:
        await client.generate("hello", aspect_ratio="1:1")

    body = json.loads(route.calls.last.request.content)
    assert body == {
        "model": "flux.2-dev",
        "prompt": "hello",
        "n": 1,
        "size": "1024x1024",  # 1:1 對映
        "response_format": "b64_json",
    }
    for banned in ("seed", "num_candidates", "num_inference_steps",
                   "guidance_scale", "aspect_ratio"):
        assert banned not in body


@pytest.mark.asyncio
@respx.mock
async def test_aspect_ratio_to_size_mapping():
    route = respx.post("http://flux-images:8000/v1/images/generations").mock(
        return_value=httpx.Response(200, json=_OPENAI_BODY)
    )
    expected = {
        "1:1": "1024x1024",
        "16:9": "1792x1024",
        "9:16": "1024x1792",
        "4:3": "1216x896",
        "3:4": "896x1216",
        "3:1": "1536x512",
    }
    async with _client() as client:
        for aspect, size in expected.items():
            await client.generate("x", aspect_ratio=aspect)
            body = json.loads(route.calls.last.request.content)
            assert body["size"] == size, aspect


@pytest.mark.asyncio
@respx.mock
async def test_unknown_aspect_ratio_falls_back_to_default_size():
    route = respx.post("http://flux-images:8000/v1/images/generations").mock(
        return_value=httpx.Response(200, json=_OPENAI_BODY)
    )
    async with _client() as client:
        await client.generate("x", aspect_ratio="47:11")
    body = json.loads(route.calls.last.request.content)
    assert body["size"] == "1024x1024"  # fallback 預設


# ── Bearer ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_bearer_sent_when_api_key_set():
    route = respx.post("http://flux-images:8000/v1/images/generations").mock(
        return_value=httpx.Response(200, json=_OPENAI_BODY)
    )
    async with _client(api_key="sk-agent-1") as client:
        await client.generate("x", aspect_ratio="16:9")
    assert route.calls.last.request.headers["authorization"] == "Bearer sk-agent-1"


@pytest.mark.asyncio
@respx.mock
async def test_no_bearer_when_api_key_empty():
    route = respx.post("http://flux-images:8000/v1/images/generations").mock(
        return_value=httpx.Response(200, json=_OPENAI_BODY)
    )
    async with _client(api_key="") as client:
        await client.generate("x", aspect_ratio="16:9")
    assert "authorization" not in route.calls.last.request.headers


# ── Response 解析:下游拿到 PNG bytes(不變)───────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_b64_json_decoded_to_png_bytes():
    respx.post("http://flux-images:8000/v1/images/generations").mock(
        return_value=httpx.Response(200, json=_OPENAI_BODY)
    )
    async with _client() as client:
        result = await client.generate("a tank in the mountains", aspect_ratio="16:9")
    assert result == _PNG


@pytest.mark.asyncio
@respx.mock
async def test_raises_on_non_200():
    respx.post("http://flux-images:8000/v1/images/generations").mock(
        return_value=httpx.Response(500, json={"error": {"message": "OOM"}})
    )
    async with _client() as client:
        with pytest.raises(FluxBackendError, match="500"):
            await client.generate("anything", aspect_ratio="16:9")


@pytest.mark.asyncio
@respx.mock
async def test_raises_on_empty_data():
    respx.post("http://flux-images:8000/v1/images/generations").mock(
        return_value=httpx.Response(200, json={"created": 1, "data": []})
    )
    async with _client() as client:
        with pytest.raises(FluxBackendError):
            await client.generate("x", aspect_ratio="1:1")


@pytest.mark.asyncio
@respx.mock
async def test_raises_on_malformed_base64():
    """b64decode 預設靜默丟棄非法字元（"!!!!" → b""）——必須 validate=True
    才會 raise，否則空 bytes 當 PNG 往下游流（Codex 審查發現）。"""
    respx.post("http://flux-images:8000/v1/images/generations").mock(
        return_value=httpx.Response(200, json={"created": 1, "data": [{"b64_json": "!!!!"}]})
    )
    async with _client() as client:
        with pytest.raises(FluxBackendError, match="malformed b64_json"):
            await client.generate("x", aspect_ratio="1:1")


@pytest.mark.asyncio
@respx.mock
async def test_raises_on_empty_b64_payload():
    """合法但空的 base64（"" → b""）也必須 fail-loud。"""
    respx.post("http://flux-images:8000/v1/images/generations").mock(
        return_value=httpx.Response(200, json={"created": 1, "data": [{"b64_json": ""}]})
    )
    async with _client() as client:
        with pytest.raises(FluxBackendError, match="empty image payload"):
            await client.generate("x", aspect_ratio="1:1")
