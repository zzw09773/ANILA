"""flux_client: HTTP client that calls flux2-dev /generate."""
from __future__ import annotations

import base64

import httpx
import pytest
import respx

from app.flux_client import FluxBackendError, FluxClient


_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
_B64 = base64.b64encode(_PNG).decode()
# Stage 1 contract (spec 3.2): /generate returns JSON {images:[b64], seed, meta}.
_JSON_BODY = {"images": [_B64], "seed": 42, "meta": {"steps": 28, "guidance": 4.0}}


@pytest.mark.asyncio
@respx.mock
async def test_generate_returns_png_bytes():
    respx.post("http://flux2-dev:8000/generate").mock(
        return_value=httpx.Response(200, json=_JSON_BODY)
    )

    async with FluxClient(base_url="http://flux2-dev:8000", timeout=10.0) as client:
        result = await client.generate("a tank in the mountains", aspect_ratio="16:9")

    # Shim decodes the first candidate back to raw PNG bytes.
    assert result == _PNG


@pytest.mark.asyncio
@respx.mock
async def test_generate_sends_correct_body():
    route = respx.post("http://flux2-dev:8000/generate").mock(
        return_value=httpx.Response(200, json=_JSON_BODY)
    )

    async with FluxClient(base_url="http://flux2-dev:8000", timeout=10.0) as client:
        await client.generate("hello", aspect_ratio="1:1")

    assert route.called
    body = route.calls.last.request.content
    import json

    parsed = json.loads(body)
    assert parsed == {"prompt": "hello", "aspect_ratio": "1:1"}


@pytest.mark.asyncio
@respx.mock
async def test_generate_raises_on_non_200():
    respx.post("http://flux2-dev:8000/generate").mock(
        return_value=httpx.Response(500, json={"detail": "OOM"})
    )

    async with FluxClient(base_url="http://flux2-dev:8000", timeout=10.0) as client:
        with pytest.raises(FluxBackendError, match="500"):
            await client.generate("anything", aspect_ratio="16:9")


@pytest.mark.asyncio
@respx.mock
async def test_generate_raises_on_empty_images():
    # JSON 200 but no images → FluxBackendError (Stage 1 contract guard).
    respx.post("http://flux2-dev:8000/generate").mock(
        return_value=httpx.Response(200, json={"images": [], "seed": 1, "meta": {}})
    )

    async with FluxClient(base_url="http://flux2-dev:8000", timeout=10.0) as client:
        with pytest.raises(FluxBackendError, match="no images"):
            await client.generate("x", aspect_ratio="1:1")
