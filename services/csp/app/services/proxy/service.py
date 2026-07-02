"""Proxy orchestration: forward requests to model backends with retry + timeout.

Split out of ``app/services/proxy_service.py`` (Doc-10 Slice 1,
behavior-preserving refactor). ``proxy_request`` / ``proxy_stream`` /
``build_default_anila_meta`` bodies are verbatim; the helpers they call now
live in the sibling modules of this package (headers / sse / usage / guard).
"""
import asyncio
import json
import logging
import time
from typing import AsyncIterator, Optional

from fastapi import HTTPException
import httpx

from app.config import settings
from app.models.model_registry import ModelRegistry
from app.services.proxy.guard import _guard_outbound
from app.services.proxy.headers import (
    _apply_gateway_auth,
    build_agent_headers,
    build_model_gateway_headers,
)
from app.services.proxy.sse import _aggregate_sse_to_chat_completion, _parse_sse_block
from app.services.proxy.usage import (
    _estimate_token_count,
    _extract_response_text,
    _extract_stream_text,
    _serialize_request_for_usage,
)
from app.services.usage_writer import enqueue_usage

# Keep the pre-split logger channel ("app.services.proxy_service") so log
# routing / filtering / capture behavior is identical after the package split.
logger = logging.getLogger("app.services.proxy_service")

def _get_timeout(model_type: str) -> float:
    if model_type == "embedding":
        return settings.EMBEDDING_TIMEOUT
    return settings.LLM_TIMEOUT


def build_default_anila_meta(
    source_name: str,
    *,
    detail: str,
    latency_ms: int | None = None,
    classified: bool = False,
    usage: dict | None = None,
) -> dict:
    """Build an anila_meta skeleton used when the downstream omits one.

    The ``classified`` flag is a one-way latch. It is driven solely by the
    resolved **agent's** ``requires_encryption`` attribute (base LLMs do not
    carry this flag). Downstreams that set ``classified=true`` in their own
    ``anila.meta`` are authoritative; this default only fills the baseline
    when nothing is emitted.
    """
    return {
        "trace_id": f"trace-{int(time.time() * 1000)}",
        "trace": [
            {
                "kind": "call",
                "label": f"呼叫 {source_name}",
                "detail": detail,
                "status": "ok",
            }
        ],
        "citations": [],
        "confidence": None,
        "handoff_chain": [],
        "follow_ups": [],
        "latency_ms": latency_ms,
        "classified": bool(classified),
        # Token usage so the chat UI can show per-message token counts. Omitted
        # (None) when unknown; the client only renders when present.
        "usage": usage,
    }


