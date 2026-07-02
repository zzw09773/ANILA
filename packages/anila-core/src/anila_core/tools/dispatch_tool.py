"""Dispatch tool — sends a query to a registered agent via CSP proxy.

The Router never calls agent endpoints directly. All dispatch goes through
CSP POST /v1/chat/completions with model=<agent_id>, so CSP can enforce
permissions and record usage.

Sprint 10 PR 2: dispatch is now **stateful**. Two new optional params
on every entrypoint:

- ``context_messages`` — pre-built ``[{role, content}, …]`` to insert
  before the user query so the target agent sees prior turns. Use with
  the filters in :mod:`anila_core.engine.handoff` (``LastNFilter``,
  ``SummaryFilter``) to keep the prompt budget under control.
- ``session_id`` — embedded in the request body as the openai-extension
  field ``anila_session_id``. Agent servers that recognise this field
  attach the same Session adapter so the dispatched turn lands in the
  right conversation history (and pause-resume keeps working across
  the dispatch boundary). CSP forwards the body verbatim.

A ``handoff_meta`` field is also embedded under ``anila_handoff`` for
agents that want to know they're being entered via handoff rather than
a fresh dispatch (e.g. to render a banner or skip "hello" boilerplate).

Convenience: :func:`dispatch_for_handoff` takes the
:class:`HandoffRequest` directly and unpacks it into the right
parameters — that's what the Router calls when it catches
:class:`RunHandoff`.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

import httpx

from ..models.handoff import HandoffRequest

logger = logging.getLogger(__name__)


async def dispatch_to_agent(
    agent_id: str,
    query: str,
    csp_base_url: str,
    csp_api_key: str,
    stream: bool = False,
    system_prompt: Optional[str] = None,
    timeout: float = 120.0,
    *,
    context_messages: Optional[list[dict[str, Any]]] = None,
    session_id: Optional[str] = None,
    handoff_meta: Optional[dict[str, Any]] = None,
) -> str:
    """Call a registered agent through CSP and return the full response text.

    Convenience wrapper around :func:`dispatch_to_agent_response` that
    discards everything but ``content``.

    See module docstring for ``context_messages`` / ``session_id`` /
    ``handoff_meta`` semantics.
    """
    response = await dispatch_to_agent_response(
        agent_id=agent_id,
        query=query,
        csp_base_url=csp_base_url,
        csp_api_key=csp_api_key,
        stream=stream,
        system_prompt=system_prompt,
        timeout=timeout,
        context_messages=context_messages,
        session_id=session_id,
        handoff_meta=handoff_meta,
    )
    content = response["content"]
    return str(content) if content is not None else ""


async def dispatch_to_agent_response(
    agent_id: str,
    query: str,
    csp_base_url: str,
    csp_api_key: str,
    stream: bool = False,
    system_prompt: Optional[str] = None,
    timeout: float = 120.0,
    *,
    context_messages: Optional[list[dict[str, Any]]] = None,
    session_id: Optional[str] = None,
    handoff_meta: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Call a registered agent through CSP and return content + metadata.

    Returns a dict with ``content`` (the agent's reply text), ``anila_meta``
    (passthrough from the agent's response if present), and ``raw`` (the
    upstream JSON for debugging on the non-stream path).
    """
    payload = _build_payload(
        agent_id=agent_id,
        query=query,
        stream=stream,
        system_prompt=system_prompt,
        context_messages=context_messages,
        session_id=session_id,
        handoff_meta=handoff_meta,
    )
    headers = {
        "Authorization": f"Bearer {csp_api_key}",
        "Content-Type": "application/json",
    }
    url = f"{csp_base_url.rstrip('/')}/v1/chat/completions"

    async with httpx.AsyncClient(timeout=timeout) as client:
        if stream:
            return await _collect_stream_response(client, url, payload, headers)
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        return {
            "content": data["choices"][0]["message"]["content"],
            "anila_meta": data.get("anila_meta"),
            "raw": data,
        }


async def dispatch_for_handoff(
    request: HandoffRequest,
    *,
    csp_base_url: str,
    csp_api_key: str,
    session_id: Optional[str] = None,
    stream: bool = False,
    system_prompt: Optional[str] = None,
    timeout: float = 120.0,
) -> dict[str, Any]:
    """Dispatch a :class:`HandoffRequest` to its target agent.

    The Router catches :class:`RunHandoff` and calls this — it unpacks
    ``request.target_agent_id`` / ``request.message`` /
    ``request.context_messages`` and embeds ``request.metadata`` +
    ``request.reason`` under ``anila_handoff`` so the target can render
    a "handed off from agent X because Y" banner.
    """
    handoff_meta: dict[str, Any] = {
        "handoff_id": request.id,
        "metadata": request.metadata,
    }
    if request.reason:
        handoff_meta["reason"] = request.reason
    return await dispatch_to_agent_response(
        agent_id=request.target_agent_id,
        query=request.message,
        csp_base_url=csp_base_url,
        csp_api_key=csp_api_key,
        stream=stream,
        system_prompt=system_prompt,
        timeout=timeout,
        context_messages=request.context_messages or None,
        session_id=session_id,
        handoff_meta=handoff_meta,
    )


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _build_payload(
    *,
    agent_id: str,
    query: str,
    stream: bool,
    system_prompt: Optional[str],
    context_messages: Optional[list[dict[str, Any]]],
    session_id: Optional[str],
    handoff_meta: Optional[dict[str, Any]],
) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    if context_messages:
        messages.extend(context_messages)
    messages.append({"role": "user", "content": query})

    payload: dict[str, Any] = {
        "model": agent_id,
        "messages": messages,
        "stream": stream,
    }
    # ANILA-extension fields. CSP forwards the body verbatim; agents
    # that recognise them attach the right Session / render handoff UI.
    # Agents that don't recognise them ignore — extra JSON keys are
    # harmless under OpenAI's spec.
    if session_id:
        payload["anila_session_id"] = session_id
    if handoff_meta:
        payload["anila_handoff"] = handoff_meta
    return payload


async def _collect_stream_response(
    client: httpx.AsyncClient,
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
) -> dict[str, Any]:
    content = ""
    anila_meta = None
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
                if chunk.get("anila_meta"):
                    anila_meta = chunk["anila_meta"]
            except (json.JSONDecodeError, KeyError, IndexError):
                continue
    return {"content": content, "anila_meta": anila_meta, "raw": None}
