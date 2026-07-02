"""Tests for ``app.services.revocation_cache``.

The revocation cache is the consumer end of the csp → anila-studio
token-revoke pipeline (see ``services/csp/.../token_revocation_publisher.py``
for the publisher side).

Contract pinned by these tests:

* ``is_revoked(user_id, token_version)`` returns True iff the cached
  ``revoked_at_version`` for that user is ``>=`` the supplied
  ``token_version``. The ``>=`` boundary matters — csp bumps
  ``token_version`` to N to invalidate everything signed at N-1 or
  before, AND the JWT carrying tv=N itself (because the bump happens
  pre-issuance for hard revoke flows).

* Cache misses are ``False`` (no revocation on record).

* ``start(app)`` is a strict cold-start sync gate:
  - connects Redis (raises on failure)
  - fetches ``GET ${CSP_BASE_URL}/api/auth/revocations?since=<now-30d>``
    (raises on failure → main.py decides whether to start the service)
  - spawns the subscriber background task
  - sets ``_ready = True``

* Once subscribing, Redis connection loss flips ``ready`` back to
  False. The subscriber attempts exponential backoff reconnect up
  to 60s and re-runs the cold-start sync on recovery. fail-closed
  is enforced by callers (auth.py / health) reading ``.ready``;
  this module deliberately does NOT raise on disconnect.

* TTL: 30 days. After expiry, ``is_revoked`` falls through to
  False because the underlying access token has expired naturally.

* schema_version != 1 events log a warning but still update the
  cache (forward-compat — we trust the user_id / revoked_at_version
  fields are stable).
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import fakeredis.aioredis
import httpx
import pytest
import respx
from freezegun import freeze_time

from app.config import settings
from app.services.revocation_cache import (
    RevocationCache,
    get_revocation_cache,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_redis_server() -> fakeredis.aioredis.FakeServer:
    """A shared FakeServer so publisher + subscriber see the same channel."""
    return fakeredis.aioredis.FakeServer()


@pytest.fixture
def fake_redis_factory(fake_redis_server, monkeypatch):
    """Patch ``redis.asyncio.from_url`` to hand out FakeRedis instances
    bound to the shared ``fake_redis_server``.

    Tests that want to simulate Redis connection failure can monkeypatch
    this further (or use a different factory).
    """

    def _factory(url: str, **kwargs: Any):
        # ``decode_responses`` is the only kwarg we care about — fakeredis
        # honours it the same way real redis-py does.
        return fakeredis.aioredis.FakeRedis(
            server=fake_redis_server,
            decode_responses=kwargs.get("decode_responses", True),
        )

    # Patch BOTH the canonical module and the local binding inside
    # ``revocation_cache`` (it imports from_url at module load time, so
    # any local alias needs the same replacement).
    monkeypatch.setattr("redis.asyncio.from_url", _factory)
    from app.services import revocation_cache as rc_module

    monkeypatch.setattr(rc_module, "_from_url", _factory, raising=False)
    return _factory


@pytest.fixture
async def empty_revocations_endpoint(respx_mock):
    """Mock csp ``GET /api/auth/revocations`` returning no revocations."""
    respx_mock.get(f"{settings.CSP_BASE_URL}/api/auth/revocations").mock(
        return_value=httpx.Response(
            200,
            json={"revocations": [], "retention_days": 30},
        )
    )
    return respx_mock


@pytest.fixture
def respx_mock():
    """respx context manager re-exposed as a fixture for ergonomics."""
    with respx.mock(assert_all_called=False) as mock:
        yield mock


@pytest.fixture
async def started_cache(fake_redis_factory, empty_revocations_endpoint):
    """A RevocationCache already through ``start()``; tests get a
    ready-to-use instance and ``stop()`` is run on teardown.
    """
    cache = RevocationCache()
    await cache.start(app=None)
    try:
        yield cache
    finally:
        await cache.stop(app=None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _publish(server, payload: dict, channel: str | None = None) -> None:
    """Publish a revocation event onto the shared fake server."""
    channel = channel or settings.REDIS_REVOCATION_CHANNEL
    publisher = fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)
    try:
        await publisher.publish(channel, json.dumps(payload))
    finally:
        await publisher.aclose()


async def _wait_until(predicate, *, timeout: float = 2.0, interval: float = 0.01) -> bool:
    """Poll ``predicate`` until True or timeout. Returns whether the
    predicate fired before the deadline."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return False


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_publish_event_updates_cache(
    fake_redis_server, started_cache
):
    """A publish on the channel should land in the cache so the next
    ``is_revoked`` call reflects it."""
    user_id = 42
    revoked_at_version = 7

    await _publish(
        fake_redis_server,
        {
            "user_id": user_id,
            "revoked_at_version": revoked_at_version,
            "ts": datetime.now(timezone.utc).isoformat(),
            "schema_version": 1,
        },
    )

    fired = await _wait_until(
        lambda: started_cache._cache.get(user_id) == revoked_at_version
    )
    assert fired, "subscriber never picked up the published event"

    # Now is_revoked must reflect the >= rule.
    assert await started_cache.is_revoked(user_id, token_version=6) is True
    assert await started_cache.is_revoked(user_id, token_version=7) is True
    assert await started_cache.is_revoked(user_id, token_version=8) is False


