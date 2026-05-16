"""HTTP client for the flux2-dev inference backend.

The backend is internal to the ``anila-models-net`` docker network
and reachable as ``http://flux2-dev:8000``. It accepts a JSON body
``{prompt, aspect_ratio}`` and returns ``image/png`` bytes.
"""
from __future__ import annotations

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

        ctype = resp.headers.get("content-type", "")
        if not ctype.startswith("image/png"):
            raise FluxBackendError(f"unexpected content-type from flux backend: {ctype!r}")

        return resp.content
