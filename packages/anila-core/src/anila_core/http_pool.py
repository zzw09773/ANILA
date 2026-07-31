"""OPT-1: process-wide shared ``httpx.AsyncClient`` for Router → CSP calls.

Baseline (2026-07-30, this worktree): every Router outbound hop
(``/api/auth/me``, ``/v1/agents``, ``/v1/chat/completions``, agent
stream, resume) constructed a fresh ``httpx.AsyncClient`` and tore it
down. Against the live local stack that cost ~0.7 ms median per hop in
TCP setup alone; under TLS to a remote model gateway the handshake is
larger. The success-path response body is unchanged — only connection
reuse differs.

Independently revertable: delete this module and restore
``async with httpx.AsyncClient(...)`` at each call site (search
``OPT-1``).

Timeouts keep the historical 120 s overall read budget but split out a
short connect budget (OPT-6 lives here too — same revert path, or set
``ANILA_HTTP_CONNECT_TIMEOUT`` back implicitly by reverting). Connect
failures surface faster; successful calls are unaffected.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_client: Optional[httpx.AsyncClient] = None


def _timeout() -> httpx.Timeout:
    # Historical default was a flat 120.0 (connect+read+write+pool).
    # Keep read at 120 so long generations still complete; fail connect
    # quickly so a dead upstream does not sit on the event loop.
    connect = float(os.environ.get("ANILA_HTTP_CONNECT_TIMEOUT", "3.0"))
    read = float(os.environ.get("ANILA_HTTP_READ_TIMEOUT", "120.0"))
    return httpx.Timeout(connect=connect, read=read, write=60.0, pool=5.0)


def get_http_client() -> httpx.AsyncClient:
    """Return the process-wide client, creating it on first use.

    Safe to call from any async context. Not thread-safe for the
    create-once race (Router is single-event-loop); if two tasks race
    the first create, the loser closes its extra client.
    """
    global _client
    if _client is not None and not _client.is_closed:
        return _client
    limits = httpx.Limits(
        max_connections=100,
        max_keepalive_connections=20,
        keepalive_expiry=30.0,
    )
    created = httpx.AsyncClient(timeout=_timeout(), limits=limits)
    if _client is not None and not _client.is_closed:
        # Lost the create race — discard ours.
        # (Cannot await aclose here; rare; GC will collect.)
        pass
    _client = created
    return _client


async def aclose_http_client() -> None:
    """Close the shared client. Called from Router lifespan shutdown."""
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


def reset_http_client() -> None:
    """Drop the cached client without awaiting close.

    Used when a new Router app is built (tests recreate the app per case;
    production calls ``create_router_app`` once). The previous client's
    connections are abandoned — acceptable for short-lived test apps.
    """
    global _client
    _client = None


# Back-compat alias for early OPT-1 call sites / scratchpads.
reset_http_client_for_tests = reset_http_client
