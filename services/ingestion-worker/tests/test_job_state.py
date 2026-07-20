from __future__ import annotations

import asyncio

import pytest

from ingestion_worker import job_state


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, RuntimeError("db down")])
async def test_heartbeat_loss_cancels_owner(monkeypatch, failure):
    async def fake_heartbeat(*_args, **_kwargs):
        if failure is False:
            return False
        raise failure

    async def owner():
        await asyncio.sleep(3600)

    monkeypatch.setattr(job_state, "heartbeat", fake_heartbeat)
    task = asyncio.create_task(owner())
    await job_state.heartbeat_loop(
        object(),
        job_id=1,
        lease_token="lease",
        lease_seconds=60,
        interval_seconds=0,
        owner_task=task,
    )
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_reaper_loop_survives_transient_db_error(monkeypatch):
    calls = 0

    async def fake_reap(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("temporary database outage")
        raise asyncio.CancelledError

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(job_state, "reap_expired", fake_reap)
    monkeypatch.setattr(job_state.asyncio, "sleep", no_sleep)
    with pytest.raises(asyncio.CancelledError):
        await job_state.reaper_loop(
            object(), interval_seconds=1, limit=1, backoff_seconds=1
        )
    assert calls == 2
