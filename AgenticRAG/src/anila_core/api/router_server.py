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
from typing import Any, AsyncIterator

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..config import settings
from ..registry.remote_agent_manifest import RemoteAgentManifest, RemoteAgentRegistry
from ..tools.dispatch_tool import dispatch_to_agent_response

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


def _make_event(event: str, payload: dict[str, Any]) -> str:
    return f"event: {event}\n" + "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


def _make_full_response(content: str, model: str, anila_meta: dict[str, Any] | None = None) -> dict:
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        "anila_meta": anila_meta or _default_anila_meta(),
    }


def _default_anila_meta() -> dict[str, Any]:
    return {
        "trace_id": f"trace-{uuid.uuid4().hex[:12]}",
        "trace": [],
        "citations": [],
        "confidence": None,
        "handoff_chain": [],
        "follow_ups": [],
        "latency_ms": None,
        "classified": False,
    }


def _make_trace_step(
    kind: str,
    label: str,
    detail: str,
    *,
    status: str = "ok",
    latency_ms: int | None = None,
) -> dict[str, Any]:
    step = {"kind": kind, "label": label, "detail": detail, "status": status}
    if latency_ms is not None:
        step["latency_ms"] = latency_ms
    return step


def _normalize_anila_meta(meta: dict[str, Any] | None) -> dict[str, Any]:
    base = _default_anila_meta()
    if not meta:
        return base
    normalized = {**base, **meta}
    normalized["trace"] = list(meta.get("trace") or [])
    normalized["citations"] = list(meta.get("citations") or [])
    normalized["handoff_chain"] = list(meta.get("handoff_chain") or [])
    normalized["follow_ups"] = list(meta.get("follow_ups") or [])
    return normalized


def _merge_anila_meta(
    base_trace: list[dict[str, Any]],
    downstream_meta: dict[str, Any] | None,
    *,
    agent_id: str | None = None,
    latency_ms: int | None = None,
) -> dict[str, Any]:
    merged = _normalize_anila_meta(downstream_meta)
    merged["trace"] = [*base_trace, *merged["trace"]]
    handoff_chain = list(merged.get("handoff_chain") or [])
    if agent_id:
        handoff_chain = [
            {
                "agent_id": "anila-router",
                "label": "Router dispatch",
                "status": "ok",
                "latency_ms": latency_ms,
                "input_summary": "router decision",
                "output_summary": f"dispatch to {agent_id}",
            },
            *handoff_chain,
        ]
    merged["handoff_chain"] = handoff_chain
    if latency_ms is not None:
        merged["latency_ms"] = latency_ms
    return merged


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

        started_at = time.time()

        base_trace = [
            _make_trace_step(
                "thinking",
                "Router 分析意圖中",
                f"解析 query: {_flatten_last_user_query(messages)}",
            ),
            _make_trace_step(
                "registry",
                "同步 agent 清單",
                f"已載入 {len(agents)} 個可用 agent",
            ),
        ]

        llm_response = await _call_llm_non_stream(caller_api_key, routing_messages)
        llm_text = llm_response["content"]
        dispatch = _parse_dispatch(llm_text)

        if dispatch:
            agent_id, query = dispatch
            manifest = registry.get(caller_api_key, agent_id)
            if manifest is None:
                content = llm_text
                base_trace.append(
                    _make_trace_step("direct", "Router 直接回答", f"agent '{agent_id}' 不存在")
                )
                anila_meta = _merge_anila_meta(
                    base_trace,
                    llm_response.get("anila_meta"),
                    latency_ms=int((time.time() - started_at) * 1000),
                )
            else:
                logger.info("Router: dispatching to agent '%s'", agent_id)
                base_trace.append(
                    _make_trace_step(
                        "dispatch",
                        "選擇 agent",
                        f"dispatch_to_agent('{agent_id}')",
                    )
                )
                agent_response = await dispatch_to_agent_response(
                    agent_id=agent_id,
                    query=query,
                    csp_base_url=settings.csp_base_url,
                    csp_api_key=caller_api_key,
                    stream=False,
                )
                content = agent_response["content"]
                base_trace.append(
                    _make_trace_step(
                        "call",
                        f"呼叫 {agent_id}",
                        "POST /v1/chat/completions (經 CSP proxy)",
                    )
                )
                anila_meta = _merge_anila_meta(
                    base_trace,
                    agent_response.get("anila_meta"),
                    agent_id=agent_id,
                    latency_ms=int((time.time() - started_at) * 1000),
                )
        else:
            content = llm_text
            base_trace.append(_make_trace_step("direct", "Router 直接回答", "無需分派 agent"))
            anila_meta = _merge_anila_meta(
                base_trace,
                llm_response.get("anila_meta"),
                latency_ms=int((time.time() - started_at) * 1000),
            )

        if stream:
            async def _event_stream() -> AsyncIterator[str]:
                for step in anila_meta["trace"]:
                    yield _make_event("anila.trace", step)
                yield _make_chunk(content, "anila-router")
                yield _make_event("anila.meta", anila_meta)
                yield _make_chunk("", "anila-router", finish="stop")
                yield "data: [DONE]\n\n"

            return StreamingResponse(
                _event_stream(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        return JSONResponse(_make_full_response(content, "anila-router", anila_meta=anila_meta))

    return app


def _flatten_last_user_query(messages: list[dict[str, Any]]) -> str:
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content[:120]
        return str(content)[:120]
    return ""


async def _call_llm_non_stream(caller_api_key: str, messages: list[dict]) -> dict[str, Any]:
    """Call main LLM through CSP without SSE and return content + metadata."""
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
    return {
        "content": data["choices"][0]["message"]["content"].strip(),
        "anila_meta": data.get("anila_meta"),
        "raw": data,
    }


# Module-level app instance for direct uvicorn invocation:
#   uvicorn anila_core.api.router_server:app --port 9000
app = create_router_app()
