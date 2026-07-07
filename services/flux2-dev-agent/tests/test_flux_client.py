"""flux_client: HTTP client that calls the FLUX backend via the OpenAI
Images API(POST {base}/v1/images/generations)。

基本行為測試;/v1 正規化、aspect→size 對映表、Bearer 等遷移細節見
test_flux_client_openai.py。
"""
from __future__ import annotations

import base64

import httpx
import pytest
import respx

from app.flux_client import FluxBackendError, FluxClient


_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
_B64 = base64.b64encode(_PNG).decode()
# OpenAI Images API contract: {created, data:[{b64_json}]}.
_JSON_BODY = {"created": 1_720_000_000, "data": [{"b64_json": _B64}]}


@pytest.mark.asyncio
@respx.mock
async def test_generate_returns_png_bytes():
    respx.post("http://flux2-dev:8000/v1/images/generations").mock(
        return_value=httpx.Response(200, json=_JSON_BODY)
    )

    async with FluxClient(base_url="http://flux2-dev:8000", timeout=10.0) as client:
        result = await client.generate("a tank in the mountains", aspect_ratio="16:9")

    # Shim decodes the first candidate back to raw PNG bytes.
    assert result == _PNG


@pytest.mark.asyncio
@respx.mock
async def test_generate_sends_correct_body():
    route = respx.post("http://flux2-dev:8000/v1/images/generations").mock(
        return_value=httpx.Response(200, json=_JSON_BODY)
    )

    async with FluxClient(base_url="http://flux2-dev:8000", timeout=10.0) as client:
        await client.generate("hello", aspect_ratio="1:1")

    assert route.called
    body = route.calls.last.request.content
    import json

    parsed = json.loads(body)
    assert parsed == {
        "model": "flux.2-dev",
        "prompt": "hello",
        "n": 1,
        "size": "1024x1024",
        "response_format": "b64_json",
    }


@pytest.mark.asyncio
@respx.mock
async def test_generate_raises_on_non_200():
    respx.post("http://flux2-dev:8000/v1/images/generations").mock(
        return_value=httpx.Response(500, json={"detail": "OOM"})
    )

    async with FluxClient(base_url="http://flux2-dev:8000", timeout=10.0) as client:
        with pytest.raises(FluxBackendError, match="500"):
            await client.generate("anything", aspect_ratio="16:9")


@pytest.mark.asyncio
@respx.mock
async def test_generate_raises_on_empty_images():
    # JSON 200 but empty data → FluxBackendError.
    respx.post("http://flux2-dev:8000/v1/images/generations").mock(
        return_value=httpx.Response(200, json={"created": 1, "data": []})
    )

    async with FluxClient(base_url="http://flux2-dev:8000", timeout=10.0) as client:
        with pytest.raises(FluxBackendError, match="no images"):
            await client.generate("x", aspect_ratio="1:1")
