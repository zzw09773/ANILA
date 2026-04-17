"""Dispatch tool — sends a query to a registered agent via CSP proxy.

The Router never calls agent endpoints directly. All dispatch goes through
CSP POST /v1/chat/completions with model=<agent_id>, so CSP can enforce
permissions and record usage.
"""

from __future__ import annotations

import json
import logging
from typing import AsyncIterator, Optional

import httpx

logger = logging.getLogger(__name__)


async def dispatch_to_agent(
    agent_id: str,
    query: str,
    csp_base_url: str,
    csp_api_key: str,
    stream: bool = False,
    system_prompt: Optional[str] = None,
    timeout: float = 120.0,
) -> str:
    """Call a registered agent through CSP and return the full response text.

    Args:
        agent_id: The agent's ID as registered in CSP (used as `model` field).
        query: The user query to forward.
        csp_base_url: myCSPPlatform base URL.
        csp_api_key: CSP API Key for the current user session.
        stream: Whether to use SSE streaming internally (collected into str).
        system_prompt: Optional system message.
        timeout: HTTP timeout in seconds.

    Returns:
        The agent's response as a plain string.
    """
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": query})

    payload = {
        "model": agent_id,
        "messages": messages,
        "stream": stream,
    }
    headers = {
        "Authorization": f"Bearer {csp_api_key}",
        "Content-Type": "application/json",
    }
    url = f"{csp_base_url.rstrip('/')}/v1/chat/completions"

    async with httpx.AsyncClient(timeout=timeout) as client:
        if stream:
            return await _collect_stream(client, url, payload, headers)
        else:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]


async def _collect_stream(
    client: httpx.AsyncClient,
    url: str,
    payload: dict,
    headers: dict,
) -> str:
    content = ""
    async with client.stream("POST", url, json=payload, headers=headers) as resp:
        resp.raise_for_status()
        async for line in resp.aiter_lines():
            line = line.strip()
            if not line or not line.startswith("data: "):
                continue
            data_str = line[6:]
            if data_str == "[DONE]":
                break
            try:
                chunk = json.loads(data_str)
                delta = chunk["choices"][0].get("delta", {})
                content += delta.get("content") or ""
            except (json.JSONDecodeError, KeyError, IndexError):
                continue
    return content