async def proxy_request(
    model: ModelRegistry,
    api_key_id: int,
    user_id: int,
    department_id: int | None,
    request_body: dict,
    endpoint_path: str,
    user_email: Optional[str] = None,
    user_identity: Optional[str] = None,
    conversation_id: Optional[str] = None,
    trace_id: Optional[str] = None,
    requires_encryption: bool = False,
    target_agent_id: Optional[int] = None,
    caller_agent_id: Optional[int] = None,
    caller_client_id: Optional[int] = None,
) -> dict:
    """Forward request to model backend with exponential backoff retry.

    Sprint 5 / Chunk W: classifies the call as ``request_type='embedding'``
    when the path looks like an embedding endpoint, ``'judge'`` when an
    explicit ``X-Anila-Request-Type: judge`` header is set upstream, else
    'chat'. The classification rides into ``token_usage`` for split-by-kind
    dashboards.
    """
    timeout = _get_timeout(model.model_type)
    base_url = model.endpoint_url.rstrip("/")
    request_type = (
        "embedding"
        if "embedding" in endpoint_path or model.model_type == "embedding"
        else "chat"
    )

    # Sprint 5 / Chunk W: AUTO_REGISTER_MODELS has historically set
    # endpoint_url with a trailing ``/v1`` (e.g. ``http://...:11434/v1``);
    # endpoint_path also starts with ``/v1`` (``/v1/embeddings``).
    # Naive concat → ``//v1/v1/embeddings`` → upstream 404. Strip the
    # trailing version segment from base_url when endpoint_path already
    # carries one. Idempotent for already-stripped urls.
    if base_url.endswith("/v1") and endpoint_path.startswith("/v1/"):
        base_url = base_url[:-3]
    elif base_url.endswith("/v2") and endpoint_path.startswith("/v2/"):
        base_url = base_url[:-3]

    # Determine the correct path based on api_version
    if model.api_version == "v2" and "embedding" in endpoint_path:
        target_url = f"{base_url}/v2/embeddings"
    else:
        target_url = f"{base_url}{endpoint_path}"

    # Call-time SSRF re-validation (TOCTOU / DNS-rebinding defense).
    _guard_outbound(target_url)

    last_error = None
    start_time = time.time()

    # ``user_id`` (DB PK) is for usage attribution only; the wire identity
    # is ``user_identity`` (員編). Builder chosen by DESTINATION (not a caller
    # flag) so the model gateway can never receive the CSP service token:
    # agent → full identity + token; model/embedding → 員編-only.
    if model.model_type == "agent":
        req_headers = build_agent_headers(
            user_identity, user_email, target_agent_id=target_agent_id
        )
    else:
        req_headers = build_model_gateway_headers(user_identity)
    # gateway key 只給 model 呼叫;agent dispatch (model_type='agent') 不帶。
    if model.model_type != "agent":
        _apply_gateway_auth(req_headers)

    for attempt in range(settings.PROXY_MAX_RETRIES):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    target_url,
                    json=request_body,
                    headers=req_headers,
                )

            duration_ms = int((time.time() - start_time) * 1000)

            if response.status_code >= 500:
                # Full upstream body stays server-side only; the client gets a
                # generic message so internal errors / stack traces never leak.
                last_error = f"後端回應 {response.status_code}: {response.text[:500]}"
                if attempt < settings.PROXY_MAX_RETRIES - 1:
                    delay = settings.PROXY_RETRY_BASE_DELAY * (2 ** attempt)
                    logger.warning(
                        f"模型 {model.name} 回應 {response.status_code}，"
                        f"{delay}s 後重試 ({attempt + 1}/{settings.PROXY_MAX_RETRIES})"
                    )
                    await asyncio.sleep(delay)
                    continue
                logger.error("模型 %s 上游 5xx: %s", model.name, last_error)
                raise HTTPException(status_code=502, detail="模型服務暫時不可用")

            if response.status_code >= 400:
                # Log the full upstream body server-side; forward only a
                # sanitized message (OpenAI-shape error.message when present),
                # never the raw text/JSON which can leak internal detail.
                logger.warning(
                    "模型 %s 上游 %s: %s",
                    model.name,
                    response.status_code,
                    response.text[:500],
                )
                detail = f"模型服務拒絕請求 (HTTP {response.status_code})"
                try:
                    body = response.json()
                    msg = (
                        body.get("error", {}).get("message")
                        if isinstance(body, dict)
                        else None
                    )
                    if isinstance(msg, str) and msg:
                        detail = msg[:300]
                except Exception:
                    pass
                raise HTTPException(status_code=response.status_code, detail=detail)

            # Fallback: upstream returned SSE despite our non-stream request
            # (some agents — e.g. asrd — only speak streaming). Aggregate the
            # deltas into one OpenAI-shape chat.completion object so the
            # caller gets JSON.
            content_type = response.headers.get("content-type", "")
            body_preview = response.text[:8].lstrip()
            if "text/event-stream" in content_type or body_preview.startswith("data:"):
                result = _aggregate_sse_to_chat_completion(response.text, model.name)
            else:
                result = response.json()

            # Extract token usage from response
            usage = result.get("usage", {})
            prompt_tokens = usage.get("prompt_tokens", 0)
            completion_tokens = usage.get("completion_tokens", 0)
            total_tokens = usage.get("total_tokens", prompt_tokens + completion_tokens)
            if not usage:
                prompt_tokens = _estimate_token_count(
                    model.name, _serialize_request_for_usage(request_body)
                )
                completion_tokens = _estimate_token_count(
                    model.name, _extract_response_text(result)
                )
                total_tokens = prompt_tokens + completion_tokens
                logger.warning(
                    "模型 %s 非串流回應未提供 usage，改用伺服端估算: prompt=%s completion=%s",
                    model.name,
                    prompt_tokens,
                    completion_tokens,
                )
            existing_meta = result.get("anila_meta")
            if not existing_meta:
                result["anila_meta"] = build_default_anila_meta(
                    model.name,
                    detail=f"Proxy -> {target_url}",
                    latency_ms=duration_ms,
                    classified=requires_encryption,
                )
            elif requires_encryption and isinstance(existing_meta, dict):
                # One-way latch: upgrade to classified when the resolved model
                # requires encryption, even if the downstream omitted the flag.
                existing_meta["classified"] = True

            # Enqueue usage record (non-blocking).
            # Sprint 5 / Chunk W: ``request_type`` flows from the
            # endpoint-path classification at the top of this function so
            # embedding rows get tagged distinct from chat rows.
            await enqueue_usage(
                api_key_id=api_key_id,
                user_id=user_id,
                department_id=department_id,
                model_id=model.id,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                request_duration_ms=duration_ms,
                conversation_id=conversation_id,
                trace_id=trace_id,
                request_type=request_type,
                caller_agent_id=caller_agent_id,
                caller_client_id=caller_client_id,
            )

            return result

        except httpx.TimeoutException:
            last_error = f"請求逾時 ({timeout}s)"
            if attempt < settings.PROXY_MAX_RETRIES - 1:
                delay = settings.PROXY_RETRY_BASE_DELAY * (2 ** attempt)
                logger.warning(
                    f"模型 {model.name} 請求逾時，"
                    f"{delay}s 後重試 ({attempt + 1}/{settings.PROXY_MAX_RETRIES})"
                )
                await asyncio.sleep(delay)
                continue

        except httpx.ConnectError:
            last_error = f"無法連線到模型端點 {target_url}"
            if attempt < settings.PROXY_MAX_RETRIES - 1:
                delay = settings.PROXY_RETRY_BASE_DELAY * (2 ** attempt)
                logger.warning(
                    f"模型 {model.name} 連線失敗，"
                    f"{delay}s 後重試 ({attempt + 1}/{settings.PROXY_MAX_RETRIES})"
                )
                await asyncio.sleep(delay)
                continue

        except HTTPException:
            raise

        except Exception as e:
            last_error = str(e)
            logger.error(f"代理請求錯誤: {e}")
            if attempt < settings.PROXY_MAX_RETRIES - 1:
                delay = settings.PROXY_RETRY_BASE_DELAY * (2 ** attempt)
                await asyncio.sleep(delay)
                continue

    raise HTTPException(
        status_code=502,
        detail=f"模型服務不可用，已重試 {settings.PROXY_MAX_RETRIES} 次: {last_error}",
    )


