"""HTTP client for the flux2-dev inference backend.

The backend is internal to the ``anila-models-net`` docker network
and reachable as ``http://flux2-dev:8000``. As of Studio FLUX Stage 1
(spec 3.2) ``/generate`` accepts ``{prompt, aspect_ratio, seed?,
num_candidates?, ...}`` and returns JSON ``{images: [base64 PNG], seed,
meta}``. This shim only needs one image, so it decodes the first
candidate back to PNG bytes.
"""
from __future__ import annotations

import base64
from typing import Optional

import httpx


class FluxBackendError(RuntimeError):
    pass


class FluxClient:
    def __init__(self, base_url: str, timeout: float = 120.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self) -> "FluxClient":
        self._client = httpx.AsyncClient(timeout=self._timeout)
        return self

    async def __aexit__(self, *exc) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def generate(self, prompt: str, aspect_ratio: str) -> bytes:
        if self._client is None:
            raise RuntimeError("FluxClient must be used as an async context manager")

        resp = await self._client.post(
            f"{self._base_url}/generate",
            json={"prompt": prompt, "aspect_ratio": aspect_ratio},
        )
        if resp.status_code != 200:
            raise FluxBackendError(f"flux backend returned {resp.status_code}: {resp.text[:200]}")

        # Stage 1 contract: JSON {images: [base64 PNG], seed, meta}.
        try:
            data = resp.json()
        except Exception as exc:
            raise FluxBackendError(
                f"flux backend returned non-JSON body: {resp.text[:200]}"
            ) from exc
        images = data.get("images")
        if not images:
            raise FluxBackendError(
                f"flux backend returned no images: {str(data)[:200]}"
            )
        return base64.b64decode(images[0])
