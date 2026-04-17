"""ANILA Core Router — OpenAI-compatible router entrypoint.

Exposes /v1/chat/completions that:
  1. Fetches available agents from CSP (RemoteAgentRegistry, TTL-cached)
  2. Calls the main LLM through CSP proxy with a routing system prompt
  3. If LLM decides to dispatch, calls dispatch_to_agent() via CSP proxy
  4. Returns SSE or JSON response to caller

All LLM/agent calls go through myCSPPlatform — never to upstream directly.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..config import settings
from ..registry.remote_agent_manifest import RemoteAgentManifest, RemoteAgentRegistry
from ..tools.dispatch_tool import dispatch_to_agent

logger = logging.getLogger(__name__)

_ROUTER_SYSTEM_TEMPLATE = """\
You are ANILA Router, an intelligent query dispatcher.

{agent_list}

Instructions:
- If the user's query is best answered by one of the available agents, respond with EXACTLY:
  DISPATCH:<agent_id>:<user_query>
  where <agent_id> is the agent's ID and <user_query> is the full user query to forward.
- If no agent is suitable or no agents are available, answer directly.
- Do NOT add any text before or after the DISPATCH line when dispatching.
"""


def _build_agent_list(agents: list[RemoteAgentManifest]) -> str:
    if not agents:
        return "Available agents: none"
    lines = ["Available agents:"]
    for m in agents:
        lines.append(f"  - {m.to_tool_description()}")
    return "\n".join(lines)


def _parse_dispatch(text: str) -> tuple[str, str] | None:
    """Return (agent_id, query) if text is a DISPATCH command, else None."""
    stripped = text.strip()
    if not stripped.startswith("DISPATCH:"):
        return None
    parts = stripped[9:].split(":", 1)
    if len(parts) != 2:
        return None
    agent_id, query = parts[0].strip(), parts[1].strip()
    if not agent_id or not query:
        return None
    return agent_id, query


def _make_chunk(content: str, model: str, finish: str | None = None) -> str:
    chunk = {
        "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": finish}],
    }
    return "data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n"


def _make_full_response(content: str, model: str) -> dict:
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _extract_bearer_api_key(request: Request) -> str:
    authorization = request.headers.get("Authorization", "")
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer API key")
    api_key = authorization[7:].strip()
    if not api_key:
        raise HTTPException(status_code=401, detail="Missing Bearer API key")
    return api_key


def create_router_app() -> FastAPI:
    """Build and return the ANILA Core Router FastAPI application."""

    registry = RemoteAgentRegistry(
        csp_base_url=settings.csp_base_url,
        ttl=60.0,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logger.info("Router started")
        yield

    app = FastAPI(
        title="ANILA Core Router",
        version="0.1.0",
        lifespan=lifespan,
    )

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "cached_agents": len(registry)}

    @app.get("/v1/models")
    async def list_models() -> JSONResponse:
        return JSONResponse({
            "object": "list",
            "data": [{
                "id": "anila-router",
                "object": "model",
                "created": int(time.time()),
                "owned_by": "anila-core",
            }],
        })

    @app.post("/v1/chat/completions", response_model=None)
    async def chat_completions(request: Request) -> StreamingResponse | JSONResponse:
        caller_api_key = _extract_bearer_api_key(request)
        body: dict = await request.json()
        messages: list[dict] = body.get("messages", [])
        stream: bool = body.get("stream", False)

        await registry.ensure_fresh(caller_api_key)
        agents = registry.list_agents(caller_api_key)

        system_prompt = _ROUTER_SYSTEM_TEMPLATE.format(
            agent_list=_build_agent_list(agents)
        )

        # Prepend router system prompt (don't override a caller-provided system msg)
        has_system = any(m.get("role") == "system" for m in messages)
        if not has_system:
            routing_messages = [{"role": "system", "content": system_prompt}] + messages
        else:
            routing_messages = messages

        # Call main LLM via CSP to get routing decision
        llm_response = await _call_llm_non_stream(caller_api_key, routing_messages)
        dispatch = _parse_dispatch(llm_response)

        if dispatch:
            agent_id, query = dispatch
            manifest = registry.get(caller_api_key, agent_id)
            if manifest is None:
                # Agent not found — answer directly
                content = llm_response
            else:
                logger.info("Router: dispatching to agent '%s'", agent_id)
                content = await dispatch_to_agent(
                    agent_id=agent_id,
                    query=query,
                    csp_base_url=settings.csp_base_url,
                    csp_api_key=caller_api_key,
                    stream=False,
                )
        else:
            content = llm_response

        if stream:
            async def _event_stream() -> AsyncIterator[str]:
                yield _make_chunk(content, "anila-router")
                yield _make_chunk("", "anila-router", finish="stop")
                yield "data: [DONE]\n\n"

            return StreamingResponse(
                _event_stream(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        return JSONResponse(_make_full_response(content, "anila-router"))

    return app


async def _call_llm_non_stream(caller_api_key: str, messages: list[dict]) -> str:
    """Call main LLM through CSP without SSE and return the full text."""
    payload = {
        "model": settings.model,
        "messages": messages,
        "stream": False,
    }
    headers = {
        "Authorization": f"Bearer {caller_api_key}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.post(
            f"{settings.csp_base_url.rstrip('/')}/v1/chat/completions",
            json=payload,
            headers=headers,
        )
        response.raise_for_status()
        data = response.json()
    return data["choices"][0]["message"]["content"].strip()


# Module-level app instance for direct uvicorn invocation:
#   uvicorn anila_core.api.router_server:app --port 9000
app = create_router_app()