async def proxy_stream(
    target_url: str,
    api_key_id: int,
    user_id: int,
    department_id: int | None,
    usage_model_id: int,
    request_body: dict,
    user_email: Optional[str] = None,
    user_identity: Optional[str] = None,
    model_name: str | None = None,
    conversation_id: Optional[str] = None,
    trace_id: Optional[str] = None,
    requires_encryption: bool = False,
    target_agent_id: Optional[int] = None,
    caller_agent_id: Optional[int] = None,
    caller_client_id: Optional[int] = None,
) -> AsyncIterator[str]:
    """Stream SSE response from a downstream backend through CSP proxy.

    Intercepts the final usage chunk to record token consumption, then
    forwards all SSE chunks verbatim to the caller. If usage is missing,
    performs a server-side token estimate from request/response text.
    """
    # Call-time SSRF re-validation (TOCTOU / DNS-rebinding defense).
    _guard_outbound(target_url)

    # ``user_id`` (DB PK) is for usage attribution only; ``user_identity``
    # (員編) is the wire identity. Builder chosen by DESTINATION
    # (target_agent_id) — never a caller flag — so the model gateway can
    # never receive the CSP service token.
    if target_agent_id is not None:
        headers = build_agent_headers(
            user_identity, user_email, target_agent_id=target_agent_id
        )
    else:
        headers = build_model_gateway_headers(user_identity)
    # gateway key 只給 model 串流;agent 串流 (target_agent_id 非 None) 不帶。
    if target_agent_id is None:
        _apply_gateway_auth(headers)
    # Force stream_options so the downstream sends usage in last chunk
    body = {**request_body, "stream": True,
            "stream_options": {"include_usage": True}}

    start_time = time.time()
    prompt_tokens = completion_tokens = 0
    usage_seen = False
    meta_seen = False
    prompt_text = _serialize_request_for_usage(body)
    completion_parts: list[str] = []
    pending_done_block: str | None = None

    try:
        async with httpx.AsyncClient(timeout=settings.LLM_TIMEOUT) as client:
            async with client.stream("POST", target_url, json=body, headers=headers) as resp:
                if resp.status_code >= 400:
                    raise HTTPException(status_code=resp.status_code,
                                        detail=f"下游回應錯誤: {resp.status_code}")
                def _emit(block: str, event_name: str | None, data: str | None) -> str:
                    """Render a block, upgrading anila.meta.classified if required."""
                    if (
                        event_name == "anila.meta"
                        and data
                        and requires_encryption
                    ):
                        try:
                            parsed = json.loads(data)
                            if isinstance(parsed, dict) and not parsed.get("classified"):
                                parsed["classified"] = True
                                return (
                                    "event: anila.meta\n"
                                    + "data: "
                                    + json.dumps(parsed, ensure_ascii=False)
                                    + "\n\n"
                                )
                        except (json.JSONDecodeError, TypeError):
                            pass
                    return block + "\n\n"

                block_lines: list[str] = []
                async for line in resp.aiter_lines():
                    if line == "":
                        if block_lines:
                            block = "\n".join(block_lines)
                            event_name, data = _parse_sse_block(block)
                            if data == "[DONE]":
                                pending_done_block = block + "\n\n"
                            else:
                                if event_name == "anila.meta":
                                    meta_seen = True
                                if data and event_name in (None, "message"):
                                    try:
                                        chunk = json.loads(data)
                                        text = _extract_stream_text(chunk)
                                        if text:
                                            completion_parts.append(text)
                                        usage = chunk.get("usage") or {}
                                        if usage:
                                            usage_seen = True
                                            prompt_tokens = usage.get("prompt_tokens", 0)
                                            completion_tokens = usage.get("completion_tokens", 0)
                                    except (json.JSONDecodeError, KeyError):
                                        pass
                                yield _emit(block, event_name, data)
                            block_lines = []
                        continue
                    block_lines.append(line)
                if block_lines:
                    block = "\n".join(block_lines)
                    event_name, data = _parse_sse_block(block)
                    if data == "[DONE]":
                        pending_done_block = block + "\n\n"
                    else:
                        if event_name == "anila.meta":
                            meta_seen = True
                        if data and event_name in (None, "message"):
                            try:
                                chunk = json.loads(data)
                                text = _extract_stream_text(chunk)
                                if text:
                                    completion_parts.append(text)
                                usage = chunk.get("usage") or {}
                                if usage:
                                    usage_seen = True
                                    prompt_tokens = usage.get("prompt_tokens", 0)
                                    completion_tokens = usage.get("completion_tokens", 0)
                            except (json.JSONDecodeError, KeyError):
                                pass
                        yield _emit(block, event_name, data)
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="下游請求逾時")
    except httpx.ConnectError:
        raise HTTPException(status_code=502, detail=f"無法連線到下游端點 {target_url}")

    duration_ms = int((time.time() - start_time) * 1000)
    if not usage_seen:
        prompt_tokens = _estimate_token_count(model_name, prompt_text)
        completion_tokens = _estimate_token_count(model_name, "".join(completion_parts))
        logger.warning(
            "串流回應未提供 usage，改用伺服端估算 %s: prompt=%s completion=%s",
            model_name or target_url,
            prompt_tokens,
            completion_tokens,
        )
    total_tokens = prompt_tokens + completion_tokens
    if not meta_seen:
        yield "event: anila.meta\n"
        yield "data: " + json.dumps(
            build_default_anila_meta(
                model_name or target_url,
                detail=f"Proxy stream -> {target_url}",
                latency_ms=duration_ms,
                classified=requires_encryption,
                usage={
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": total_tokens,
                },
            ),
            ensure_ascii=False,
        ) + "\n\n"
    if pending_done_block:
        yield pending_done_block
    if total_tokens > 0:
        await enqueue_usage(
            api_key_id=api_key_id,
            user_id=user_id,
            department_id=department_id,
            model_id=usage_model_id,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            request_duration_ms=duration_ms,
            conversation_id=conversation_id,
            trace_id=trace_id,
            caller_agent_id=caller_agent_id,
            caller_client_id=caller_client_id,
        )
