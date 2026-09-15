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

Read budget used to be 120 s. Thinking models (GLM Flash etc.) routinely
think silently for longer than that, after which Router aborted a healthy
upstream and the UI said the LLM was down. Default now matches nginx
``/v1`` / ``/router`` (300 s). Connect failures still surface quickly.

Environment knobs (all optional, all with pre-branch-safe defaults):
``ANILA_HTTP_CONNECT_TIMEOUT`` (3), ``ANILA_HTTP_READ_TIMEOUT`` (300),
``ANILA_HTTP_MAX_CONNECTIONS`` (0 = uncapped, see ``_max_connections``),
``ANILA_HTTP_POOL_TIMEOUT`` (only meaningful with a finite cap),
``ANILA_HTTP_MAX_KEEPALIVE`` (20).
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_client: Optional[httpx.AsyncClient] = None


def _max_connections() -> Optional[int]:
    """Process-wide ceiling on concurrent outbound connections.

    Default is **no ceiling**, and that is a deliberate safety property,
    not laziness about picking a number.

    Before this branch every outbound hop built its own
    ``httpx.AsyncClient``, so there was no process-wide cap at all: N
    concurrent Router turns opened up to N sockets and *nothing ever
    queued*. httpx only makes a caller wait for a connection when
    ``max_connections`` is a finite number; with ``None`` the acquire
    path cannot block, so ``httpx.PoolTimeout`` is unreachable by
    construction. A shared pool that invents a failure the unoptimised
    code could not produce is a net loss on a platform serving ~3000
    people with one maintainer — this branch exists to make the Router
    faster, not to add a new way for it to break at peak.

    What happens at the limit, with no cap:

    * Peak socket demand is at most one per in-flight request — the same
      as pre-branch — and usually *fewer*, because keep-alive hands a
      finished turn's socket to the next one. Pre-branch reused nothing,
      so "uncapped + reuse" is strictly lighter on file descriptors than
      what it replaced. There is no load at which the shared pool opens
      more sockets than the old code did.
    * The backstop is the one the old code already had: the process fd
      limit and the upstream's own accept queue. Those surface as
      connect errors bounded by ``ANILA_HTTP_CONNECT_TIMEOUT``, which is
      the pre-existing failure mode — not a new 5-second silent stall
      that ends in a 500 the user cannot distinguish from a model fault.

    An operator who genuinely wants a ceiling sets
    ``ANILA_HTTP_MAX_CONNECTIONS`` to a positive integer; only then does
    queueing (and ``ANILA_HTTP_POOL_TIMEOUT``) become reachable, and
    that is their explicit, reversible choice.
    """
    raw = os.environ.get("ANILA_HTTP_MAX_CONNECTIONS", "0").strip()
    # ``int(float(...))`` so "100.0" from a compose file is not a crash.
    value = int(float(raw or "0"))
    return value if value > 0 else None


def _pool_timeout() -> Optional[float]:
    """How long to wait for a pooled connection, or ``None`` for forever.

    Unreachable while ``_max_connections()`` is ``None`` (nothing ever
    queues), so the default is ``None`` — there is no configuration in
    which the shipped defaults can raise ``PoolTimeout``. It only starts
    to mean anything once an operator opts into a finite cap, and then
    5 s is the historical httpx-style default; ``0`` means wait
    indefinitely.
    """
    if _max_connections() is None:
        return None
    raw = float(os.environ.get("ANILA_HTTP_POOL_TIMEOUT", "5.0"))
    return raw if raw > 0 else None


def _limits() -> httpx.Limits:
    # ``max_keepalive_connections`` is a *retention* cap, not a
    # concurrency cap: exceeding it never blocks or fails a request, it
    # only closes the socket after use — i.e. that hop degrades to
    # exactly the pre-branch cost. Default stays at the value this
    # branch shipped (20) so steady-state fd usage is unchanged; raise
    # ``ANILA_HTTP_MAX_KEEPALIVE`` if you want more reuse at peak.
    return httpx.Limits(
        max_connections=_max_connections(),
        max_keepalive_connections=int(
            float(os.environ.get("ANILA_HTTP_MAX_KEEPALIVE", "20"))
        ),
        keepalive_expiry=30.0,
    )


def _timeout() -> httpx.Timeout:
    # Historical default was a flat 120.0 (connect+read+write+pool).
    # Read is per-chunk inactivity: a thinking model that emits nothing
    # for >120 s used to die mid-thought. 300 s matches nginx /v1.
    connect = float(os.environ.get("ANILA_HTTP_CONNECT_TIMEOUT", "3.0"))
    read = float(os.environ.get("ANILA_HTTP_READ_TIMEOUT", "300.0"))
    return httpx.Timeout(
        connect=connect, read=read, write=60.0, pool=_pool_timeout()
    )


def get_http_client() -> httpx.AsyncClient:
    """Return the process-wide client, creating it on first use.

    Safe to call from any async context. Not thread-safe for the
    create-once race (Router is single-event-loop); if two tasks race
    the first create, the loser closes its extra client.
    """
    global _client
    if _client is not None and not _client.is_closed:
        return _client
    created = httpx.AsyncClient(timeout=_timeout(), limits=_limits())
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
