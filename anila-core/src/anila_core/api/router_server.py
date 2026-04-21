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
    classified_override: bool = False,
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
    # One-way latch: never downgrade; upgrade to classified when either the
    # downstream response or the resolved agent demands encryption.
    if classified_override or merged.get("classified"):
        merged["classified"] = True
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
        return {
            "status": "ok",
            "cached_agents": len(registry),
            "last_refresh_error": registry.last_refresh_error,
            "last_refresh_at": registry.last_refresh_at,
        }

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
                (
                    f"已載入 {len(agents)} 個可用 agent"
                    if not registry.last_refresh_error
                    else f"registry refresh 失敗：{registry.last_refresh_error}"
                ),
                status="error" if registry.last_refresh_error else "ok",
            ),
        ]

        # Non-streaming LLM routing call (always — dispatch decision requires
        # full LLM output; see Wave B plan).
        llm_response = await _call_llm_non_stream(caller_api_key, routing_messages)
        if llm_response["error"]:
            base_trace.append(
                _make_trace_step(
                    "direct",
                    "LLM 無法回應",
                    llm_response["error"],
                    status="error",
                )
            )
            fallback_content = (
                "（LLM 暫時無法回應，請稍後再試。若持續發生請檢查 CSP / 本地模型服務。）"
            )
            anila_meta = _merge_anila_meta(
                base_trace,
                None,
                latency_ms=int((time.time() - started_at) * 1000),
            )
            return _respond(fallback_content, anila_meta, stream)

        llm_text = llm_response["content"]
        dispatch = _parse_dispatch(llm_text)

        # Non-dispatch path: Router answers directly.
        if not dispatch:
            base_trace.append(
                _make_trace_step("direct", "Router 直接回答", "無需分派 agent")
            )
            anila_meta = _merge_anila_meta(
                base_trace,
                llm_response.get("anila_meta"),
                latency_ms=int((time.time() - started_at) * 1000),
            )
            return _respond(llm_text, anila_meta, stream)

        agent_id, query = dispatch
        manifest = registry.get(caller_api_key, agent_id)

        # Hallucinated agent id — fall back to direct Router answer.
        if manifest is None:
            base_trace.append(
                _make_trace_step(
                    "direct",
                    "Router 直接回答",
                    f"agent '{agent_id}' 不存在",
                    status="error",
                )
            )
            anila_meta = _merge_anila_meta(
                base_trace,
                llm_response.get("anila_meta"),
                latency_ms=int((time.time() - started_at) * 1000),
            )
            return _respond(llm_text, anila_meta, stream)

        logger.info("Router: dispatching to agent '%s' (stream=%s)", agent_id, stream)
        base_trace.append(
            _make_trace_step(
                "dispatch",
                "選擇 agent",
                f"dispatch_to_agent('{agent_id}')",
            )
        )

        # Streaming dispatch path: forward agent SSE chunks in real time.
        if stream:
            async def _event_stream() -> AsyncIterator[str]:
                # Emit known trace steps before the agent content starts.
                for step in base_trace:
                    yield _make_event("anila.trace", step)
                yield _make_event(
                    "anila.trace",
                    _make_trace_step(
                        "call",
                        f"呼叫 {agent_id}",
                        "POST /v1/chat/completions (經 CSP proxy, streaming)",
                    ),
                )

                downstream_meta: dict[str, Any] | None = None
                had_error = False
                aggregated = ""

                async for event in _stream_agent_sse(agent_id, query, caller_api_key):
                    kind = event.get("type")
                    if kind == "content":
                        piece = event["content"]
                        aggregated += piece
                        yield _make_chunk(piece, "anila-router")
                    elif kind == "meta":
                        downstream_meta = event["anila_meta"]
                    elif kind == "error":
                        had_error = True
                        friendly = (
                            f"（agent「{agent_id}」暫時不可用：{event.get('error')}。"
                            "已自動略過，請稍後再試。）"
                        )
                        yield _make_event(
                            "anila.trace",
                            _make_trace_step(
                                "error",
                                f"{agent_id} 發生錯誤",
                                event.get("detail") or event.get("error", ""),
                                status="error",
                            ),
                        )
                        yield _make_chunk(friendly, "anila-router")
                    elif kind == "done":
                        break

                final_meta = _merge_anila_meta(
                    base_trace,
                    downstream_meta,
                    agent_id=agent_id,
                    latency_ms=int((time.time() - started_at) * 1000),
                    classified_override=bool(manifest.requires_encryption),
                )
                # Streaming path: trace steps already emitted above, so avoid
                # re-emitting them via the meta event.
                final_meta_for_event = {**final_meta, "trace": []}
                yield _make_event("anila.meta", final_meta_for_event)
                yield _make_chunk("", "anila-router", finish="stop")
                yield "data: [DONE]\n\n"
                logger.info(
                    "Router dispatch done (agent=%s, error=%s, len=%d)",
                    agent_id, had_error, len(aggregated),
                )

            return StreamingResponse(
                _event_stream(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        # Non-streaming dispatch path: aggregate via safe dispatch.
        agent_response = await _dispatch_safe(
            agent_id, query, caller_api_key, stream=False
        )
        if agent_response["error"]:
            base_trace.append(
                _make_trace_step(
                    "error",
                    f"{agent_id} 發生錯誤",
                    agent_response["error"],
                    status="error",
                )
            )
        else:
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
            classified_override=bool(manifest.requires_encryption),
        )
        return _respond(agent_response["content"], anila_meta, stream=False)

    def _respond(
        content: str,
        anila_meta: dict[str, Any],
        stream: bool,
    ) -> StreamingResponse | JSONResponse:
        """Shared response builder for the non-streaming-dispatch paths.

        Note: true agent streaming has its own bespoke event_stream above; this
        helper handles Router-direct answers and degraded fallbacks, which emit
        the full content as a single chunk.
        """
        if stream:
            async def _event_stream() -> AsyncIterator[str]:
                for step in anila_meta["trace"]:
                    yield _make_event("anila.trace", step)
                yield _make_chunk(content, "anila-router")
                meta_for_event = {**anila_meta, "trace": []}
                yield _make_event("anila.meta", meta_for_event)
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
    """Call main LLM through CSP without SSE and return content + metadata.

    Never raises — on failure returns ``{"content": "", "error": <str>, ...}`` so
    the Router can degrade gracefully instead of returning 500.
    """
    payload = {
        "model": settings.model,
        "messages": messages,
        "stream": False,
    }
    headers = {
        "Authorization": f"Bearer {caller_api_key}",
        "Content-Type": "application/json",
    }
    try:
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
            "error": None,
        }
    except httpx.HTTPStatusError as exc:
        err = f"LLM upstream HTTP {exc.response.status_code}"
        logger.error("%s — body=%s", err, exc.response.text[:300])
        return {"content": "", "anila_meta": None, "raw": None, "error": err}
    except httpx.RequestError as exc:
        err = f"LLM connection error: {type(exc).__name__}"
        logger.error("%s — %s", err, exc)
        return {"content": "", "anila_meta": None, "raw": None, "error": err}
    except Exception as exc:
        err = f"LLM unexpected error: {type(exc).__name__}"
        logger.exception("LLM call failed")
        return {"content": "", "anila_meta": None, "raw": None, "error": err}


