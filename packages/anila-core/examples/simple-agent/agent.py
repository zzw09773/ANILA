"""Simple agent example — echo agent registered on ANILA platform.

Shows the minimal structure for a custom agent built with anila-core SDK:
- Uses CSPPlatformProvider so all LLM calls go through CSP data plane
- Validates incoming requests via CspServiceTokenMiddleware
- Exposes OpenAI-compatible /v1/chat/completions

Run:
    CSP_BASE_URL=http://localhost:8000 CSP_API_KEY=sk-... \
    CSP_SERVICE_TOKEN=my-service-secret \
    uvicorn agent:app --port 9100
"""

from __future__ import annotations

import json
import time
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from anila_core.api.middleware.auth import CspServiceTokenMiddleware
from anila_core.config import settings


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield


app = FastAPI(title="Simple Echo Agent", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CspServiceTokenMiddleware,
    service_token=settings.csp_service_token,
    dev_mode=settings.api_dev_mode,
)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/v1/models")
async def list_models() -> JSONResponse:
    return JSONResponse({
        "object": "list",
        "data": [{"id": "simple-echo-agent", "object": "model",
                  "created": int(time.time()), "owned_by": "example"}],
    })


@app.post("/v1/chat/completions", response_model=None)
async def chat_completions(request: Request) -> JSONResponse | StreamingResponse:
    body: dict = await request.json()
    messages: list[dict] = body.get("messages", [])
    stream: bool = body.get("stream", False)

    # Extract last user message
    user_text = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            user_text = m.get("content", "")
            break

    # Identity: echo with metadata from CSP-forwarded headers
    user_id = request.headers.get("X-ANILA-User-Id", "anonymous")
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
