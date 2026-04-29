"""HTTP client to ``anila-functions-worker-api`` (the trusted gate).

Sprint 1 ships this in stub mode — when the env var
``ANILA_FUNCTIONS_STUB_EXTRACT=1`` is set the client returns a canned
metadata payload so the CSP-side endpoint tests can run without the
worker container being up. Sprint 2 wires the real httpx calls and
exercises the worker's ``/extract-meta`` and dispatch endpoints
end-to-end.

The header / env naming intentionally diverges (Codex round-7):

* ENV  ``ANILA_FUNCTIONS_API_SECRET``  (full prefix convention)
* HTTP ``X-Functions-Api-Secret``      (HTTP header convention)

Same value, two surface forms.
"""

from __future__ import annotations

import os
from typing import AsyncIterator

import httpx


WORKER_API_URL = os.environ.get(
    "ANILA_FUNCTIONS_WORKER_API_URL",
    "http://anila-functions-worker-api:8000",
)
API_SECRET_ENV = "ANILA_FUNCTIONS_API_SECRET"
SECRET_HEADER = "X-Functions-Api-Secret"


class WorkerClient:
    """Thin httpx wrapper. Raises on connection error / non-2xx."""

    def __init__(self, base_url: str | None = None, secret: str | None = None):
        self.base_url = base_url or WORKER_API_URL
        self.secret = secret or os.environ.get(API_SECRET_ENV, "")
        self._headers = {SECRET_HEADER: self.secret} if self.secret else {}

    async def extract_meta(self, code: str) -> dict:
        """POST /extract-meta — extract Action / Valves / metadata schema.

        Sprint 1 stub path: when ``ANILA_FUNCTIONS_STUB_EXTRACT=1`` the
        client short-circuits and returns a deterministic canned reply.
        Used by backend integration tests so they don't need the worker
        container running.
        """
        if os.environ.get("ANILA_FUNCTIONS_STUB_EXTRACT") == "1":
            return self._stub_extract(code)

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{self.base_url}/extract-meta",
                json={"code": code},
                headers=self._headers,
            )
            resp.raise_for_status()
            return resp.json()

    async def stream_run(self, payload: dict) -> AsyncIterator[bytes]:
        """POST to the worker's run endpoint, yield SSE bytes as the
        worker streams them.

        Caller is responsible for forwarding to its own SSE response.
        Timeout is None because user-code dispatch can legitimately take
        up to 30s; the worker enforces its own wall-clock limit.
        """
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream(
                "POST",
                f"{self.base_url}/exec",
                json=payload,
                headers=self._headers,
            ) as resp:
                resp.raise_for_status()
                async for chunk in resp.aiter_bytes():
                    yield chunk

    @staticmethod
    def _stub_extract(code: str) -> dict:
        """Sprint 1 canned response.

        Returns a single 'stub-btn' action and an empty Valves schema so
        the create / save_version paths exercise the full INSERT chain
        without invoking subprocess / sandbox infrastructure.
        """
        return {
            "actions_meta_json": [
                {"id": "stub-btn", "name": "Stub", "icon_url": None}
            ],
            "valves_schema_json": {"type": "object", "properties": {}},
            "metadata_json": {"title": "Stub Function", "version": "1.0"},
            "extract_strategy": "stub",
            "errors": [],
        }