async def test_cache_miss_returns_false(started_cache):
    """Users never revoked produce cache misses → not revoked."""
    assert await started_cache.is_revoked(999, token_version=5) is False


# ---------------------------------------------------------------------------
# Cold-start sync
# ---------------------------------------------------------------------------


async def test_cold_start_seeds_cache_from_csp(fake_redis_factory, respx_mock):
    """``start()`` must hit ``GET /api/auth/revocations`` and seed cache
    with the rows returned, so subscribers booting AFTER a publish
    don't permanently miss the event."""
    now = datetime.now(timezone.utc)
    respx_mock.get(f"{settings.CSP_BASE_URL}/api/auth/revocations").mock(
        return_value=httpx.Response(
            200,
            json={
                "revocations": [
                    {"user_id": 1, "revoked_at_version": 3, "ts": now.isoformat()},
                    {"user_id": 2, "revoked_at_version": 5, "ts": now.isoformat()},
                    {"user_id": 3, "revoked_at_version": 1, "ts": now.isoformat()},
                ],
                "retention_days": 30,
            },
        )
    )

    cache = RevocationCache()
    await cache.start(app=None)
    try:
        assert cache.ready is True
        assert await cache.is_revoked(1, token_version=3) is True
        assert await cache.is_revoked(2, token_version=4) is True
        assert await cache.is_revoked(2, token_version=6) is False
        assert await cache.is_revoked(3, token_version=1) is True
        # User not in the cold-start set → not revoked.
        assert await cache.is_revoked(4, token_version=99) is False
    finally:
        await cache.stop(app=None)


async def test_cold_start_failure_raises(fake_redis_factory, respx_mock):
    """If csp ``/revocations`` returns 5xx during cold start, ``start()``
    must raise — the caller (main.py lifespan) is the one allowed to
    decide whether the service comes up at all.
    """
    respx_mock.get(f"{settings.CSP_BASE_URL}/api/auth/revocations").mock(
        return_value=httpx.Response(503)
    )

    cache = RevocationCache()
    with pytest.raises(Exception):
        await cache.start(app=None)
    # Even on failure, ready must still be False.
    assert cache.ready is False
    # Best-effort stop — should not raise even though start() didn't
    # finish wiring everything.
    await cache.stop(app=None)


# ---------------------------------------------------------------------------
# >= boundary
# ---------------------------------------------------------------------------


async def test_revoked_at_version_inclusive_boundary(started_cache, fake_redis_server):
    """Revocation @ version=5 must block v4, v5 AND let v6 through."""
    await _publish(
        fake_redis_server,
        {
            "user_id": 100,
            "revoked_at_version": 5,
            "ts": datetime.now(timezone.utc).isoformat(),
            "schema_version": 1,
        },
    )
    fired = await _wait_until(lambda: started_cache._cache.get(100) == 5)
    assert fired

    assert await started_cache.is_revoked(100, token_version=4) is True
    assert await started_cache.is_revoked(100, token_version=5) is True
    assert await started_cache.is_revoked(100, token_version=6) is False


# ---------------------------------------------------------------------------
# TTL expiry
# ---------------------------------------------------------------------------


async def test_ttl_expires_after_30_days(fake_redis_factory, empty_revocations_endpoint):
    """After the 30-day TTL elapses, the entry vanishes and the user is
    no longer treated as revoked (their JWT has expired naturally)."""
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)

    with freeze_time(start) as frozen:
        cache = RevocationCache()
        await cache.start(app=None)
        try:
            # Force a value into the cache directly — we're testing TTL
            # semantics, not the subscriber path.
            cache._cache[123] = 9
            assert await cache.is_revoked(123, token_version=9) is True

            # Just inside the TTL: still revoked.
            frozen.tick(timedelta(seconds=settings.REVOCATION_CACHE_TTL_SECONDS - 60))
            assert await cache.is_revoked(123, token_version=9) is True

            # Past the TTL: cache must miss → not revoked.
            frozen.tick(timedelta(seconds=120))
            assert await cache.is_revoked(123, token_version=9) is False
        finally:
            await cache.stop(app=None)


