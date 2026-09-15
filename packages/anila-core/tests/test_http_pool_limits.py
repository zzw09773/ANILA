"""Shared HTTP pool must not manufacture failures the old code could not.

Before OPT-1 every outbound hop built its own ``httpx.AsyncClient``, so
there was no process-wide connection cap and nothing ever queued. The
first cut of ``http_pool`` shipped ``max_connections=100`` with a 5 s
``pool`` timeout, which at peak turns the 101st concurrent Router turn
into an ``httpx.PoolTimeout`` — a failure mode the unoptimised code was
structurally incapable of producing.

Mutation notes are on each test: the exact one-line production edit that
turns it red.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from anila_core import http_pool
from anila_core.http_pool import (
    aclose_http_client,
    get_http_client,
    reset_http_client,
)

# One more than the old ``max_connections=100`` — the smallest load that
# the old defaults would have queued and then failed.
CONCURRENCY = 101

_POOL_ENV = (
    "ANILA_HTTP_MAX_CONNECTIONS",
    "ANILA_HTTP_POOL_TIMEOUT",
    "ANILA_HTTP_MAX_KEEPALIVE",
)


@pytest.fixture(autouse=True)
def _clean_pool_env(monkeypatch):
    """Shipped defaults, not whatever the developer's shell exports."""
    for name in _POOL_ENV:
        monkeypatch.delenv(name, raising=False)
    reset_http_client()
    yield
    reset_http_client()


def test_pool_defaults_cannot_manufacture_a_pool_timeout() -> None:
    """No cap => httpx never queues => ``PoolTimeout`` is unreachable.

    Mutant (revert): in ``http_pool._limits`` set
    ``max_connections=100`` (or in ``_timeout`` set ``pool=5.0``) — the
    corresponding assertion below fails.
    """
    limits = http_pool._limits()
    assert limits.max_connections is None, (
        "a finite process-wide cap re-introduces pool queueing, which the "
        "pre-branch per-call clients could not do"
    )
    assert http_pool._timeout().pool is None, (
        "a pool-acquire deadline is only meaningful behind a finite cap"
    )
    assert http_pool._timeout().read >= 300, (
        "read timeout must outlast a thinking-model pause; 120s aborted healthy streams"
    )


def test_pool_limits_are_env_overridable(monkeypatch) -> None:
    """Operators can opt into a ceiling without a code change.

    Mutant (revert): hard-code ``max_connections=100`` /
    ``max_keepalive_connections=20`` in ``_limits`` and ``pool=5.0`` in
    ``_timeout`` — the env values below are ignored and this fails.
    """
    monkeypatch.setenv("ANILA_HTTP_MAX_CONNECTIONS", "7")
    monkeypatch.setenv("ANILA_HTTP_POOL_TIMEOUT", "1.5")
    monkeypatch.setenv("ANILA_HTTP_MAX_KEEPALIVE", "3")

    limits = http_pool._limits()
    assert limits.max_connections == 7
    assert limits.max_keepalive_connections == 3
    assert http_pool._timeout().pool == 1.5

    # A finite cap with an explicit "wait, do not fail" choice.
    monkeypatch.setenv("ANILA_HTTP_POOL_TIMEOUT", "0")
    assert http_pool._timeout().pool is None


@pytest.mark.asyncio
async def test_shared_pool_does_not_queue_at_the_old_cap() -> None:
    """101 simultaneous hops all reach the upstream; none is queued.

    Behavioural, not a constant check: a real TCP listener holds every
    connection open until the test releases it, so the assertion is that
    the pool actually granted ``CONCURRENCY`` sockets at once.

    Mutant (revert): in ``http_pool._limits`` set
    ``max_connections=100`` — only 100 requests reach the listener, the
    101st sits in the pool queue, and (with ``pool=5.0`` restored in
    ``_timeout``) fails with ``httpx.PoolTimeout``.
    """
    arrived = asyncio.Event()
    release = asyncio.Event()
    seen = 0

    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal seen
        try:
            await reader.readuntil(b"\r\n\r\n")
        except Exception:  # pragma: no cover — client went away
            writer.close()
            return
        seen += 1
        if seen >= CONCURRENCY:
            arrived.set()
        await release.wait()
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"
        )
        try:
            await writer.drain()
        except Exception:  # pragma: no cover
            pass
        writer.close()

    server = await asyncio.start_server(_handle, "127.0.0.1", 0, backlog=256)
    port = server.sockets[0].getsockname()[1]
    url = f"http://127.0.0.1:{port}/"

    client = get_http_client()
    tasks = [
        asyncio.create_task(client.get(url)) for _ in range(CONCURRENCY)
    ]
    try:
        try:
            await asyncio.wait_for(arrived.wait(), timeout=15.0)
            all_arrived = True
        except asyncio.TimeoutError:
            all_arrived = False
        # Snapshot before releasing: afterwards the queued request drains
        # through and ``seen`` would misleadingly read CONCURRENCY.
        seen_at_deadline = seen
        release.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        server.close()
        await server.wait_closed()
        await aclose_http_client()

    assert all_arrived, (
        f"only {seen_at_deadline} of {CONCURRENCY} concurrent requests reached the "
        "upstream — the shared pool queued the rest, which the pre-branch "
        "per-call clients never did"
    )
    pool_timeouts = [r for r in results if isinstance(r, httpx.PoolTimeout)]
    assert not pool_timeouts, (
        f"{len(pool_timeouts)} request(s) failed with PoolTimeout, a failure "
        "the unoptimised code could not produce"
    )
    assert all(
        isinstance(r, httpx.Response) and r.status_code == 200 for r in results
    ), [r for r in results if not isinstance(r, httpx.Response)]
