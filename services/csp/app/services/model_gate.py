"""Per-model concurrency gate shared by every CSP process.

The registrant sets ``model_registry.max_concurrent``. NULL means unlimited.
A positive limit is a Redis-backed semaphore: holders keep a lease, and
everyone else waits in a queue served round-robin per user. One user's
burst cannot take the next slot while someone else is waiting.

The decision function ``apply`` is pure. ``MemoryBoard`` and ``RedisBoard``
both call it under a lock, so tests and production share one algorithm.
A process-local board is only for one worker (pytest, or uvicorn with no
extra workers). Several workers and no ``REDIS_URL`` refuse to start.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from typing import Any, Callable

logger = logging.getLogger(__name__)

QUEUE_WAIT_SECONDS = 120
LEASE_MS = 30_000
STALE_MS = 5_000
POLL_SECONDS = 0.4

UNAVAILABLE_MESSAGE = "暫時無法確認這個模型的使用人數，請稍後再試"


def queue_status_message(position: int) -> str:
    return f"目前使用人數較多，排隊中，你是第 {position} 位"


def queue_timeout_message() -> str:
    return f"排隊超過 {QUEUE_WAIT_SECONDS} 秒，請稍後再試"


class ModelSlotDenied(Exception):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def fresh_state() -> dict[str, Any]:
    return {"inflight": {}, "order": [], "queues": {}, "meta": {}}


def _now_ms() -> int:
    return int(time.time() * 1000)


def _purge(state: dict[str, Any], now: int) -> None:
    inflight = state["inflight"]
    for token, expiry in list(inflight.items()):
        if int(expiry) <= now:
            inflight.pop(token, None)
            state["meta"].pop(token, None)
    meta = state["meta"]
    queues: dict[str, list[str]] = state["queues"]
    for token, info in list(meta.items()):
        if token in inflight:
            continue
        if now - int(info.get("heartbeat", 0)) > STALE_MS:
            user = str(info.get("user"))
            meta.pop(token, None)
            q = queues.get(user) or []
            queues[user] = [item for item in q if item != token]
            if not queues[user]:
                queues.pop(user, None)
    state["order"] = [
        user for user in state["order"] if queues.get(user)
    ]


def _head(state: dict[str, Any]) -> str | None:
    queues = state["queues"]
    while state["order"]:
        user = state["order"][0]
        q = queues.get(user) or []
        if q:
            return q[0]
        state["order"].pop(0)
        queues.pop(user, None)
    return None


def _position(state: dict[str, Any], ticket: str) -> int:
    order = list(state["order"])
    queues = {user: list(items) for user, items in state["queues"].items()}
    pos = 0
    guard = 0
    while order and guard < 100_000:
        guard += 1
        user = order[0]
        q = queues.get(user) or []
        if not q:
            order.pop(0)
            continue
        pos += 1
        found = q.pop(0)
        if found == ticket:
            return pos
        if q:
            order.append(order.pop(0))
        else:
            order.pop(0)
    return pos or 1


def _grant(state: dict[str, Any], ticket: str, now: int) -> None:
    info = state["meta"][ticket]
    user = str(info["user"])
    q = state["queues"].get(user) or []
    if q and q[0] == ticket:
        q.pop(0)
    state["queues"][user] = q
    if state["order"] and state["order"][0] == user:
        state["order"].pop(0)
        if q:
            state["order"].append(user)
    if not q:
        state["queues"].pop(user, None)
    state["inflight"][ticket] = now + LEASE_MS
    info["heartbeat"] = now


def _enqueue(state: dict[str, Any], ticket: str, user: str, now: int) -> None:
    state["meta"][ticket] = {
        "user": user,
        "heartbeat": now,
        "enqueued": now,
    }
    q = state["queues"].setdefault(user, [])
    if ticket not in q:
        q.append(ticket)
    if user not in state["order"]:
        state["order"].append(user)


def apply(state: dict[str, Any], op: dict[str, Any], now: int) -> dict[str, Any]:
    """Mutate ``state`` and return a status dict. ``now`` is epoch milliseconds."""
    kind = op.get("op")
    ticket = str(op.get("ticket") or "")
    # A poll is proof of life for this ticket. Refresh it before purge so a
    # waiter who reaches the 120s ceiling is told they timed out, instead of
    # being deleted as idle in the same step. Other idle tickets still drop.
    if kind == "poll":
        info = state["meta"].get(ticket)
        if info is not None:
            info["heartbeat"] = now
    _purge(state, now)
    if kind == "snapshot":
        queued = sum(len(items) for items in state["queues"].values())
        return {"status": "snapshot", "inflight": len(state["inflight"]), "queue_length": queued}

    if kind == "release":
        state["inflight"].pop(ticket, None)
        info = state["meta"].pop(ticket, None)
        if info:
            user = str(info["user"])
            q = [item for item in state["queues"].get(user, []) if item != ticket]
            if q:
                state["queues"][user] = q
            else:
                state["queues"].pop(user, None)
                state["order"] = [item for item in state["order"] if item != user]
        return {"status": "released"}

    if kind == "pulse":
        if ticket in state["inflight"]:
            state["inflight"][ticket] = now + LEASE_MS
        return {"status": "pulsed"}

    limit = int(op["limit"])
    user = str(op.get("user") or "")

    if kind == "arrive":
        if ticket in state["inflight"]:
            return {"status": "granted", "position": 0}
        room = len(state["inflight"]) < limit
        waiting = any(state["queues"].values())
        if room and not waiting:
            state["meta"][ticket] = {"user": user, "heartbeat": now, "enqueued": now}
            state["inflight"][ticket] = now + LEASE_MS
            return {"status": "granted", "position": 0}
        _enqueue(state, ticket, user, now)
        return {"status": "queued", "position": _position(state, ticket)}

    if kind == "poll":
        if ticket in state["inflight"]:
            return {"status": "granted", "position": 0}
        info = state["meta"].get(ticket)
        if info is None:
            return {"status": "missing", "position": 0}
        info["heartbeat"] = now
        enqueued = int(info.get("enqueued", now))
        if now - enqueued >= QUEUE_WAIT_SECONDS * 1000:
            apply(state, {"op": "release", "ticket": ticket}, now)
            return {"status": "timeout", "position": 0}
        if len(state["inflight"]) < limit and _head(state) == ticket:
            _grant(state, ticket, now)
            return {"status": "granted", "position": 0}
        return {"status": "queued", "position": _position(state, ticket)}

    raise ValueError(f"unknown gate op {kind!r}")


class MemoryBoard:
    """One lock, shared by every admission in this process (and by tests)."""

    def __init__(self) -> None:
        self.states: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    async def mutate(
        self,
        model_id: int,
        fn: Callable[[dict[str, Any], int], dict[str, Any]],
        now: int,
    ) -> dict[str, Any]:
        async with self._lock:
            state = self.states.setdefault(str(model_id), fresh_state())
            return fn(state, now)


# Compare-and-delete. GET then DELETE can drop a lock another worker
# took after the first command.
_UNLOCK_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""


class RedisBoard:
    """Same ``apply`` function, serialized across processes with a Redis lock."""

    def __init__(self, client: Any) -> None:
        self.client = client

    async def mutate(
        self,
        model_id: int,
        fn: Callable[[dict[str, Any], int], dict[str, Any]],
        now: int,
    ) -> dict[str, Any]:
        lock_key = f"anila:mgate:lock:{model_id}"
        state_key = f"anila:mgate:state:{model_id}"
        token = uuid.uuid4().hex
        acquired = False
        for _ in range(100):
            ok = await self.client.set(lock_key, token, nx=True, ex=5)
            if ok:
                acquired = True
                break
            await asyncio.sleep(0.01)
        if not acquired:
            raise ModelSlotDenied(UNAVAILABLE_MESSAGE, code="model_queue_unavailable")
        try:
            raw = await self.client.get(state_key)
            state = json.loads(raw) if raw else fresh_state()
            result = fn(state, now)
            await self.client.set(state_key, json.dumps(state, separators=(",", ":")))
            return result
        finally:
            try:
                await self.client.eval(_UNLOCK_LUA, 1, lock_key, token)
            except Exception:
                logger.warning("model gate unlock failed model=%s", model_id)


_board: MemoryBoard | RedisBoard | None = None


def _argv(pid: int) -> list[str]:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            raw = handle.read()
    except OSError:
        return []
    return [part.decode("utf-8", "surrogateescape") for part in raw.split(b"\0") if part]


def _workers_in(argv: list[str]) -> int | None:
    for index, arg in enumerate(argv):
        if arg == "--workers" and index + 1 < len(argv):
            try:
                return max(1, int(argv[index + 1]))
            except ValueError:
                return 2
        if arg.startswith("--workers="):
            try:
                return max(1, int(arg.split("=", 1)[1]))
            except ValueError:
                return 2
    return None


def process_worker_count() -> int:
    """Uvicorn ``--workers`` on this process or its parent. Pytest is one."""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return 1
    for pid in (os.getpid(), os.getppid()):
        found = _workers_in(_argv(pid))
        if found is not None:
            return found
    return 1


def require_model_gate() -> None:
    """Refuse to boot when several workers would each keep a private board."""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return
    if os.environ.get("REDIS_URL"):
        return
    count = process_worker_count()
    if count <= 1:
        return
    logger.error(
        "模型排隊拒絕啟動：沒有 REDIS_URL，而這個 process 是 %s 個 worker 之一。"
        "程序內計數板會把同時處理上限拆開，所以不啟動。",
        count,
    )
    raise RuntimeError(
        f"沒有 REDIS_URL，{count} 個 worker 不能改用程序內的模型排隊計數板"
    )


def get_board() -> MemoryBoard | RedisBoard:
    global _board
    if _board is not None:
        return _board
    if os.environ.get("PYTEST_CURRENT_TEST"):
        _board = MemoryBoard()
        return _board
    url = os.environ.get("REDIS_URL")
    if not url:
        if process_worker_count() <= 1:
            _board = MemoryBoard()
            return _board
        require_model_gate()
    import redis.asyncio as aioredis

    client = aioredis.from_url(url, decode_responses=True, socket_timeout=1.0)
    _board = RedisBoard(client)
    return _board


def reset_board_for_tests() -> None:
    global _board
    _board = None


class ModelAdmission:
    def __init__(
        self,
        model_id: int,
        user_id: int | str,
        limit: int,
        *,
        board: MemoryBoard | RedisBoard | None = None,
    ) -> None:
        self.model_id = int(model_id)
        self.user = str(user_id)
        self.limit = int(limit)
        self.board = board or get_board()
        self.ticket = uuid.uuid4().hex
        self.held = False

    @classmethod
    def maybe(cls, model: Any, user_id: int | None, board: Any = None) -> ModelAdmission | None:
        limit = getattr(model, "max_concurrent", None)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            return None
        model_id = getattr(model, "id", None)
        if model_id is None:
            return None
        who = user_id if user_id is not None else f"anon:{uuid.uuid4().hex}"
        return cls(int(model_id), who, limit, board=board)

    async def _step(self, kind: str) -> dict[str, Any]:
        op = {"op": kind, "ticket": self.ticket, "user": self.user, "limit": self.limit}

        def _fn(state: dict[str, Any], now: int) -> dict[str, Any]:
            return apply(state, op, now)

        try:
            return await self.board.mutate(self.model_id, _fn, _now_ms())
        except ModelSlotDenied:
            raise
        except Exception as exc:
            logger.warning("model gate redis failed model=%s: %s", self.model_id, exc)
            raise ModelSlotDenied(UNAVAILABLE_MESSAGE, code="model_queue_unavailable") from exc

    async def wait_positions(self):
        outcome = await self._step("arrive")
        last_position = 1
        while True:
            status = outcome.get("status")
            if status == "granted":
                self.held = True
                return
            if status == "timeout":
                raise ModelSlotDenied(queue_timeout_message(), code="model_queue_timeout")
            if status == "missing":
                raise ModelSlotDenied(queue_timeout_message(), code="model_queue_timeout")
            position = int(outcome.get("position") or 1)
            last_position = position
            yield position
            await asyncio.sleep(POLL_SECONDS)
            try:
                outcome = await self._step("poll")
            except ModelSlotDenied as exc:
                # The ticket is already in the queue. A lock timeout must
                # not turn that wait into an error; arrive still fails closed.
                if exc.code != "model_queue_unavailable":
                    raise
                outcome = {"status": "queued", "position": last_position}

    async def wait_granted(self) -> None:
        async for _position in self.wait_positions():
            pass

    async def pulse_forever(self) -> None:
        try:
            while True:
                await asyncio.sleep(10)
                await self._step("pulse")
        except asyncio.CancelledError:
            raise

    async def release(self) -> None:
        if not self.ticket:
            return
        try:
            await self._step("release")
        except Exception:
            logger.warning("model gate release failed model=%s", self.model_id, exc_info=True)
        self.held = False


def read_metrics(model_ids: list[int]) -> dict[int, tuple[int, int]]:
    """Best-effort inflight / queue lengths. Empty when Redis is not in use."""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return {}
    url = os.environ.get("REDIS_URL")
    if not url or not model_ids:
        return {}
    try:
        import redis

        client = redis.Redis.from_url(
            url, decode_responses=True, socket_timeout=0.3, socket_connect_timeout=0.3,
        )
        now = _now_ms()
        out: dict[int, tuple[int, int]] = {}
        for model_id in model_ids:
            raw = client.get(f"anila:mgate:state:{model_id}")
            if not raw:
                out[int(model_id)] = (0, 0)
                continue
            state = json.loads(raw)
            snap = apply(state, {"op": "snapshot"}, now)
            out[int(model_id)] = (int(snap["inflight"]), int(snap["queue_length"]))
        return out
    except Exception:
        logger.warning("model gate metrics unavailable", exc_info=True)
        return {}
