"""Run CSP periodic loops in exactly one process.

Uvicorn workers each execute the app lifespan. Alert scans, retention,
and the keyring maintainer must not run N times. Usage rows are the
exception: each worker writes the queue it filled. A Redis lock elects
one leader for the rest. Renew and release compare the token in one Lua
script, so a lease that expires between two commands cannot be extended
or deleted by the worker that lost it. If renewal fails, the loops are
cancelled before this process tries to take the lock again.

Under pytest there is one process and often no Redis, so the loops start
the way they did before workers existed.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

LEADER_KEY = "anila:csp:background-leader"
StartLoops = Callable[[], Awaitable[list[asyncio.Task]]]

# One round trip. GET then EXPIRE can extend a lease another worker took
# after the first command. The script expires only when the token matches.
_RENEW_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('expire', KEYS[1], ARGV[2])
end
return 0
"""

_RELEASE_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""


async def renew_lease(redis: Any, key: str, token: str, ttl: float) -> bool:
    renewed = await redis.eval(_RENEW_LUA, 1, key, token, str(int(max(1, ttl))))
    return bool(renewed)


async def release_lease(redis: Any, key: str, token: str) -> bool:
    released = await redis.eval(_RELEASE_LUA, 1, key, token)
    return bool(released)


async def run_single_leader(
    start_loops: StartLoops,
    *,
    client: Any = None,
    key: str = LEADER_KEY,
    ttl: float = 20,
    poll: float = 2,
) -> None:
    if client is None and os.environ.get("PYTEST_CURRENT_TEST"):
        tasks = await start_loops()
        try:
            await asyncio.Event().wait()
        finally:
            await _cancel(tasks)
        return

    token = uuid.uuid4().hex
    renew_every = max(0.05, ttl / 3)
    while True:
        redis = client if client is not None else await _redis()
        if redis is None:
            logger.error("background leader has no Redis; periodic loops are paused")
            await asyncio.sleep(poll)
            continue
        try:
            owned = await redis.set(key, token, nx=True, ex=int(max(1, ttl)))
        except Exception:
            logger.exception("background leader could not acquire the lock")
            await asyncio.sleep(poll)
            continue
        if not owned:
            await asyncio.sleep(poll)
            continue
        tasks: list[asyncio.Task] = []
        try:
            logger.info("background leader acquired %s", key)
            tasks = list(await start_loops())
            while True:
                await asyncio.sleep(renew_every)
                if not await renew_lease(redis, key, token, ttl):
                    logger.warning("background leader lost %s", key)
                    break
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("background leader loop failed")
        finally:
            await _cancel(tasks)
            try:
                await release_lease(redis, key, token)
            except Exception:
                logger.exception("background leader could not release %s", key)


async def _cancel(tasks: list[asyncio.Task]) -> None:
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _redis() -> Any | None:
    url = os.environ.get("REDIS_URL")
    if not url:
        return None
    try:
        import redis.asyncio as aioredis

        return aioredis.from_url(url, decode_responses=True, socket_timeout=1.0)
    except Exception:
        logger.exception("background leader redis client failed")
        return None
