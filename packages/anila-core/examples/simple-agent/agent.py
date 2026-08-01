"""Simple agent example — echo agent registered on ANILA platform.

Shows the minimal structure for a custom agent:
- Exposes OpenAI-compatible /v1/chat/completions
- Verifies platform dispatch JWT (P2.1) via standalone ``anila_verify``

P2.1 (2026-08-01): the platform signs a ~5-minute RS256 JWT per dispatch
(``Authorization: Bearer <JWT>``; claims ``user_id`` / ``department`` /
``agent_id``). Agents verify against public JWKS — developers do **not**
obtain or store a long-lived ``csk-`` / ``CSP_SERVICE_TOKEN``.

Obtain ``anila_verify.py`` from the governance centre (tier ②). The template
zip may not yet ship verify code / CA / wheel — open the downloaded zip and
check; the UI states when download endpoints are not live yet. Verify
sidecar image is **not** published today.

Run (after placing ``anila_verify.py`` next to this file):

    CSP_BASE_URL=https://<csp-host-reachable-from-agent> \\
    ANILA_CA_FILE=/path/to/cspki_ca_bundle.pem \\
    uvicorn agent:app --port 9100

Do **not** set ``SSL_CERT_FILE`` (it replaces the entire trust store).
"""

from __future__ import annotations

import json
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

try:
    from anila_verify import AnilaVerifyError, verify_authorization
except ImportError:  # pragma: no cover - example documents the download path
    AnilaVerifyError = Exception  # type: ignore[misc, assignment]
    verify_authorization = None  # type: ignore[assignment]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield


app = FastAPI(title="Simple Echo Agent", version="0.1.0", lifespan=lifespan)


def _require_dispatch(request: Request) -> dict:
    """Verify platform dispatch JWT; fail-closed."""
    if verify_authorization is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "anila_verify.py not found — download it from the governance "
                "centre (AgentGuardPanel tier ②) and place it beside this file"
            ),
        )
    base = os.environ.get("CSP_BASE_URL", "").rstrip("/")
    if not base:
        raise HTTPException(status_code=503, detail="CSP_BASE_URL is required")
    try:
        return verify_authorization(
            request.headers.get("Authorization"),
            jwks_url=f"{base}/.well-known/jwks.json",
            ca_file=os.environ.get("ANILA_CA_FILE") or None,
        )
    except AnilaVerifyError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/v1/models")
async def list_models(request: Request) -> JSONResponse:
    _require_dispatch(request)
    return JSONResponse({
        "object": "list",
        "data": [{"id": "simple-echo-agent", "object": "model",
                  "created": int(time.time()), "owned_by": "example"}],
    })


@app.post("/v1/chat/completions", response_model=None)
async def chat_completions(request: Request) -> JSONResponse | StreamingResponse:
    claims = _require_dispatch(request)
    body: dict = await request.json()
    messages: list[dict] = body.get("messages", [])
    stream: bool = body.get("stream", False)

    user_text = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            user_text = m.get("content", "")
            break

    # Identity comes from verified JWT claims (not plaintext X-ANILA-User-*).
    user_id = claims.get("user_id") or "anonymous"
    reply = f"[Echo from simple-agent] User {user_id} said: {user_text}"

    if stream:
        async def _stream() -> AsyncIterator[str]:
            chunk = {
                "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": "simple-echo-agent",
                "choices": [{"index": 0, "delta": {"content": reply}, "finish_reason": None}],
            }
            yield "data: " + json.dumps(chunk) + "\n\n"
            stop_chunk = {**chunk, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
            yield "data: " + json.dumps(stop_chunk) + "\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(_stream(), media_type="text/event-stream",
                                  headers={"Cache-Control": "no-cache"})

    return JSONResponse({
        "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": "simple-echo-agent",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": reply},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    })