# ---------------------------------------------------------------------------
# Out-of-order publish: keep the MAX version
# ---------------------------------------------------------------------------


async def test_out_of_order_publish_keeps_max(started_cache, fake_redis_server):
    """Pub/sub doesn't guarantee delivery order across reconnects.
    If we receive v3 then a stale v2, the cache must still report v3 —
    older revocations cannot un-revoke newer ones."""
    uid = 200

    # v3 first.
    await _publish(
        fake_redis_server,
        {
            "user_id": uid,
            "revoked_at_version": 3,
            "ts": datetime.now(timezone.utc).isoformat(),
            "schema_version": 1,
        },
    )
    fired = await _wait_until(lambda: started_cache._cache.get(uid) == 3)
    assert fired

    # Then a stale v2.
    await _publish(
        fake_redis_server,
        {
            "user_id": uid,
            "revoked_at_version": 2,
            "ts": datetime.now(timezone.utc).isoformat(),
            "schema_version": 1,
        },
    )
    # Give the subscriber a moment to consume the second message.
    await asyncio.sleep(0.05)

    # Cache must still hold the max.
    assert started_cache._cache.get(uid) == 3
    assert await started_cache.is_revoked(uid, token_version=3) is True


# ---------------------------------------------------------------------------
# Redis disconnect → fail-closed (_ready = False)
# ---------------------------------------------------------------------------


async def test_redis_disconnect_flips_ready_false(
    fake_redis_factory, empty_revocations_endpoint, monkeypatch
):
    """When the subscriber loses its Redis connection, ``ready`` must
    flip to False so callers (auth.py / health) can fail-closed."""
    cache = RevocationCache()
    # Stretch the backoff so the test isn't racing the reconnect loop.
    cache._reconnect_initial_delay = 60.0
    cache._reconnect_max_delay = 60.0
    await cache.start(app=None)
    try:
        assert cache.ready is True

        # Simulate a connection drop by closing the active pubsub from
        # under the subscriber loop. ``listen()`` will raise and the
        # subscriber should set _ready = False before backoff.
        pubsub = cache._pubsub
        assert pubsub is not None
        await pubsub.aclose()

        fired = await _wait_until(lambda: cache.ready is False, timeout=3.0)
        assert fired, "subscriber did not flip ready=False on connection loss"
    finally:
        await cache.stop(app=None)


# ---------------------------------------------------------------------------
# schema_version != 1 → log warning, still update cache
# ---------------------------------------------------------------------------


async def test_unknown_schema_version_logs_warning_but_updates(
    started_cache, fake_redis_server, caplog
):
    """Forward-compatibility: a newer schema_version should NOT cause
    the subscriber to drop the event. We log a warning and trust the
    stable fields (user_id, revoked_at_version)."""
    uid = 300
    with caplog.at_level(logging.WARNING):
        await _publish(
            fake_redis_server,
            {
                "user_id": uid,
                "revoked_at_version": 11,
                "ts": datetime.now(timezone.utc).isoformat(),
                "schema_version": 99,
            },
        )
        fired = await _wait_until(lambda: started_cache._cache.get(uid) == 11)
        assert fired

    assert await started_cache.is_revoked(uid, token_version=11) is True
    # A warning was emitted somewhere mentioning schema_version.
    assert any(
        "schema_version" in record.getMessage().lower()
        for record in caplog.records
        if record.levelno >= logging.WARNING
    ), "expected a schema_version warning in logs"


# ---------------------------------------------------------------------------
# Lifecycle: start/stop cycle
# ---------------------------------------------------------------------------


async def test_start_stop_cycle(fake_redis_factory, empty_revocations_endpoint):
    """Full lifecycle: start sets ready, stop cancels the subscriber
    and closes redis cleanly."""
    cache = RevocationCache()
    await cache.start(app=None)
    assert cache.ready is True
    assert cache._subscriber_task is not None
    assert cache._subscriber_task.done() is False

    await cache.stop(app=None)
    assert cache.ready is False
    # Background task must be done (cancelled or returned) after stop.
    assert cache._subscriber_task is None or cache._subscriber_task.done()


# ---------------------------------------------------------------------------
# Module-level singleton + factory
# ---------------------------------------------------------------------------


def test_get_revocation_cache_returns_singleton():
    """The factory must return the same instance across calls so DI
    inside FastAPI dependencies behaves predictably."""
    a = get_revocation_cache()
    b = get_revocation_cache()
    assert a is b
    assert isinstance(a, RevocationCache)
