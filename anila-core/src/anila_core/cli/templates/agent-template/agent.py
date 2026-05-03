"""{{AGENT_NAME}} — ANILA agent built with anila-core.

This is the entry point for your agent.  It exposes an OpenAI-compatible
/v1/chat/completions endpoint and validates that every inbound request
originates from the ANILA CSP proxy (via X-CSP-Service-Token).

Run locally (development):
    uvicorn agent:app --reload --port 9100

Run with full CSP wiring:
    CSP_BASE_URL=http://localhost:8000 \\
    CSP_API_KEY=sk-your-key \\
    CSP_SERVICE_TOKEN=dev-service-token \\
    uvicorn agent:app --host 0.0.0.0 --port 9100
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
from anila_core.context.agent_context import get_current_context
from anila_core.providers.cspplatform_provider import CSPPlatformProvider


# ── Provider ─────────────────────────────────────────────────────────────────

def _make_provider() -> CSPPlatformProvider | None:
    """Return a CSP-backed LLM provider, or None when running in dev mode."""
    if not settings.csp_base_url or settings.csp_base_url == "http://localhost:8000":
        return None
    return CSPPlatformProvider(
        csp_base_url=settings.csp_base_url,
        csp_api_key=settings.csp_api_key,
    )


# ── App ───────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Put startup logic here (e.g. load vector index, warm up model)
    yield
    # Put shutdown logic here


app = FastAPI(title="{{AGENT_DISPLAY_NAME}}", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CspServiceTokenMiddleware,
    service_token=settings.csp_service_token,
    dev_mode=settings.api_dev_mode,
)


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "agent": "{{AGENT_NAME}}"}


@app.get("/v1/models")
async def list_models() -> JSONResponse:
    return JSONResponse({
        "object": "list",
        "data": [{
            "id": "{{AGENT_NAME}}",
            "object": "model",
            "created": int(time.time()),
            "owned_by": "{{AGENT_NAME}}",
        }],
    })


# ── Chat completions ──────────────────────────────────────────────────────────

@app.post("/v1/chat/completions", response_model=None)
async def chat_completions(request: Request) -> JSONResponse | StreamingResponse:
    body: dict = await request.json()
    messages: list[dict] = body.get("messages", [])
    stream: bool = body.get("stream", False)

    # Read identity injected by CSP proxy
    user_id = request.headers.get("X-ANILA-User-Id", "anonymous")
    user_email = request.headers.get("X-ANILA-User-Email", "")

    # ── TODO: implement your agent logic here ────────────────────────────────
    # Examples:
    #   - Call an LLM via CSPPlatformProvider
    #   - Search a vector database
    #   - Call an internal API
    #   - Use the QueryEngine turn loop from anila_core.engine
    #
    # For now we return a stub response so the agent can be registered and tested.
    last_user_msg = next(
        (m.get("content", "") for m in reversed(messages) if m.get("role") == "user"),
        "",
    )
    reply = (
        f"[{{AGENT_DISPLAY_NAME}}] Hello {user_email or user_id}! "
        f"You asked: {last_user_msg!r}. "
        "Implement your logic in agent.py."
    )
    # ── end TODO ─────────────────────────────────────────────────────────────

    req_id = f"chatcmpl-{uuid.uuid4().hex[:8]}"
    created = int(time.time())
    trace_id = f"trace-{uuid.uuid4().hex[:12]}"

    # Sprint 13 follow-up: honour the per-run classified latch. Tools
    # that consulted a classified agent during this turn (e.g.
    # ``agent_as_tool`` against an agent whose
    # ``requires_encryption=True``) will have flipped
    # ``ctx.classified_latch``. OR it into our own classified flag so
    # the downstream ``anila_meta.classified`` one-way latch keeps
    # working even when the taint enters via a sub-call rather than
    # via direct CSP dispatch.
    ctx = get_current_context()
    classified_from_subcalls = bool(ctx and ctx.classified_latch)

    anila_meta = {
        "trace_id": trace_id,
        "trace": [
            {
                "kind": "agent",
                "label": "{{AGENT_DISPLAY_NAME}} 處理請求",
                "detail": "stub agent response",
                "status": "ok",
            }
        ],
        "citations": [],
        "confidence": None,
        "handoff_chain": [
            {
                "agent_id": "{{AGENT_NAME}}",
                "label": "{{AGENT_DISPLAY_NAME}}",
                "status": "ok",
                "input_summary": last_user_msg[:80],
                "output_summary": "stub response generated",
            }
        ],
        "follow_ups": [],
        "latency_ms": None,
        "classified": classified_from_subcalls,
    }

    if stream:
        async def _stream() -> AsyncIterator[str]:
            chunk = {
                "id": req_id, "object": "chat.completion.chunk",
                "created": created, "model": "{{AGENT_NAME}}",
                "choices": [{"index": 0, "delta": {"content": reply}, "finish_reason": None}],
            }
            yield "event: anila.trace\n"
            yield "data: " + json.dumps(anila_meta["trace"][0]) + "\n\n"
            yield "data: " + json.dumps(chunk) + "\n\n"
            yield "event: anila.meta\n"
            yield "data: " + json.dumps(anila_meta) + "\n\n"
            stop = {**chunk, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
            yield "data: " + json.dumps(stop) + "\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            _stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )

    return JSONResponse({
        "id": req_id, "object": "chat.completion",
        "created": created, "model": "{{AGENT_NAME}}",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": reply},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": len(reply.split()),
                  "total_tokens": len(reply.split())},
        "anila_meta": anila_meta,
    })
