"""Redis pub/sub publisher for JWT token-revocation events.

csp/backend is the *source-of-truth* for user JWT validity (it owns
the ``users.token_version`` column and the new ``token_revocations``
audit table). Downstream services that verify CSP-signed JWTs — most
immediately the soon-to-be-extracted ``anila-studio`` — need to know
when a token has been revoked so they can:

  1. Reject the in-flight request that's still carrying the old token.
  2. Evict any cached resolution of that token from their in-memory
     deny / allow lists.

This module fans out revocation events on the Redis channel
``anila:auth:token-revoke``. The payload schema is::

    {"user_id": int,
     "revoked_at_version": int,
     "ts": "<ISO-8601 UTC>",
     "schema_version": 1}

Why best-effort, not fail-closed
================================

There are two ends to this pipe. Each gets the opposite half of the
R14 fail-closed trade-off:

* **csp/backend (this module, the publisher) — best-effort.**
  The DB bump on ``users.token_version`` AND the row insert into
  ``token_revocations`` are already committed by the time we get
  here. If Redis is unreachable we log the error and return; the
  caller's HTTP request still succeeds because the durable record
  is in place. Subscribers cold-starting later replay it via
  ``GET /api/auth/revocations?since=...``.

* **anila-studio (subscriber, separate service) — fail-closed.**
  When a subscriber can't reach Redis it has no way to know if it's
  missing a revocation, so it must refuse to verify tokens (503).
  That code lives in the studio service, not here.

This split keeps a temporary Redis outage from locking out every
user trying to log out (csp side) while still preventing a stale
deny-list from leaking permissions on the consumer side.

Lazy connection
===============

The Redis client is built on first call, not on import. Required:

* pytest collection happens on machines that have no Redis at all.
* The redis-py asyncio client binds to the running event loop, so
  building it at import time would tie it to the wrong loop.

The factory ``_get_redis_client`` is module-public so tests can
monkeypatch it with an in-memory fake — see
``tests/test_token_revoke_publish.py``.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


# Redis channel name. Downstream subscribers (anila-studio,
# documentation runbooks) MUST match this constant. Don't break the
# string without coordinating a cutover.
CHANNEL = "anila:auth:token-revoke"

# Payload schema. Bump alongside any breaking change so subscribers
# can downgrade gracefully on encountering a newer event.
SCHEMA_VERSION = 1

# Default Redis URL — anila-platform docker-compose puts a Redis at
# this hostname, and tests don't need a real connection.
DEFAULT_REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")

# 同步發布的 Redis 逾時由呼叫端以必填關鍵字傳入；這裡不再讀環境變數，
# 也不保留另一份會與治理頁分歧的預設值。


# Module-level singleton for the async Redis client. Cleared by tests
# via monkeypatch.setattr(..., "_client_singleton", None).
_client_singleton = None  # type: ignore[var-annotated]


def _revocation_message(user_id: int, revoked_at_version: int) -> str:
    payload = {
        "user_id": user_id,
        "revoked_at_version": revoked_at_version,
        "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "schema_version": SCHEMA_VERSION,
    }
    return json.dumps(payload, separators=(",", ":"))


async def _get_redis_client(redis_url: Optional[str] = None):
    """Return (and cache) an ``redis.asyncio.Redis`` client.

    Imported lazily so a missing redis package or unreachable broker
    doesn't break pytest collection — failures land at publish time
    where the outer try/except already eats them.

    The cached client is bound to whichever event loop ran the first
    call. For FastAPI that's fine — every request handler runs on
    the same loop. Tests bypass the cache entirely.
    """
    global _client_singleton
    if _client_singleton is not None:
        return _client_singleton

    # Local import: keeps the module importable even when ``redis``
    # is not installed, and avoids early-binding to a stale event loop.
    import redis.asyncio as aioredis  # type: ignore[import-not-found]

    url = redis_url or DEFAULT_REDIS_URL
    _client_singleton = aioredis.from_url(url, decode_responses=True)
    return _client_singleton


async def publish_revocation(
    user_id: int,
    revoked_at_version: int,
    *,
    redis_url: Optional[str] = None,
) -> None:
    """Broadcast one revocation event. Never raises.

    Args:
        user_id: The user whose tokens are now invalid.
        revoked_at_version: ``users.token_version`` value AFTER the
            bump — the lowest version that is still valid. Subscribers
            compare ``jwt.tv < revoked_at_version`` and reject when the
            inequality holds; a token stamped ``tv == revoked_at_version``
            was issued *by* this bump and must be honoured. (This line
            said ``>=`` until 2026-07-31, contradicting
            ``models/token_revocation.py`` — and ``>=`` is what the
            consumers were coded against, so any password change locked
            the user out of studio and ASR permanently.)
        redis_url: Override the default URL (mostly for tests).

    The function MUST be called only after the corresponding DB
    write has committed. Flipping the order would risk broadcasting
    a revocation that ends up rolled back and over-invalidating
    subscribers.
    """
    message = _revocation_message(user_id, revoked_at_version)

    try:
        client = await _get_redis_client(redis_url)
    except Exception:  # noqa: BLE001
        # Importing / building the client failed — usually because
        # redis-py isn't installed or the URL is malformed. Still
        # best-effort; the DB row keeps the cold-start path working.
        logger.exception(
            "token-revoke publish skipped: cannot build Redis client "
            "(user_id=%s version=%s)",
            user_id,
            revoked_at_version,
        )
        return

    try:
        await client.publish(CHANNEL, message)
    except Exception:  # noqa: BLE001
        # Redis blip / network issue. Subscribers will reconcile on
        # their next cold-start sync, so we eat it and move on.
        logger.exception(
            "token-revoke publish failed (user_id=%s version=%s)",
            user_id,
            revoked_at_version,
        )


def _make_sync_redis_client(redis_url: Optional[str] = None, *, timeout: float):
    """Return a short-lived sync Redis client for sync FastAPI endpoints.

    ``timeout`` is a **required keyword**: the caller resolved it through the
    settings registry while it still held a DB session. Giving it a default here
    would recreate the second-default drift Task 3 retired — the page would show
    one number and this client would use another, with no error anywhere.
    """
    import redis  # type: ignore[import-not-found]

    url = redis_url or DEFAULT_REDIS_URL
    return redis.from_url(
        url,
        decode_responses=True,
        socket_connect_timeout=timeout,
        socket_timeout=timeout,
    )


def publish_revocation_sync(
    user_id: int,
    revoked_at_version: int,
    *,
    redis_url: Optional[str] = None,
    timeout: float,
) -> None:
    """Broadcast one revocation event from a synchronous call path.

    Synchronous FastAPI endpoints run in worker threads. Calling the async
    publisher through ``asyncio.run()`` creates a throwaway event loop and can
    poison the cached redis.asyncio client for later calls. This path uses a
    short-lived synchronous Redis client instead. It remains best-effort and
    never raises.
    """
    message = _revocation_message(user_id, revoked_at_version)
    client = None
    try:
        client = _make_sync_redis_client(redis_url, timeout=timeout)
    except Exception:  # noqa: BLE001
        logger.exception(
            "token-revoke sync publish skipped: cannot build Redis client "
            "(user_id=%s version=%s)",
            user_id,
            revoked_at_version,
        )
        return

    try:
        client.publish(CHANNEL, message)
    except Exception:  # noqa: BLE001
        logger.exception(
            "token-revoke sync publish failed (user_id=%s version=%s)",
            user_id,
            revoked_at_version,
        )
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            logger.debug("token-revoke sync Redis close failed", exc_info=True)


def _kid_revocation_message(kids: list[str]) -> str:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "event": "jwt_kid_revoke",
        "revoked_kids": kids,
        "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    return json.dumps(payload, separators=(",", ":"))


def publish_kid_revocations_sync(
    kids,
    *,
    redis_url: Optional[str] = None,
    timeout: float,
) -> None:
    """緊急輪替之後，把退役的 kid 送到既有撤銷通道。失敗只記 log。

    必須在資料庫提交之後呼叫。訂閱端冷啟動改走
    ``GET /api/auth/revocations`` 的 ``revoked_kids``。
    """
    names = [kid for kid in kids if isinstance(kid, str) and kid]
    if not names:
        return
    message = _kid_revocation_message(names)
    client = None
    try:
        client = _make_sync_redis_client(redis_url, timeout=timeout)
    except Exception:  # noqa: BLE001
        logger.exception(
            "jwt-kid revoke sync publish skipped: cannot build Redis client (kids=%s)",
            names,
        )
        return
    try:
        client.publish(CHANNEL, message)
    except Exception:  # noqa: BLE001
        logger.exception("jwt-kid revoke sync publish failed (kids=%s)", names)
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            logger.debug("jwt-kid revoke sync Redis close failed", exc_info=True)
