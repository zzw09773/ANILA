"""Real-Redis durability and fencing tests for the Studio queue.

These tests are intentionally opt-in because the regular unit suite must not
silently depend on a developer's Redis.  Point ``ANILA_STUDIO_REDIS_TEST_URL``
at an isolated Redis 7 database.  The optional AOF restart case additionally
requires ``ANILA_STUDIO_REDIS_DOCKER_CONTAINER`` naming that disposable
container; it never restarts the platform Redis implicitly.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
import uuid
from dataclasses import replace

import pytest
import pytest_asyncio
from redis.asyncio import from_url

from app.config import settings
from app.services.job_store import JobStore, LeaseLost, PersistedJob


REDIS_URL = os.getenv("ANILA_STUDIO_REDIS_TEST_URL")
DOCKER_CONTAINER = os.getenv("ANILA_STUDIO_REDIS_DOCKER_CONTAINER")
pytestmark = pytest.mark.skipif(
    not REDIS_URL,
    reason="ANILA_STUDIO_REDIS_TEST_URL must point to an isolated Redis 7 DB",
)


def _job(job_id: str, *, artifact_type: str = "report", max_attempts: int = 3):
    return PersistedJob(
        job_id=job_id,
        artifact_type=artifact_type,
        owner_user_id=7,
        state="pending",
        status_view={"state": "pending"},
        collection_id=11,
        task_id="21",
        source_snapshot_id="31",
        request_spec={"title": f"job-{job_id}"},
        max_attempts=max_attempts,
    )


@pytest_asyncio.fixture
async def redis_store(monkeypatch):
    prefix = f"test:studio:{uuid.uuid4().hex}:"
    monkeypatch.setattr(settings, "REDIS_URL", REDIS_URL)
    monkeypatch.setattr(settings, "JOB_STORE_KEY_PREFIX", prefix)
    monkeypatch.setattr(settings, "JOB_STORE_TTL_SECONDS", 120)
    client = from_url(REDIS_URL, decode_responses=True)
    await client.flushdb()
    store = JobStore()
    await store.start()
    assert store.ready
    try:
        yield store, client, prefix
    finally:
        await store.stop()
        await client.flushdb()
        await client.aclose()


@pytest.mark.asyncio
async def test_more_than_128_stale_index_rows_cannot_starve_valid_work(redis_store):
    store, client, prefix = redis_store
    await store.enqueue(_job("valid"), now_epoch=10)
    ready_key = f"{prefix.rstrip(':')}:ready"
    all_key = f"{prefix.rstrip(':')}:all"
    stale = {f"stale-{index}": 0 for index in range(140)}
    await client.zadd(ready_key, stale)
    await client.sadd(all_key, *stale)

    claimed = await store.claim(worker_id="worker-a", lease_seconds=30, now_epoch=10)

    assert claimed is not None
    assert claimed.job_id == "valid"
    assert await client.zcard(ready_key) == 0
    remaining = await client.smembers(all_key)
    assert set(stale).isdisjoint(remaining)


@pytest.mark.asyncio
async def test_concurrent_claim_and_stale_owner_are_fenced(redis_store):
    store, _client, _prefix = redis_store
    await store.enqueue(_job("race"), now_epoch=100)
    first, second = await asyncio.gather(
        store.claim_job("race", worker_id="a", lease_seconds=10, now_epoch=100),
        store.claim_job("race", worker_id="b", lease_seconds=10, now_epoch=100),
    )
    claimed = first or second
    assert claimed is not None
    assert (first is None) != (second is None)

    with pytest.raises(LeaseLost):
        await store.heartbeat(
            "race", "not-the-owner", lease_seconds=10, now_epoch=101
        )
    await store.heartbeat(
        "race", claimed.lease_token or "", lease_seconds=10, now_epoch=101
    )
    recovered = await store.reap_expired(now_epoch=112)
    assert recovered == ["race"]
    replacement = await store.claim(
        worker_id="replacement", lease_seconds=10, now_epoch=112
    )
    assert replacement is not None
    assert replacement.attempt_count == 2
    assert replacement.restart_count == 1

    stale_projection = replace(claimed, stage="stale-write")
    with pytest.raises(LeaseLost):
        await store.update_claimed(
            stale_projection,
            claimed.lease_token or "",
            now_epoch=112,
        )


@pytest.mark.asyncio
async def test_retry_then_dlq_and_nonterminal_envelopes_never_expire(redis_store):
    store, client, prefix = redis_store
    key = f"{prefix}dlq"
    await store.enqueue(_job("dlq", max_attempts=2), now_epoch=1)
    assert await client.ttl(key) == -1

    first = await store.claim(worker_id="one", lease_seconds=10, now_epoch=1)
    assert first is not None
    assert await client.ttl(key) == -1
    state = await store.retry_or_dead_letter(
        "dlq",
        first.lease_token or "",
        error="transient",
        retry_delay_seconds=1,
        now_epoch=2,
    )
    assert state == "retry_wait"
    assert await client.ttl(key) == -1

    second = await store.claim(worker_id="two", lease_seconds=10, now_epoch=3)
    assert second is not None and second.attempt_count == 2
    state = await store.retry_or_dead_letter(
        "dlq",
        second.lease_token or "",
        error="permanent",
        retry_delay_seconds=1,
        now_epoch=4,
    )
    assert state == "dead_letter"
    terminal = await store.get("dlq")
    assert terminal is not None and terminal.state == "dead_letter"
    assert 0 < await client.ttl(key) <= settings.JOB_STORE_TTL_SECONDS
    assert not await client.sismember(f"{prefix.rstrip(':')}:all", "dlq")


@pytest.mark.asyncio
async def test_terminal_report_pending_survives_restartable_reclaim(redis_store):
    store, client, prefix = redis_store
    await store.enqueue(_job("terminal-report", max_attempts=1), now_epoch=1)
    claimed = await store.claim(worker_id="one", lease_seconds=30, now_epoch=1)
    assert claimed is not None
    pending = replace(
        claimed,
        stage="terminal_report_pending",
        checkpoint={
            "terminal_status": "failed",
            "local_terminal_state": "dead_letter",
        },
    )
    await store.update_claimed(pending, claimed.lease_token or "", now_epoch=2)
    await store.defer_terminal_report(
        pending.job_id,
        pending.lease_token or "",
        error="CSP temporarily unavailable",
        retry_delay_seconds=1,
        now_epoch=2,
    )
    assert await client.ttl(f"{prefix}terminal-report") == -1
    reclaimed = await store.claim(worker_id="two", lease_seconds=30, now_epoch=3)
    assert reclaimed is not None
    assert reclaimed.checkpoint["terminal_status"] == "failed"
    assert reclaimed.stage == "terminal_report_pending"


def _docker_redis_url(container: str) -> str:
    output = subprocess.check_output(
        ["docker", "port", container, "6379/tcp"],
        text=True,
        timeout=10,
    ).strip().splitlines()[0]
    host_port = int(output.rsplit(":", 1)[1])
    return f"redis://127.0.0.1:{host_port}/0"


@pytest.mark.asyncio
@pytest.mark.skipif(
    not DOCKER_CONTAINER,
    reason="ANILA_STUDIO_REDIS_DOCKER_CONTAINER is required for AOF restart",
)
async def test_aof_restart_reconciles_all_five_pipeline_envelopes(monkeypatch):
    redis_url = _docker_redis_url(DOCKER_CONTAINER)
    prefix = f"test:studio:restart:{uuid.uuid4().hex}:"
    monkeypatch.setattr(settings, "REDIS_URL", redis_url)
    monkeypatch.setattr(settings, "JOB_STORE_KEY_PREFIX", prefix)
    client = from_url(redis_url, decode_responses=True)
    await client.flushdb()
    assert await client.config_get("appendonly") == {"appendonly": "yes"}

    before = JobStore()
    await before.start()
    artifact_types = ["slides", "report", "mindmap", "infographic", "datatable"]
    for index, artifact_type in enumerate(artifact_types, start=1):
        await before.enqueue(
            _job(f"restart-{index}", artifact_type=artifact_type),
            now_epoch=10,
        )
    await before.stop()
    await client.aclose()
    # appendfsync=everysec: bound the evidence wait to one fsync interval.
    await asyncio.sleep(1.2)

    subprocess.run(
        ["docker", "restart", DOCKER_CONTAINER],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    deadline = time.monotonic() + 20
    restarted_url = _docker_redis_url(DOCKER_CONTAINER)
    monkeypatch.setattr(settings, "REDIS_URL", restarted_url)
    while True:
        probe = from_url(restarted_url, decode_responses=True)
        try:
            if await probe.ping():
                break
        except Exception:  # noqa: BLE001 - bounded container readiness poll
            if time.monotonic() >= deadline:
                raise
            await asyncio.sleep(0.2)
        finally:
            await probe.aclose()

    after = JobStore()
    await after.start()
    stats = await after.reconcile_indexes()
    assert stats == {"active": 5, "claimable": 5, "leased": 0}
    claimed_types: set[str] = set()
    for _ in artifact_types:
        claimed = await after.claim(
            worker_id="after-restart", lease_seconds=30, now_epoch=11
        )
        assert claimed is not None
        claimed_types.add(claimed.artifact_type)
    assert claimed_types == set(artifact_types)
    await after.stop()
    cleanup = from_url(restarted_url, decode_responses=True)
    await cleanup.flushdb()
    await cleanup.aclose()
