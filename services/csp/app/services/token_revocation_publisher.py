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


# Module-level singleton for the async Redis client. Cleared by tests
# via monkeypatch.setattr(..., "_client_singleton", None).
_client_singleton = None  # type: ignore[var-annotated]


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
            bump. Subscribers compare ``jwt.tv >= revoked_at_version``
            and reject when the inequality holds.
        redis_url: Override the default URL (mostly for tests).

    The function MUST be called only after the corresponding DB
    write has committed. Flipping the order would risk broadcasting
    a revocation that ends up rolled back and over-invalidating
    subscribers.
    """
    payload = {
        "user_id": user_id,
        "revoked_at_version": revoked_at_version,
        "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "schema_version": SCHEMA_VERSION,
    }
    message = json.dumps(payload, separators=(",", ":"))

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
