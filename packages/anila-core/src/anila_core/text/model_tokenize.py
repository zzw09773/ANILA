"""Optional model ``/tokenize`` client for compact thresholds.

Fails closed. anila-core stays tiktoken-free. Router compact only calls
``tokenize_prompt`` when ``ANILA_TOKENIZE_URL`` is set.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from ..security.url_guard import ENDPOINT_KIND_MODEL, UnsafeEndpointError, validate_outbound_url
from .token_count import derive_tokenize_urls, parse_tokenize_response

logger = logging.getLogger(__name__)

_TOKENIZE_TIMEOUT_S = 2.0


async def tokenize_prompt(
    client: httpx.AsyncClient,
    url: str,
    prompt: str,
    *,
    timeout: float = _TOKENIZE_TIMEOUT_S,
) -> int | None:
    """POST ``{prompt}`` then ``{inputs}``. None on any failure."""
    if not prompt or not url:
        return None
    try:
        validate_outbound_url(url, endpoint_kind=ENDPOINT_KIND_MODEL)
    except UnsafeEndpointError:
        return None
    for body in ({"prompt": prompt}, {"inputs": prompt}):
        try:
            response = await client.post(url, json=body, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 — never break compact
            logger.debug("tokenize POST failed (%s): %s", type(exc).__name__, exc)
            return None
        if response.status_code >= 400:
            continue
        try:
            payload: Any = response.json()
        except Exception:
            continue
        counted = parse_tokenize_response(payload)
        if counted is not None:
            return counted
    return None


async def probe_tokenize_url(
    client: httpx.AsyncClient,
    endpoint_url: str,
    *,
    timeout: float = _TOKENIZE_TIMEOUT_S,
) -> str | None:
    """Return the first working tokenize URL, or None.

    For CSP / ops only. Router must not point this at
    ``router-primary.endpoint_url`` — that would talk to the model host.
    Compact opt-in is ``ANILA_TOKENIZE_URL`` via :func:`tokenize_prompt`.
    """
    for url in derive_tokenize_urls(endpoint_url):
        counted = await tokenize_prompt(client, url, "ping", timeout=timeout)
        if counted is not None:
            return url
    return None
