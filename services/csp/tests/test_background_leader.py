"""Periodic CSP loops elect one process. A second process waits."""

from __future__ import annotations

import asyncio
import time

from app.services.background_leader import LEADER_KEY, release_lease, renew_lease, run_single_leader


class FakeRedis:
    def __init__(self) -> None:
        self.kv: dict[str, str] = {}
        self.deadline: dict[str, float] = {}

    def _live(self, key: str) -> None:
        deadline = self.deadline.get(key)
        if deadline is not None and time.monotonic() >= deadline:
            self.kv.pop(key, None)
            self.deadline.pop(key, None)

    async def set(self, key, value, nx=False, ex=None):
        self._live(key)
        if nx and key in self.kv:
            return None
        self.kv[key] = value
        if ex:
            self.deadline[key] = time.monotonic() + float(ex)
        return True

    async def get(self, key):
        self._live(key)
        return self.kv.get(key)

    async def delete(self, key):
        self.kv.pop(key, None)
        self.deadline.pop(key, None)

    async def expire(self, key, seconds):
        self._live(key)
        if key not in self.kv:
            return False
        self.deadline[key] = time.monotonic() + float(seconds)
        return True

    async def eval(self, script, numkeys, *keys_and_args):
        """Atomic stand-in for the leader Lua. No yield between compare and write."""
        key = keys_and_args[0]
        token = keys_and_args[numkeys]
        self._live(key)
        if self.kv.get(key) != token:
            return 0
        if "expire" in script:
            self.deadline[key] = time.monotonic() + float(keys_and_args[numkeys + 1])
            return 1
        self.kv.pop(key, None)
        self.deadline.pop(key, None)
        return 1

    def drop(self, key: str) -> None:
        self.kv.pop(key, None)
        self.deadline.pop(key, None)


def test_only_one_leader_runs_and_the_other_takes_over():
    async def _run():
        redis = FakeRedis()
        active = 0
        peak = 0
        started = 0

        def make_start():
            async def start():
                nonlocal active, peak, started
                active += 1
                started += 1
                peak = max(peak, active)

                async def hold():
                    nonlocal active
                    try:
                        await asyncio.Event().wait()
                    finally:
                        active -= 1

                return [asyncio.create_task(hold())]

            return start

        first = asyncio.create_task(
            run_single_leader(make_start(), client=redis, ttl=0.3, poll=0.05)
        )
        second = asyncio.create_task(
            run_single_leader(make_start(), client=redis, ttl=0.3, poll=0.05)
        )
        await asyncio.sleep(0.15)
        assert started == 1
        assert peak == 1
        assert active == 1
        # Process death: the holder stops, then the waiter takes the lock.
        first.cancel()
        await asyncio.sleep(0.4)
        assert started >= 2
        assert peak == 1
        assert active == 1
        first.cancel()
        second.cancel()
        await asyncio.gather(first, second, return_exceptions=True)
        assert active == 0

    asyncio.run(_run())


def test_renew_does_not_extend_a_lease_taken_between_commands():
    """GET then EXPIRE would lengthen the new owner's key. Lua must not."""

    async def _run():
        redis = FakeRedis()
        await redis.set("k", "old", ex=10)
        redis.drop("k")
        await redis.set("k", "new-owner", ex=15)
        deadline = redis.deadline["k"]
        assert await renew_lease(redis, "k", "old", 60) is False
        assert redis.kv["k"] == "new-owner"
        assert redis.deadline["k"] == deadline
        assert await release_lease(redis, "k", "old") is False
        assert redis.kv["k"] == "new-owner"

    asyncio.run(_run())


def test_lost_lease_cancels_loops_and_keeps_the_new_owner():
    async def _run():
        redis = FakeRedis()
        stopped = asyncio.Event()

        async def start():
            async def hold():
                try:
                    await asyncio.Event().wait()
                finally:
                    stopped.set()

            return [asyncio.create_task(hold())]

        task = asyncio.create_task(
            run_single_leader(start, client=redis, ttl=0.3, poll=0.05)
        )
        await asyncio.sleep(0.08)
        redis.kv[LEADER_KEY] = "new-owner"
        redis.deadline[LEADER_KEY] = time.monotonic() + 30
        await asyncio.wait_for(stopped.wait(), timeout=1)
        assert redis.kv[LEADER_KEY] == "new-owner"
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert redis.kv.get(LEADER_KEY) == "new-owner"

    asyncio.run(_run())
