"""Cross-service JWT revocation cache.

Background
==========

anila-studio verifies CSP-signed JWTs by signature alone (no DB
lookup), so once a user's tokens are revoked on the csp side
(``users.token_version`` bump) we need an out-of-band channel to
tell anila-studio about it before the access-token expiry naturally
kills the session.

The pipeline:

* **csp/backend** (the source-of-truth) bumps ``users.token_version``,
  writes a row to ``token_revocations``, and publishes a JSON event
  on the Redis channel ``anila:auth:token-revoke``. See
  ``services/csp/app/services/token_revocation_publisher.py``.
* **anila-studio** (this module) first subscribes to the live channel, then
  replays the last 30 days through
  ``GET /api/auth/revocations?since=...``. Events arriving during replay queue
  on the subscribed connection, so there is no replay→subscribe loss window.

Why two paths
-------------

If we only used pub/sub, a freshly-deployed studio pod would have
an empty deny list and would happily honour every old token until
each one expired naturally. The HTTP replay closes that gap.

If we only used HTTP polling, the delay between revoke and effect
would be the poll interval (multi-second at best). Live pub/sub
keeps the propagation lag well under a second on a healthy network.
The periodic HTTP replay remains active while Redis is healthy, so a CSP
publish failure cannot leave a running subscriber stale forever; the formal
default bounds that fallback at five seconds.

fail-closed posture
===================

When Redis is unreachable (subscriber lost connection, broker down),
this module sets ``self._ready = False``. It does NOT raise on its
own — the decision of *what to do* when the cache is unhealthy lives
in callers:

* ``/health`` reads ``.ready`` to decide if the pod should accept
  traffic (Kubernetes / load-balancer cue).
* ``auth.py``'s JWT verifier reads ``.ready`` to decide whether to
  return 503 on every request (refuses to validate tokens with an
  unhealthy deny list — the "fail closed" half of R14).

Splitting "I'm unhealthy" (this module) from "what to do about it"
(callers) keeps the module testable without mocking out HTTP layers,
and lets policy evolve without rewriting cache internals.

Reconnect behaviour
-------------------

When the subscriber catches a connection error it:

1. Sets ``_ready = False`` immediately (callers fail-closed).
2. Waits ``_reconnect_delay`` seconds (exponential: 1, 2, 4, …, 60).
3. Re-opens the Redis connection.
4. Rebuilds the CSP HTTP client (close pooled connections so Docker DNS
   re-resolves after ``csp`` recreate) and re-runs cold-start sync.
5. Re-subscribes and flips ``_ready`` back to True.

A failed reconnect also rebuilds the HTTP client before the next backoff
retry. Cold-start failure during reconnect is treated like the original
disconnect — keep ``_ready = False`` and keep retrying. Never
silently "downgrade to TTL-only", which was the v1-plan R14 hole.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from cachetools import TTLCache
from redis.asyncio import Redis as AsyncRedis
from redis.asyncio import from_url as _from_url

from app.config import settings


logger = logging.getLogger(__name__)


# Module-level singleton (lazy). FastAPI dependencies grab this via
# ``get_revocation_cache()`` so the same instance lives across the
# request graph and the lifespan callbacks.
_singleton: "RevocationCache | None" = None


# Payload schema version we natively understand. Anything else gets a
# warning + best-effort cache write (forward-compat).
SUPPORTED_SCHEMA_VERSION = 2
_COMPATIBLE_SCHEMA_VERSIONS = {1, 2}


class RevocationPayloadError(RuntimeError):
    """A durable/live revocation row cannot be safely interpreted."""


def _digest_identifier(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _valid_digest(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


class RevocationCache:
    """In-memory JWT revocation deny-list, fed by csp.

    Lifecycle:
        * ``await cache.start(app)`` — connect Redis, replay last 30
          days from csp, kick off the subscriber, mark ``ready``.
        * ``await cache.is_revoked(user_id, token_version)`` — query
          path used by JWT verifier.
        * ``await cache.stop(app)`` — cancel subscriber, close Redis.

    Thread/concurrency safety:
        Single event-loop only. All public coroutines must be awaited
        from the same loop that owns the underlying Redis client.
    """

    def __init__(self) -> None:
        # ``revoked_at_version`` per user_id. Capped at 10k entries so
        # a runaway / hostile publisher can't OOM the pod. 10k is more
        # than the realistic concurrent-banned-user count for any
        # tenant we expect to serve.
        #
        # ``timer=time.time`` (not the default ``time.monotonic``)
        # lets ``freezegun`` advance the cache clock in unit tests.
        # Wall-clock skew between this process and Redis is fine —
        # TTL is anchored to the moment WE wrote the entry, not to
        # anyone else's notion of time.
        self._cache: TTLCache[int, int] = TTLCache(
            maxsize=10_000,
            ttl=settings.REVOCATION_CACHE_TTL_SECONDS,
            timer=time.time,
        )
        self._jti_cache: TTLCache[str, bool] = TTLCache(
            maxsize=100_000,
            ttl=settings.REVOCATION_CACHE_TTL_SECONDS,
            timer=time.time,
        )
        self._sid_cache: TTLCache[str, bool] = TTLCache(
            maxsize=100_000,
            ttl=settings.REVOCATION_CACHE_TTL_SECONDS,
            timer=time.time,
        )

        # Readiness gate consumed by ``/health`` and ``auth.py``.
        # False until cold-start sync completes; flips back to False
        # whenever the subscriber loses Redis.
        self._ready: bool = False

        # Underlying handles, populated by ``start()``.
        self._redis: AsyncRedis | None = None
        self._pubsub: Any | None = None
        self._http: httpx.AsyncClient | None = None
        self._subscriber_task: asyncio.Task[None] | None = None
        self._last_sync_at: datetime | None = None

        # Stop signal for the subscriber loop. Set by ``stop()`` so the
        # background task knows we're winding down vs. handling a
        # transient connection error.
        self._stopping: asyncio.Event = asyncio.Event()

        # Reconnect backoff — exposed as attributes so tests can poke
        # them (default flow is slow on purpose so it doesn't spam an
        # outage scenario; tests using a slow backoff don't actually
        # need to wait through it).
        self._reconnect_initial_delay: float = 1.0
        self._reconnect_max_delay: float = 60.0

    def _build_http_client(self) -> httpx.AsyncClient:
        """Fresh httpx client — new pool forces Docker DNS re-resolution."""
        timeout = httpx.Timeout(
            settings.INTERNAL_TIMEOUT_SECONDS,
            connect=settings.INTERNAL_TIMEOUT_CONNECT,
        )
        return httpx.AsyncClient(timeout=timeout)

    async def _close_http_client(self) -> None:
        if self._http is None:
            return
        try:
            await self._http.aclose()
        except Exception:  # noqa: BLE001
            logger.debug("httpx aclose failed", exc_info=True)
        self._http = None

    async def _reset_http_client(self) -> httpx.AsyncClient:
        """Close any pooled client and open a new one (stale-IP heal)."""
        await self._close_http_client()
        self._http = self._build_http_client()
        return self._http

    async def _ensure_http_client(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = self._build_http_client()
        return self._http

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def ready(self) -> bool:
        """Read-only readiness probe.

        Consumed by ``/health`` to gate Kubernetes traffic, and by
        the JWT verifier to enforce fail-closed when Redis is down.
        """
        return self._ready

    async def is_revoked(
        self,
        user_id: int,
        token_version: int,
        *,
        jti: str | None = None,
        sid: str | None = None,
    ) -> bool:
        """True iff the JWT bearing ``(user_id, token_version)`` has
        been revoked by csp.

        Rule: cached ``revoked_at_version > token_version`` ⇒ revoked.
        CSP first bumps the user's durable version to N and records N as the
        revocation boundary. Tokens minted before the bump carry N-1 (or
        lower); legitimate tokens minted after it carry N and must remain
        usable.

        A cache miss means we have no record of revocation for that
        user, so the token is considered valid. (TTL expiry yields a
        miss too — by then the token has expired naturally.)
        """
        if jti and self._jti_cache.get(_digest_identifier(jti), False):
            return True
        if sid and self._sid_cache.get(_digest_identifier(sid), False):
            return True
        revoked_at_version = self._cache.get(user_id)
        return (
            revoked_at_version is not None
            and revoked_at_version > token_version
        )

    async def start(self, app: Any) -> None:
        """Cold-start sync + subscriber spinup, run once on lifespan.

        Steps, in order:
            1. Connect Redis. Raises if we can't even build the client.
            2. Subscribe so live events begin queueing.
            3. Pull the last 30 days of revocations from csp. Raises on HTTP
               error — callers (``main.py`` lifespan) decide whether the
               service starts at all.
            4. Spawn the subscriber background task.
            5. Mark ``_ready = True``.

        Raising on cold-start failure is deliberate: ``main.py``'s
        lifespan will let the exception bubble up, which marks the
        pod unhealthy and lets Kubernetes restart it from scratch.
        """
        # Reset the stop signal in case this instance is being
        # reused (mostly relevant in tests).
        self._stopping = asyncio.Event()

        # Step 1: build the Redis client. We delegate to a module-level
        # ``_from_url`` alias so tests can patch it; ``redis.asyncio``'s
        # own ``from_url`` is what we forward to in production.
        # Long-lived pub/sub: enable periodic PING + TCP keepalive so an idle
        # subscriber connection isn't silently torn down. Without this an idle
        # read could time out / drop, flipping ``_ready=False`` and 503-ing auth
        # until the reconnect+resync completes. Keeps fail-closed semantics
        # intact while removing the spurious idle-disconnect flapping.
        self._redis = _from_url(
            settings.REDIS_URL,
            decode_responses=True,
            health_check_interval=30,
            socket_keepalive=True,
        )

        # Step 2: subscribe *before* replay. Redis buffers live events on this
        # connection while the HTTP replay is in flight, closing the otherwise
        # unavoidable replay→subscribe loss window.
        self._pubsub = self._redis.pubsub()
        await self._pubsub.subscribe(settings.REDIS_REVOCATION_CHANNEL)

        # Long-lived HTTP client for CSP revocation replay. Rebuilt on
        # reconnect failure so Docker DNS re-resolves after csp recreate.
        await self._reset_http_client()

        # Step 3: cold-start sync. Any HTTP error escapes — main.py / the
        # lifespan owner decides whether the service comes up. Live events that
        # arrive during this request remain queued on ``self._pubsub``.
        await self._cold_start_sync()

        self._subscriber_task = asyncio.create_task(
            self._run_subscriber(),
            name="revocation-subscriber",
        )

        # Step 4: ready. Order matters — only flip to True AFTER the
        # subscriber is wired up so we never report ready while
        # actually deaf to publishes.
        self._ready = True
        logger.info(
            "revocation cache ready: %d entries from cold-start, subscribed to %r",
            len(self._cache),
            settings.REDIS_REVOCATION_CHANNEL,
        )

    async def stop(self, app: Any) -> None:
        """Graceful shutdown. Safe to call even if ``start()`` failed
        partway through — every step is best-effort and individually
        guarded.
        """
        self._ready = False
        self._stopping.set()

        # Cancel the subscriber task first; that lets it unwind from
        # inside ``listen()`` cleanly before we tear down the redis
        # connection it's reading from.
        if self._subscriber_task is not None and not self._subscriber_task.done():
            self._subscriber_task.cancel()
            try:
                await self._subscriber_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                # Either the task absorbed the cancel cleanly, or it
                # was already failing on its own — either way we're
                # tearing it down.
                pass

        # Close the pubsub channel (don't crash if it was never opened).
        if self._pubsub is not None:
            try:
                await self._pubsub.aclose()
            except Exception:  # noqa: BLE001
                logger.debug("pubsub aclose failed during stop", exc_info=True)
            self._pubsub = None

        # Finally close the Redis client.
        if self._redis is not None:
            try:
                await self._redis.aclose()
            except Exception:  # noqa: BLE001
                logger.debug("redis aclose failed during stop", exc_info=True)
            self._redis = None

        await self._close_http_client()

        self._subscriber_task = None

    # ------------------------------------------------------------------
    # Internal — cold-start sync
    # ------------------------------------------------------------------

    async def _cold_start_sync(self) -> None:
        """Replay the last 30 days of revocations from csp into the
        local cache. Called on ``start()`` AND after every reconnect
        so any gap during the outage is closed before we re-mark
        ready.
        """
        sync_started = datetime.now(timezone.utc)
        since = self._last_sync_at or (
            sync_started
            - timedelta(seconds=settings.REVOCATION_CACHE_TTL_SECONDS)
        )
        url = f"{settings.CSP_BASE_URL}/api/auth/revocations"
        params = {"since": since.isoformat()}
        headers: dict[str, str] = {}
        if settings.CSP_SERVICE_TOKEN:
            # csp's verify_service_token accepts this header (the
            # legacy env-var fallback path), no DB row needed.
            headers["X-CSP-Service-Token"] = settings.CSP_SERVICE_TOKEN

        client = await self._ensure_http_client()
        response = await client.get(url, params=params, headers=headers)
        response.raise_for_status()
        body = response.json()

        if not isinstance(body, dict):
            raise RevocationPayloadError("cold-start response is not an object")
        entries = body.get("revocations")
        if not isinstance(entries, list):
            raise RevocationPayloadError("cold-start revocations is not a list")
        for entry in entries:
            if not isinstance(entry, dict) or not self._apply_payload(entry):
                raise RevocationPayloadError(
                    f"malformed revocation row during cold-start: {entry!r}"
                )
        # Use the request start rather than completion as the next cursor. A
        # row committed while this request was in flight therefore remains in
        # the next inclusive query, closing the HTTP snapshot boundary.
        self._last_sync_at = sync_started

        logger.info(
            "cold-start sync pulled %d revocation rows from csp since %s",
            len(entries),
            since.isoformat(),
        )

    # ------------------------------------------------------------------
    # Internal — subscriber loop
    # ------------------------------------------------------------------

    async def _run_subscriber(self) -> None:
        """Background task: consume pub/sub events forever.

        On connection error: flip ``_ready=False``, wait with
        exponential backoff, re-open the connection, re-run cold-start
        sync, and resume. We exit only when ``_stopping`` is set
        (i.e., ``stop()`` was called).
        """
        delay = self._reconnect_initial_delay
        while not self._stopping.is_set():
            try:
                await self._consume_until_error()
                # ``_consume_until_error`` only returns on graceful
                # shutdown — if we're here without the stop signal,
                # treat as a disconnect and reconnect.
            except asyncio.CancelledError:
                # ``stop()`` cancelled us — propagate so we shut down.
                raise
            except Exception:  # noqa: BLE001
                logger.warning(
                    "revocation subscriber lost Redis connection; "
                    "will retry in %.1fs",
                    delay,
                    exc_info=True,
                )

            # Fail-closed: anyone querying ``ready`` between here and
            # the next successful resubscribe gets False.
            self._ready = False

            if self._stopping.is_set():
                break

            # Backoff sleep, but interruptible by ``stop()``.
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=delay)
                # ``wait()`` returned cleanly → stopping → exit.
                break
            except asyncio.TimeoutError:
                pass

            # Try to reconnect.
            try:
                await self._reconnect()
                # Reset backoff on success.
                delay = self._reconnect_initial_delay
                self._ready = True
                logger.info("revocation subscriber reconnected and re-synced")
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                # Reconnect failed → drop pooled HTTP client (stale CSP IP)
                # then bump backoff and loop.
                logger.warning(
                    "revocation subscriber reconnect failed; "
                    "will retry in %.1fs",
                    delay,
                    exc_info=True,
                )
                await self._reset_http_client()
                delay = min(delay * 2, self._reconnect_max_delay)

    async def _consume_until_error(self) -> None:
        """Pump pub/sub messages until a real connection error or stop.

        Uses ``get_message(timeout=...)`` rather than ``listen()``: an idle
        interval (no revocation event — the normal case) returns ``None`` so
        we just loop, and idleness never looks like a disconnect. A genuine
        connection failure still raises, propagating to ``_run_subscriber``
        for reconnect. This stops the cache from flapping ``_ready=False``
        (and 503-ing auth) merely because no token was revoked for a while —
        the previous ``listen()`` blocking read surfaced idle read-timeouts
        as disconnects.
        """
        assert self._pubsub is not None, "subscriber started before pubsub open"
        last_reconcile = time.monotonic()
        while not self._stopping.is_set():
            interval = max(
                0.1,
                float(settings.REVOCATION_RECONCILE_INTERVAL_SECONDS),
            )
            remaining = max(0.05, interval - (time.monotonic() - last_reconcile))
            message = await self._pubsub.get_message(
                ignore_subscribe_messages=True,
                timeout=min(1.0, remaining),
            )
            if message is not None and message.get("type") == "message":
                self._handle_message(message.get("data"))
            elif message is not None:
                # subscribe / unsubscribe confirmations and the like — ignore.
                pass
            if time.monotonic() - last_reconcile >= interval:
                # Durable polling is the acknowledgement fallback for Redis
                # pub/sub. Any HTTP/shape failure propagates to the outer loop,
                # flips readiness false, and triggers reconnect+replay.
                await self._cold_start_sync()
                last_reconcile = time.monotonic()

    def _handle_message(self, raw: Any) -> None:
        """Parse one Redis message payload and write to the cache.

        Defensive about every field — we'd rather drop a malformed
        event than blow up the subscriber loop, which would put us
        into fail-closed for everyone.
        """
        if raw is None:
            raise RevocationPayloadError("revocation event has no payload")
        # redis-py with decode_responses=True hands us a str; without
        # it, bytes. Handle both.
        if isinstance(raw, bytes):
            try:
                raw = raw.decode("utf-8")
            except UnicodeDecodeError:
                raise RevocationPayloadError(
                    "revocation event is not UTF-8"
                ) from None
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            raise RevocationPayloadError("revocation event is not JSON") from None
        if not isinstance(payload, dict):
            raise RevocationPayloadError("revocation event is not an object")

        schema_version = payload.get("schema_version")
        if schema_version not in _COMPATIBLE_SCHEMA_VERSIONS:
            # Forward compatibility: log a warning so ops notices a
            # csp upgrade, but trust the stable fields and write to
            # cache anyway.
            logger.warning(
                "revocation event: unknown schema_version=%r (supported=%d); "
                "applying anyway",
                schema_version,
                SUPPORTED_SCHEMA_VERSION,
            )

        if not self._apply_payload(payload):
            raise RevocationPayloadError("revocation event payload is malformed")

    def _apply_payload(self, payload: dict[str, Any]) -> bool:
        """Apply one durable/live event without ever widening its scope."""
        try:
            user_id = int(payload["user_id"])
            version = int(payload["revoked_at_version"])
        except (KeyError, TypeError, ValueError):
            return False
        if user_id <= 0 or version < 0:
            return False
        scope = payload.get("scope") or "user_version"
        if scope == "jti":
            digest = payload.get("token_jti_hash")
            if not _valid_digest(digest):
                return False
            self._jti_cache[digest] = True
            return True
        if scope == "sid":
            digest = payload.get("session_id_hash")
            if not _valid_digest(digest):
                return False
            self._sid_cache[digest] = True
            return True
        if scope != "user_version":
            return False
        existing = self._cache.get(user_id, 0)
        if version > existing:
            self._cache[user_id] = version
        return True

    async def _reconnect(self) -> None:
        """Open a fresh Redis connection + pubsub and replay cold-start.

        Called after a connection error from inside the subscriber loop.
        On success, the subscriber loop continues from the new pubsub.
        On failure, exception propagates back to ``_run_subscriber``
        which handles backoff.
        """
        # Tear down whatever's left of the broken connection. Best
        # effort — if these throw we don't care, we're about to make
        # a new one.
        if self._pubsub is not None:
            try:
                await self._pubsub.aclose()
            except Exception:  # noqa: BLE001
                pass
            self._pubsub = None
        if self._redis is not None:
            try:
                await self._redis.aclose()
            except Exception:  # noqa: BLE001
                pass
            self._redis = None

        # Long-lived pub/sub: enable periodic PING + TCP keepalive so an idle
        # subscriber connection isn't silently torn down. Without this an idle
        # read could time out / drop, flipping ``_ready=False`` and 503-ing auth
        # until the reconnect+resync completes. Keeps fail-closed semantics
        # intact while removing the spurious idle-disconnect flapping.
        self._redis = _from_url(
            settings.REDIS_URL,
            decode_responses=True,
            health_check_interval=30,
            socket_keepalive=True,
        )
        self._pubsub = self._redis.pubsub()
        await self._pubsub.subscribe(settings.REDIS_REVOCATION_CHANNEL)
        # Fresh HTTP client before replay so CSP's new container IP resolves.
        await self._reset_http_client()
        # Subscribe first so publishes during replay queue on this connection;
        # replay then closes the preceding disconnected interval.
        await self._cold_start_sync()


def get_revocation_cache() -> RevocationCache:
    """Module-level singleton accessor.

    FastAPI dependencies should use this so the same cache instance
    backs both the lifespan callbacks (``start``/``stop``) and the
    per-request ``is_revoked`` calls inside the JWT verifier.
    """
    global _singleton
    if _singleton is None:
        _singleton = RevocationCache()
    return _singleton