async def _dispatch_safe(
    agent_id: str,
    query: str,
    caller_api_key: str,
    *,
    stream: bool = False,
) -> dict[str, Any]:
    """Call the dispatched agent through CSP; never raises.

    On failure returns ``{"content": <friendly msg>, "error": <str>, ...}`` so
    the Router can surface the outage as a trace step instead of a 500.
    """
    try:
        result = await dispatch_to_agent_response(
            agent_id=agent_id,
            query=query,
            csp_base_url=settings.csp_base_url,
            csp_api_key=caller_api_key,
            stream=stream,
        )
        result["error"] = None
        return result
    except httpx.HTTPStatusError as exc:
        err = f"agent '{agent_id}' HTTP {exc.response.status_code}"
        logger.error("Dispatch failed: %s — body=%s", err, exc.response.text[:300])
        return {
            "content": f"（agent「{agent_id}」暫時不可用：upstream HTTP {exc.response.status_code}，請稍後再試）",
            "anila_meta": None,
            "raw": None,
            "error": err,
        }
    except httpx.RequestError as exc:
        err = f"agent '{agent_id}' connection error: {type(exc).__name__}"
        logger.error("Dispatch failed: %s — %s", err, exc)
        return {
            "content": f"（agent「{agent_id}」連線失敗，已自動略過，請稍後再試）",
            "anila_meta": None,
            "raw": None,
            "error": err,
        }
    except Exception as exc:
        err = f"agent '{agent_id}' unexpected: {type(exc).__name__}"
        logger.exception("Dispatch failed unexpectedly")
        return {
            "content": f"（agent「{agent_id}」發生未預期錯誤，已自動略過）",
            "anila_meta": None,
            "raw": None,
            "error": err,
        }


async def _stream_agent_sse(
    agent_id: str,
    query: str,
    caller_api_key: str,
) -> AsyncIterator[dict[str, Any]]:
    """Open an SSE connection to the dispatched agent via CSP and yield parsed chunks.

    Each yield is a dict: ``{"type": "content"|"meta"|"error"|"done", ...}``. This
    lets the Router forward content deltas as they arrive (true streaming) while
    still capturing agent ``anila_meta`` for the final merged event.
    """
    payload = {
        "model": agent_id,
        "messages": [{"role": "user", "content": query}],
        "stream": True,
    }
    headers = {
        "Authorization": f"Bearer {caller_api_key}",
        "Content-Type": "application/json",
    }
    url = f"{settings.csp_base_url.rstrip('/')}/v1/chat/completions"
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            async with client.stream("POST", url, json=payload, headers=headers) as resp:
                if resp.status_code >= 400:
                    body = await resp.aread()
                    yield {
                        "type": "error",
                        "error": f"agent '{agent_id}' HTTP {resp.status_code}",
                        "detail": body.decode("utf-8", errors="replace")[:300],
                    }
                    return
                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line or not line.startswith("data: "):
                        continue
                    data_str = line[6:]
                    if data_str == "[DONE]":
                        yield {"type": "done"}
                        return
                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(chunk, dict) and chunk.get("anila_meta"):
                        yield {"type": "meta", "anila_meta": chunk["anila_meta"]}
                        continue
                    try:
                        delta = chunk["choices"][0].get("delta", {}) or {}
                    except (KeyError, IndexError, TypeError):
                        continue
                    content_piece = delta.get("content") or ""
                    if content_piece:
                        yield {"type": "content", "content": content_piece}
    except httpx.RequestError as exc:
        yield {
            "type": "error",
            "error": f"agent '{agent_id}' connection error: {type(exc).__name__}",
            "detail": str(exc),
        }
    except Exception as exc:
        logger.exception("Streaming dispatch failed unexpectedly")
        yield {
            "type": "error",
            "error": f"agent '{agent_id}' unexpected: {type(exc).__name__}",
            "detail": str(exc),
        }


# Module-level app instance for direct uvicorn invocation:
#   uvicorn anila_core.api.router_server:app --port 9000
app = create_router_app()
