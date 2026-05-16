"""flux_client: HTTP client that calls flux2-dev /generate."""
from __future__ import annotations

import httpx
import pytest
import respx

from app.flux_client import FluxBackendError, FluxClient


_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


@pytest.mark.asyncio
@respx.mock
async def test_generate_returns_png_bytes():
    respx.post("http://flux2-dev:8000/generate").mock(
        return_value=httpx.Response(200, content=_PNG, headers={"content-type": "image/png"})
    )

    async with FluxClient(base_url="http://flux2-dev:8000", timeout=10.0) as client:
        result = await client.generate("a tank in the mountains", aspect_ratio="16:9")

    assert result == _PNG


@pytest.mark.asyncio
@respx.mock
async def test_generate_sends_correct_body():
    route = respx.post("http://flux2-dev:8000/generate").mock(
        return_value=httpx.Response(200, content=_PNG, headers={"content-type": "image/png"})
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
async def test_generate_raises_on_wrong_content_type():
    respx.post("http://flux2-dev:8000/generate").mock(
        return_value=httpx.Response(200, content=b"not a png", headers={"content-type": "text/plain"})
    )

    async with FluxClient(base_url="http://flux2-dev:8000", timeout=10.0) as client:
        with pytest.raises(FluxBackendError, match="content-type"):
            await client.generate("x", aspect_ratio="1:1")
