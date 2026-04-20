"""Proxy service: forward requests to model backends with retry + timeout."""
import asyncio
import json
import logging
import math
import re
import time
from typing import AsyncIterator, Optional
from fastapi import HTTPException
import httpx
from app.config import settings
from app.models.model_registry import ModelRegistry
from app.services.usage_writer import enqueue_usage

logger = logging.getLogger(__name__)


def _get_timeout(model_type: str) -> float:
    if model_type == "embedding":
        return settings.EMBEDDING_TIMEOUT
    return settings.LLM_TIMEOUT


def _build_downstream_headers(
    user_id: int,
    user_email: Optional[str] = None,
    user_groups: Optional[str] = None,
) -> dict:
    """Build service credential + identity headers for downstream agents."""
    headers: dict = {"Content-Type": "application/json"}
    if settings.CSP_SERVICE_TOKEN:
        headers["X-CSP-Service-Token"] = settings.CSP_SERVICE_TOKEN
    headers["X-ANILA-User-Id"] = str(user_id)
    if user_email:
        headers["X-ANILA-User-Email"] = user_email
    if user_groups:
        headers["X-ANILA-User-Groups"] = user_groups
    return headers


def _flatten_content(content) -> str:
    """Best-effort flattening of OpenAI-compatible message content."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
                continue
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            elif "text" in block:
                parts.append(str(block.get("text", "")))
            elif "content" in block:
                parts.append(str(block.get("content", "")))
        return "\n".join(p for p in parts if p)
    return str(content)


def _serialize_request_for_usage(request_body: dict) -> str:
    """Flatten request payload into a prompt string for token estimation."""
    parts: list[str] = []

    for msg in request_body.get("messages", []) or []:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role", "user"))
        content = _flatten_content(msg.get("content"))
        if content:
            parts.append(f"{role}: {content}")

    for tool in request_body.get("tools", []) or []:
        try:
            parts.append(json.dumps(tool, ensure_ascii=False, sort_keys=True))
        except TypeError:
            parts.append(str(tool))

    return "\n".join(parts)


def _extract_response_text(result: dict) -> str:
    """Extract assistant-visible text from a non-streaming response."""
    texts: list[str] = []
    for choice in result.get("choices", []) or []:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message") or {}
        if isinstance(message, dict):
            content = _flatten_content(message.get("content"))
            if content:
                texts.append(content)
            reasoning = message.get("reasoning") or message.get("reasoning_content")
            if reasoning:
                texts.append(str(reasoning))
    return "\n".join(texts)


def _extract_stream_text(chunk: dict) -> str:
    """Extract text/reasoning/tool-call deltas from a streaming chunk."""
    parts: list[str] = []
    for choice in chunk.get("choices", []) or []:
        if not isinstance(choice, dict):
            continue
        delta = choice.get("delta") or {}
        if not isinstance(delta, dict):
            continue
        for key in ("content", "reasoning", "reasoning_content"):
            value = delta.get(key)
            if value:
                parts.append(str(value))
        for tool_call in delta.get("tool_calls", []) or []:
            if not isinstance(tool_call, dict):
                continue
            fn = tool_call.get("function") or {}
            if isinstance(fn, dict):
                if fn.get("name"):
                    parts.append(str(fn["name"]))
                if fn.get("arguments"):
                    parts.append(str(fn["arguments"]))
    return "".join(parts)


def _estimate_token_count(model_name: str | None, text: str) -> int:
    """Estimate tokens when the upstream does not provide usage.

    Strategy:
    1. Try `tiktoken` when available.
    2. Fall back to a mixed heuristic for ASCII/CJK text.
    """
    if not text:
        return 0

    if model_name:
        try:
            import tiktoken  # type: ignore[import-not-found]

            try:
                encoder = tiktoken.encoding_for_model(model_name)
            except KeyError:
                encoder = tiktoken.get_encoding("cl100k_base")
            return len(encoder.encode(text))
        except Exception:
            pass

    cjk_chars = len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]", text))
    ascii_chars = sum(1 for ch in text if ord(ch) < 128 and not ch.isspace())
    other_chars = sum(
        1 for ch in text if not ch.isspace() and ord(ch) >= 128 and not re.match(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]", ch)
    )
    wordish = len(re.findall(r"[A-Za-z0-9_]+|[^\w\s]", text))
    heuristic = math.ceil((ascii_chars / 4.0) + (cjk_chars * 1.15) + (other_chars / 2.0))
    return max(wordish, heuristic, 1)


async def proxy_request(
    model: ModelRegistry,
    api_key_id: int,
    user_id: int,
    department_id: int | None,
    request_body: dict,
    endpoint_path: str,
    user_email: Optional[str] = None,
    inject_identity: bool = False,
) -> dict:
    """Forward request to model backend with exponential backoff retry."""
    timeout = _get_timeout(model.model_type)
    base_url = model.endpoint_url.rstrip("/")

    # Determine the correct path based on api_version
    if model.api_version == "v2" and "embedding" in endpoint_path:
        target_url = f"{base_url}/v2/embeddings"
    else:
        target_url = f"{base_url}{endpoint_path}"

    last_error = None
    start_time = time.time()

    req_headers = (
        _build_downstream_headers(user_id, user_email)
        if inject_identity
        else {"Content-Type": "application/json"}
    )

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
                last_error = f"後端回應 {response.status_code}: {response.text[:200]}"
                if attempt < settings.PROXY_MAX_RETRIES - 1:
                    delay = settings.PROXY_RETRY_BASE_DELAY * (2 ** attempt)
                    logger.warning(
                        f"模型 {model.name} 回應 {response.status_code}，"
                        f"{delay}s 後重試 ({attempt + 1}/{settings.PROXY_MAX_RETRIES})"
                    )
                    await asyncio.sleep(delay)
                    continue
                raise HTTPException(status_code=502, detail=f"模型服務錯誤: {last_error}")

            if response.status_code >= 400:
                raise HTTPException(
                    status_code=response.status_code,
                    detail=response.json() if response.headers.get("content-type", "").startswith("application/json") else response.text[:500],
                )

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

            # Enqueue usage record (non-blocking)
            await enqueue_usage(
                api_key_id=api_key_id,
                user_id=user_id,
                department_id=department_id,
                model_id=model.id,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                request_duration_ms=duration_ms,
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
    inject_identity: bool = False,
    model_name: str | None = None,
) -> AsyncIterator[str]:
    """Stream SSE response from a downstream backend through CSP proxy.

    Intercepts the final usage chunk to record token consumption, then
    forwards all SSE chunks verbatim to the caller. If usage is missing,
    performs a server-side token estimate from request/response text.
    """
    headers = (
        _build_downstream_headers(user_id, user_email)
        if inject_identity
        else {"Content-Type": "application/json"}
    )
    # Force stream_options so the downstream sends usage in last chunk
    body = {**request_body, "stream": True,
            "stream_options": {"include_usage": True}}

    start_time = time.time()
    prompt_tokens = completion_tokens = 0
    usage_seen = False
    prompt_text = _serialize_request_for_usage(body)
    completion_parts: list[str] = []

    try:
        async with httpx.AsyncClient(timeout=settings.LLM_TIMEOUT) as client:
            async with client.stream("POST", target_url, json=body, headers=headers) as resp:
                if resp.status_code >= 400:
                    raise HTTPException(status_code=resp.status_code,
                                        detail=f"下游回應錯誤: {resp.status_code}")
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    yield line + "\n\n"
                    # Parse usage from last chunk before [DONE]
                    if line.startswith("data: ") and line != "data: [DONE]":
                        try:
                            chunk = json.loads(line[6:])
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
        )
