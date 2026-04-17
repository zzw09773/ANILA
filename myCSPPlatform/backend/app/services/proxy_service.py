"""Proxy service: forward requests to model backends with retry + timeout."""
import asyncio
import json
import logging
import time
from typing import AsyncIterator, Optional
from fastapi import HTTPException
from fastapi.responses import StreamingResponse
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
    endpoint_url: str,
    api_key_id: int,
    user_id: int,
    department_id: int | None,
    agent_id: int,
    request_body: dict,
    user_email: Optional[str] = None,
) -> AsyncIterator[str]:
    """Stream SSE response from a downstream agent through CSP proxy.

    Intercepts the final usage chunk to record token consumption, then
    forwards all SSE chunks verbatim to the caller.
    """
    target_url = f"{endpoint_url.rstrip('/')}/v1/chat/completions"
    headers = _build_downstream_headers(user_id, user_email)
    # Force stream_options so the downstream sends usage in last chunk
    body = {**request_body, "stream": True,
            "stream_options": {"include_usage": True}}

    start_time = time.time()
    prompt_tokens = completion_tokens = 0

    try:
        async with httpx.AsyncClient(timeout=settings.LLM_TIMEOUT) as client:
            async with client.stream("POST", target_url, json=body, headers=headers) as resp:
                if resp.status_code >= 400:
                    raise HTTPException(status_code=resp.status_code,
                                        detail=f"Agent 回應錯誤: {resp.status_code}")
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    yield line + "\n\n"
                    # Parse usage from last chunk before [DONE]
                    if line.startswith("data: ") and line != "data: [DONE]":
                        try:
                            chunk = json.loads(line[6:])
                            usage = chunk.get("usage") or {}
                            if usage:
                                prompt_tokens = usage.get("prompt_tokens", 0)
                                completion_tokens = usage.get("completion_tokens", 0)
                        except (json.JSONDecodeError, KeyError):
                            pass
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Agent 請求逾時")
    except httpx.ConnectError:
        raise HTTPException(status_code=502, detail=f"無法連線到 agent {target_url}")

    duration_ms = int((time.time() - start_time) * 1000)
    total_tokens = prompt_tokens + completion_tokens
    if total_tokens > 0:
        await enqueue_usage(
            api_key_id=api_key_id,
            user_id=user_id,
            department_id=department_id,
            model_id=agent_id,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            request_duration_ms=duration_ms,
        )
