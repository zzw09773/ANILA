from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any, AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse


app = FastAPI(title="ANILA Smoke OpenAI Backend", version="0.1.0")

EMBEDDING_DIMENSION = 4096


def _extract_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


def _last_user_text(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            return _extract_text(message.get("content", ""))
    return ""


def _system_text(messages: list[dict[str, Any]]) -> str:
    for message in messages:
        if message.get("role") == "system":
            return _extract_text(message.get("content", ""))
    return ""


def _response_text(messages: list[dict[str, Any]]) -> str:
    user_text = _last_user_text(messages)
    system_text = _system_text(messages)
    if "You are ANILA Router" in system_text and "agentic-rag" in system_text:
        return f"DISPATCH:agentic-rag:{user_text}"
    return f"Mock answer from agentic-rag: {user_text or 'ok'}"


def _usage(payload: dict[str, Any], content: str) -> dict[str, int]:
    prompt_size = len(json.dumps(payload.get("messages", []), ensure_ascii=False))
    completion_size = len(content)
    prompt_tokens = max(1, prompt_size // 4)
    completion_tokens = max(1, completion_size // 4)
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }


def _chunk(content: str, model: str, *, finish_reason: str | None = None, usage: dict[str, int] | None = None) -> str:
    body: dict[str, Any] = {
        "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "delta": {"content": content} if content else {},
            "finish_reason": finish_reason,
        }],
    }
    if usage:
        body["usage"] = usage
    return "data: " + json.dumps(body, ensure_ascii=False) + "\n\n"


def _embedding_for_text(text: str) -> list[float]:
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    values: list[float] = []
    for idx in range(EMBEDDING_DIMENSION):
        byte = digest[idx % len(digest)]
        values.append(round((byte / 255.0) - 0.5, 6))
    return values


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/models")
async def models() -> JSONResponse:
    return JSONResponse({
        "object": "list",
        "data": [
            {"id": "gpt-4o-mini", "object": "model", "owned_by": "smoke-openai"},
            {"id": "text-embedding-3-small", "object": "model", "owned_by": "smoke-openai"},
        ],
    })


@app.post("/v1/chat/completions", response_model=None)
@app.post("/chat/completions", response_model=None)
async def chat_completions(request: Request) -> StreamingResponse | JSONResponse:
    payload = await request.json()
    model = payload.get("model", "gpt-4o-mini")
    content = _response_text(payload.get("messages", []))
    usage = _usage(payload, content)
    stream = bool(payload.get("stream"))
    if not stream:
        return JSONResponse({
            "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }],
            "usage": usage,
        })

    async def event_stream() -> AsyncIterator[str]:
        yield _chunk(content, model)
        yield _chunk("", model, finish_reason="stop", usage=usage)
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/v1/embeddings")
@app.post("/embeddings")
async def embeddings(request: Request) -> JSONResponse:
    payload = await request.json()
    model = payload.get("model", "text-embedding-3-small")
    inputs = payload.get("input", [])
    if isinstance(inputs, str):
        inputs = [inputs]
    data = []
    for idx, text in enumerate(inputs):
        data.append({
            "object": "embedding",
            "index": idx,
            "embedding": _embedding_for_text(str(text)),
        })
    return JSONResponse({
        "object": "list",
        "data": data,
        "model": model,
        "usage": {"prompt_tokens": max(1, len(inputs)), "total_tokens": max(1, len(inputs))},
    })
